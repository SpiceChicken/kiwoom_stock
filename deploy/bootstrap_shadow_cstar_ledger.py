#!/usr/bin/env python3
"""Safely seed the C* release ledger before enabling market-day schedules."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import re
from typing import Any, Mapping

try:
    import boto3
    from boto3.dynamodb.types import TypeSerializer
    from botocore.exceptions import BotoCoreError
except ImportError as error:  # pragma: no cover - operational dependency
    raise SystemExit("boto3 is required") from error

try:
    from deploy.shadow_cstar_contract import (
        make_release_intent,
        release_id_for,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    from shadow_cstar_contract import (  # type: ignore[no-redef]
        make_release_intent,
        release_id_for,
    )


REGION = "ap-northeast-2"
GROUP_NAME = "kiwoom-shadow-cstar"
START_NAME = "kiwoom-shadow-cstar-start"
STOP_NAME = "kiwoom-shadow-cstar-stop"
GENERATION_RE = re.compile(r"^cstar-g[0-9]{6,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
ROLLOUT_RE = re.compile(r"^[1-9][0-9]{0,19}$")


class BootstrapError(ValueError):
    """A fail-closed ledger or schedule boundary error."""


@dataclass(frozen=True)
class BootstrapConfig:
    table_name: str
    generation: str
    protocol_sha256: str
    source_sha: str
    image_digest: str
    compose_shadow_sha256: str
    worker_sha256: str
    validator_sha256: str
    shadow_document_sha256: str
    rollout_attempt_id: str


def _required_hex(value: str, *, label: str) -> str:
    if SHA256_RE.fullmatch(value) is None:
        raise BootstrapError(f"{label} invalid")
    return value


def _release(config: BootstrapConfig) -> dict[str, str]:
    value = make_release_intent({
        "image_digest": config.image_digest,
        "source_sha": config.source_sha,
        "compose_shadow_sha256": config.compose_shadow_sha256,
        "worker_sha256": config.worker_sha256,
        "validator_sha256": config.validator_sha256,
        "shadow_document_sha256": config.shadow_document_sha256,
        "rollout_attempt_id": config.rollout_attempt_id,
    })
    return value


def _generation(config: BootstrapConfig, start_arn: str, stop_arn: str) -> dict[str, object]:
    return {
        "PK": f"GEN#{config.generation}",
        "SK": "META",
        "schedule_generation": config.generation,
        "protocol_version": 1,
        "protocol_sha256": config.protocol_sha256,
        "schedule_arns": {"start": start_arn, "stop": stop_arn},
    }


def _release_item(config: BootstrapConfig, release: Mapping[str, str]) -> dict[str, object]:
    release_id = release_id_for(release)
    return {
        "PK": f"RELEASE#{release_id}",
        "SK": "META",
        **release,
    }


def _active_pointer(release_id: str) -> dict[str, str]:
    return {
        "PK": "CONTROL#CSTAR",
        "SK": "RELEASE",
        "release_id": release_id,
        "state": "ACTIVE",
    }


def _validate_config(config: BootstrapConfig) -> None:
    if not config.table_name:
        raise BootstrapError("table name missing")
    if GENERATION_RE.fullmatch(config.generation) is None:
        raise BootstrapError("generation invalid")
    if SOURCE_SHA_RE.fullmatch(config.source_sha) is None:
        raise BootstrapError("source sha invalid")
    for label, value in (
        ("protocol sha256", config.protocol_sha256),
        ("compose shadow sha256", config.compose_shadow_sha256),
        ("worker sha256", config.worker_sha256),
        ("validator sha256", config.validator_sha256),
        ("shadow document sha256", config.shadow_document_sha256),
    ):
        _required_hex(value, label=label)
    if not config.image_digest.startswith("ghcr.io/spicechicken/kiwoom_stock@sha256:"):
        raise BootstrapError("image digest invalid")
    _required_hex(config.image_digest.rsplit(":", 1)[-1], label="image digest")
    if ROLLOUT_RE.fullmatch(config.rollout_attempt_id) is None:
        raise BootstrapError("rollout attempt id invalid")


def _schedule_update_args(schedule: Mapping[str, object], *, state: str) -> dict[str, object]:
    required = (
        "Name",
        "GroupName",
        "ScheduleExpression",
        "FlexibleTimeWindow",
        "Target",
    )
    if any(key not in schedule for key in required):
        raise BootstrapError("schedule read-back incomplete")
    result: dict[str, object] = {
        key: schedule[key] for key in required
    }
    result["State"] = state
    for key in ("ScheduleExpressionTimezone", "Description", "StartDate", "EndDate", "KmsKeyArn"):
        if key in schedule:
            result[key] = schedule[key]
    return result


def _validate_schedule(
    schedule: Mapping[str, object],
    *,
    name: str,
    phase: str,
    generation: str,
) -> str:
    if (
        schedule.get("Name") != name
        or schedule.get("GroupName") != GROUP_NAME
        or schedule.get("ScheduleExpressionTimezone") != "Asia/Seoul"
        or schedule.get("FlexibleTimeWindow") != {"Mode": "OFF"}
    ):
        raise BootstrapError(f"{phase} schedule shape invalid")
    expected_expression = "cron(50 8 ? * MON-FRI *)" if phase == "start" else "cron(35 15 ? * MON-FRI *)"
    if schedule.get("ScheduleExpression") != expected_expression:
        raise BootstrapError(f"{phase} schedule expression invalid")
    target = schedule.get("Target")
    if not isinstance(target, Mapping) or not isinstance(target.get("Input"), str):
        raise BootstrapError(f"{phase} target invalid")
    try:
        payload = json.loads(target["Input"])
    except (TypeError, json.JSONDecodeError):
        raise BootstrapError(f"{phase} target input invalid") from None
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema_version") != 1
        or payload.get("phase") != phase
        or payload.get("schedule_generation") != generation
    ):
        raise BootstrapError(f"{phase} target contract invalid")
    arn = schedule.get("Arn")
    if not isinstance(arn, str) or not arn:
        raise BootstrapError(f"{phase} schedule arn missing")
    return arn


def _wire_item(value: Mapping[str, object]) -> dict[str, object]:
    serializer = TypeSerializer()
    return {key: serializer.serialize(item) for key, item in value.items()}


def _read(table: Any, key: Mapping[str, str]) -> dict[str, object] | None:
    response = table.get_item(Key=dict(key), ConsistentRead=True)
    item = response.get("Item") if isinstance(response, Mapping) else None
    return dict(item) if isinstance(item, Mapping) else None


def _selected(value: Mapping[str, object], keys: set[str]) -> dict[str, object]:
    return {key: value.get(key) for key in keys}


def _seed_ledger(table: Any, config: BootstrapConfig, start_arn: str, stop_arn: str) -> str:
    release = _release(config)
    release_id = release_id_for(release)
    generation = _generation(config, start_arn, stop_arn)
    release_item = _release_item(config, release)
    pointer = _active_pointer(release_id)
    generation_key = {"PK": generation["PK"], "SK": generation["SK"]}
    release_key = {"PK": release_item["PK"], "SK": release_item["SK"]}
    pointer_key = {"PK": pointer["PK"], "SK": pointer["SK"]}
    existing_generation = _read(table, generation_key)
    existing_release = _read(table, release_key)
    existing_pointer = _read(table, pointer_key)
    if existing_generation is not None and _selected(
        existing_generation,
        {"schedule_generation", "protocol_version", "protocol_sha256", "schedule_arns"},
    ) != _selected(
        generation,
        {"schedule_generation", "protocol_version", "protocol_sha256", "schedule_arns"},
    ):
        raise BootstrapError("existing generation mismatch")
    if existing_release is not None and _selected(
        existing_release, set(release)
    ) != _selected(release_item, set(release)):
        raise BootstrapError("existing release mismatch")
    if existing_pointer is not None and _selected(
        existing_pointer, {"release_id", "state"}
    ) != _selected(pointer, {"release_id", "state"}):
        raise BootstrapError("active release pointer mismatch")
    puts: list[dict[str, object]] = []
    for existing, item in (
        (existing_generation, generation),
        (existing_release, release_item),
        (existing_pointer, pointer),
    ):
        if existing is None:
            puts.append({
                "Put": {
                    "TableName": config.table_name,
                    "Item": _wire_item(item),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            })
    if puts:
        table.meta.client.transact_write_items(TransactItems=puts)
    return release_id


def _verify_ledger(table: Any, config: BootstrapConfig, start_arn: str, stop_arn: str) -> str:
    release = _release(config)
    release_id = release_id_for(release)
    expected = (
        _generation(config, start_arn, stop_arn),
        _release_item(config, release),
        _active_pointer(release_id),
    )
    for item in expected:
        actual = _read(table, {"PK": str(item["PK"]), "SK": str(item["SK"])})
        if actual is None:
            raise BootstrapError(f"ledger item missing: {item['PK']}")
        for key, value in item.items():
            if key in {"PK", "SK"}:
                continue
            if actual.get(key) != value:
                raise BootstrapError(f"ledger item mismatch: {item['PK']}:{key}")
    return release_id


def _schedule_state(client: Any, name: str) -> str:
    schedule = client.get_schedule(Name=name, GroupName=GROUP_NAME)
    state = schedule.get("State")
    if state not in {"ENABLED", "DISABLED"}:
        raise BootstrapError(f"schedule state invalid: {name}")
    return str(state)


def run(config: BootstrapConfig, *, region: str, check: bool) -> dict[str, object]:
    _validate_config(config)
    scheduler = boto3.client("scheduler", region_name=region)
    table = boto3.resource("dynamodb", region_name=region).Table(config.table_name)
    start = scheduler.get_schedule(Name=START_NAME, GroupName=GROUP_NAME)
    stop = scheduler.get_schedule(Name=STOP_NAME, GroupName=GROUP_NAME)
    start_arn = _validate_schedule(start, name=START_NAME, phase="start", generation=config.generation)
    stop_arn = _validate_schedule(stop, name=STOP_NAME, phase="stop", generation=config.generation)
    before = {"start": str(start["State"]), "stop": str(stop["State"])}
    release_id = release_id_for(_release(config))
    if check:
        _verify_ledger(table, config, start_arn, stop_arn)
        return {
            "mode": "check",
            "release_id": release_id,
            "schedule_state": before,
            "ledger": "ready",
        }
    if before["start"] == "ENABLED":
        scheduler.update_schedule(**_schedule_update_args(start, state="DISABLED"))
    if before["stop"] == "ENABLED":
        scheduler.update_schedule(**_schedule_update_args(stop, state="DISABLED"))
    try:
        _seed_ledger(table, config, start_arn, stop_arn)
        _verify_ledger(table, config, start_arn, stop_arn)
        scheduler.update_schedule(**_schedule_update_args(start, state="ENABLED"))
        scheduler.update_schedule(**_schedule_update_args(stop, state="ENABLED"))
    except Exception:
        # The safe failure state is disabled schedules with the validated ledger.
        raise
    after = {
        "start": _schedule_state(scheduler, START_NAME),
        "stop": _schedule_state(scheduler, STOP_NAME),
    }
    if after != {"start": "ENABLED", "stop": "ENABLED"}:
        raise BootstrapError("schedule enable read-back failed")
    return {
        "mode": "apply",
        "release_id": release_id,
        "schedule_state_before": before,
        "schedule_state_after": after,
        "ledger": "ready",
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
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
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(_config_from_args(args), region=args.region, check=args.check)
    except (BootstrapError, BotoCoreError, OSError, ValueError) as error:
        print(f"shadow C* ledger bootstrap failed: {error}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
