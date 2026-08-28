#!/usr/bin/env python3
"""Rotate the C* active release without mutating an immutable release item.

This command is deliberately separate from the initial ledger bootstrap.  A
rotation adds a new release intent and conditionally moves the active pointer;
it never edits or deletes an existing release.  ``--check`` is read-only and
``--apply`` is required for the schedule/pointer mutation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time
import json
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

try:
    import boto3
except ImportError as error:  # pragma: no cover - operational dependency
    raise SystemExit("boto3 is required") from error

try:
    from deploy.bootstrap_shadow_cstar_ledger import (
        GROUP_NAME,
        START_NAME,
        STOP_NAME,
        BootstrapConfig,
        _generation,
        _read,
        _release,
        _release_item,
        _schedule_update_args,
        _selected,
        _validate_config,
        _validate_schedule,
        verify_source_artifacts,
    )
    from deploy.shadow_cstar_contract import (
        RELEASE_INTENT_KEYS,
        release_id_for,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from bootstrap_shadow_cstar_ledger import (  # type: ignore[no-redef]
        GROUP_NAME,
        START_NAME,
        STOP_NAME,
        BootstrapConfig,
        _generation,
        _read,
        _release,
        _release_item,
        _schedule_update_args,
        _selected,
        _validate_config,
        _validate_schedule,
        verify_source_artifacts,
    )
    from shadow_cstar_contract import (  # type: ignore[no-redef]
        RELEASE_INTENT_KEYS,
        release_id_for,
    )


REGION = "ap-northeast-2"
KST = ZoneInfo("Asia/Seoul")
ROTATION_SAFE_TIME = time(16, 0)


class RotationError(ValueError):
    """Raised when a C* release rotation precondition is not met."""


@dataclass(frozen=True)
class RotationState:
    old_release_id: str
    new_release_id: str
    new_release: Mapping[str, str]
    new_release_exists: bool


def _release_intent(item: Mapping[str, object]) -> dict[str, object]:
    return {key: item.get(key) for key in RELEASE_INTENT_KEYS}


def _validate_existing_release(
    table: Any,
    pointer: Mapping[str, object],
) -> str:
    old_release_id = pointer.get("release_id")
    if not isinstance(old_release_id, str) or len(old_release_id) != 64:
        raise RotationError("active release pointer is invalid")
    old_item = _read(table, {"PK": f"RELEASE#{old_release_id}", "SK": "META"})
    if old_item is None:
        raise RotationError("active release intent is missing")
    try:
        computed_id = release_id_for(_release_intent(old_item))
    except (TypeError, ValueError) as error:
        raise RotationError("active release intent is invalid") from error
    if computed_id != old_release_id:
        raise RotationError("active release pointer does not match its intent")
    return old_release_id


def _prepare_rotation(
    table: Any,
    config: BootstrapConfig,
    start_arn: str,
    stop_arn: str,
) -> RotationState:
    expected_generation = _generation(config, start_arn, stop_arn)
    actual_generation = _read(
        table,
        {"PK": str(expected_generation["PK"]), "SK": str(expected_generation["SK"])},
    )
    if actual_generation is None or _selected(
        actual_generation,
        {"schedule_generation", "protocol_version", "protocol_sha256", "schedule_arns"},
    ) != _selected(
        expected_generation,
        {"schedule_generation", "protocol_version", "protocol_sha256", "schedule_arns"},
    ):
        raise RotationError("schedule generation does not match")

    pointer_key = {"PK": "CONTROL#CSTAR", "SK": "RELEASE"}
    pointer = _read(table, pointer_key)
    if pointer is None or pointer.get("state") != "ACTIVE":
        raise RotationError("active release pointer is missing or inactive")
    old_release_id = _validate_existing_release(table, pointer)

    new_release = _release(config)
    new_release_id = release_id_for(new_release)
    new_item = _release_item(config, new_release)
    existing_new = _read(
        table,
        {"PK": str(new_item["PK"]), "SK": str(new_item["SK"])},
    )
    if existing_new is not None and _selected(
        existing_new,
        set(new_release),
    ) != _selected(new_item, set(new_release)):
        raise RotationError("new release intent already exists with different content")
    if new_release_id == old_release_id:
        raise RotationError("new release is already active; use --check for verification")
    return RotationState(
        old_release_id=old_release_id,
        new_release_id=new_release_id,
        new_release=new_release,
        new_release_exists=existing_new is not None,
    )


def _rotation_transactions(
    table_name: str,
    state: RotationState,
) -> list[dict[str, object]]:
    transactions: list[dict[str, object]] = []
    if not state.new_release_exists:
        item = {
            "PK": f"RELEASE#{state.new_release_id}",
            "SK": "META",
            **dict(state.new_release),
        }
        transactions.append({
            "Put": {
                "TableName": table_name,
                "Item": item,
                "ConditionExpression": "attribute_not_exists(PK)",
            }
        })
    transactions.append({
        "Update": {
            "TableName": table_name,
            "Key": {"PK": "CONTROL#CSTAR", "SK": "RELEASE"},
            "UpdateExpression": "SET release_id = :new_release_id, #state = :active",
            "ConditionExpression": (
                "release_id = :old_release_id AND #state = :active"
            ),
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": {
                ":new_release_id": state.new_release_id,
                ":old_release_id": state.old_release_id,
                ":active": "ACTIVE",
            },
        }
    })
    return transactions


def _assert_safe_rotation_time(now: datetime) -> None:
    """Avoid changing the active tuple during a weekday market session."""

    local = now.astimezone(KST)
    if local.weekday() < 5 and local.time() < ROTATION_SAFE_TIME:
        raise RotationError(
            "release rotation is blocked before 16:00 KST on a weekday; "
            "close and reconcile the current session first"
        )


def run(
    config: BootstrapConfig,
    *,
    region: str,
    check: bool,
    now: datetime | None = None,
) -> dict[str, object]:
    _validate_config(config)
    scheduler = boto3.client("scheduler", region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(config.table_name)
    start = scheduler.get_schedule(Name=START_NAME, GroupName=GROUP_NAME)
    stop = scheduler.get_schedule(Name=STOP_NAME, GroupName=GROUP_NAME)
    start_arn = _validate_schedule(
        start, name=START_NAME, phase="start", generation=config.generation
    )
    stop_arn = _validate_schedule(
        stop, name=STOP_NAME, phase="stop", generation=config.generation
    )
    state = _prepare_rotation(table, config, start_arn, stop_arn)
    before = {"start": str(start["State"]), "stop": str(stop["State"])}
    if before != {"start": "ENABLED", "stop": "ENABLED"}:
        raise RotationError("both C* schedules must be ENABLED before rotation")
    if check:
        return {
            "mode": "check",
            "old_release_id": state.old_release_id,
            "new_release_id": state.new_release_id,
            "new_release_exists": state.new_release_exists,
            "schedule_state": before,
        }
    _assert_safe_rotation_time(now or datetime.now(tz=KST))
    scheduler.update_schedule(**_schedule_update_args(start, state="DISABLED"))
    scheduler.update_schedule(**_schedule_update_args(stop, state="DISABLED"))
    try:
        table.meta.client.transact_write_items(
            TransactItems=_rotation_transactions(config.table_name, state)
        )
        pointer = _read(table, {"PK": "CONTROL#CSTAR", "SK": "RELEASE"})
        if pointer is None or pointer.get("release_id") != state.new_release_id:
            raise RotationError("active release pointer read-back failed")
        scheduler.update_schedule(**_schedule_update_args(start, state="ENABLED"))
        scheduler.update_schedule(**_schedule_update_args(stop, state="ENABLED"))
    except Exception:
        # Keep schedules disabled if any mutation or read-back fails.
        raise
    after = {
        "start": str(scheduler.get_schedule(Name=START_NAME, GroupName=GROUP_NAME)["State"]),
        "stop": str(scheduler.get_schedule(Name=STOP_NAME, GroupName=GROUP_NAME)["State"]),
    }
    if after != {"start": "ENABLED", "stop": "ENABLED"}:
        raise RotationError("schedule enable read-back failed")
    return {
        "mode": "apply",
        "old_release_id": state.old_release_id,
        "new_release_id": state.new_release_id,
        "schedule_state_before": before,
        "schedule_state_after": after,
    }


def _config_from_args(args: argparse.Namespace) -> BootstrapConfig:
    return BootstrapConfig(
        table_name=args.table_name,
        generation=args.generation,
        protocol_sha256=args.protocol_sha256,
        source_sha=args.source_sha,
        image_digest=args.image_digest,
        compose_shadow_sha256=args.compose_shadow_sha256,
        worker_sha256=args.worker_sha256,
        validator_sha256=args.validator_sha256,
        shadow_document_sha256=args.shadow_document_sha256,
        rollout_attempt_id=args.rollout_attempt_id,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rotate the active C* release with an atomic pointer update."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true", help="read-only preflight")
    action.add_argument("--apply", action="store_true", help="apply the rotation")
    parser.add_argument("--region", default=REGION)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--generation", required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--compose-shadow-sha256", required=True)
    parser.add_argument("--worker-sha256", required=True)
    parser.add_argument("--validator-sha256", required=True)
    parser.add_argument("--shadow-document-sha256", required=True)
    parser.add_argument("--rollout-attempt-id", required=True)
    parser.add_argument(
        "--repository-root",
        default=str(Path(__file__).resolve().parents[1]),
    )
    args = parser.parse_args()
    try:
        config = _config_from_args(args)
        _validate_config(config)
        verify_source_artifacts(config, Path(args.repository_root))
        result = run(
            config,
            region=args.region,
            check=args.check,
        )
    except (RotationError, ValueError) as error:
        print(f"C* release rotation failed: {error}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
