#!/usr/bin/env python3
"""Build one bounded observation of a scheduled shadow workflow start."""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
from typing import Mapping


MAX_RUN_INPUT_BYTES = 4_096
MAX_OBSERVATION_BYTES = 4_096
MAX_DELAY_SECONDS = 86_399
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
UTC_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
RUN_KEYS = {
    "event", "id", "created_at", "run_started_at", "head_branch",
}
OBSERVATION_KEYS = {
    "schema_version",
    "run_id",
    "cron",
    "desired_state",
    "expected_at_utc",
    "created_at_utc",
    "run_started_at_utc",
    "delivery_delay_seconds",
    "queue_delay_seconds",
    "total_start_delay_seconds",
}
CRON_CONTRACT = {
    "50 23 * * 0-4": {
        "desired_state": "continuous",
        "hour": 23,
        "minute": 50,
        "weekdays": {6, 0, 1, 2, 3},
    },
    "35 6 * * 1-5": {
        "desired_state": "stop",
        "hour": 6,
        "minute": 35,
        "weekdays": {0, 1, 2, 3, 4},
    },
}


class ObservationError(ValueError):
    """A value-free schedule observation rejection."""


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ObservationError("invalid")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> object:
    raise ObservationError("invalid")


def _load_json(path: Path, *, maximum_bytes: int) -> Mapping[str, object]:
    try:
        if not path.is_file() or path.is_symlink():
            raise ObservationError("invalid")
        raw = path.read_bytes()
    except OSError:
        raise ObservationError("invalid") from None
    if len(raw) > maximum_bytes:
        raise ObservationError("invalid")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise ObservationError("invalid") from None
    if not isinstance(value, Mapping):
        raise ObservationError("invalid")
    return value


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise ObservationError("invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ObservationError("invalid") from None
    return parsed.replace(tzinfo=timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_occurrence(created_at: datetime, cron: str) -> datetime:
    contract = CRON_CONTRACT.get(cron)
    if contract is None:
        raise ObservationError("invalid")
    candidates: list[datetime] = []
    for offset in range(8):
        day = created_at.date() - timedelta(days=offset)
        candidate = datetime(
            day.year,
            day.month,
            day.day,
            int(contract["hour"]),
            int(contract["minute"]),
            tzinfo=timezone.utc,
        )
        if (
            candidate.weekday() in contract["weekdays"]
            and candidate <= created_at
        ):
            candidates.append(candidate)
    if not candidates:
        raise ObservationError("invalid")
    expected = max(candidates)
    delay = int((created_at - expected).total_seconds())
    if delay < 0 or delay > MAX_DELAY_SECONDS:
        raise ObservationError("invalid")
    return expected


def _bounded_delay(value: float) -> int:
    if not value.is_integer():
        raise ObservationError("invalid")
    result = int(value)
    if result < 0 or result > MAX_DELAY_SECONDS:
        raise ObservationError("invalid")
    return result


def _is_bounded_integer(value: object) -> bool:
    return (
        type(value) is int
        and 0 <= value <= MAX_DELAY_SECONDS
    )


def build_observation(
    run: Mapping[str, object],
    *,
    run_id: str,
    cron: str,
    desired_state: str,
) -> dict[str, object]:
    if (
        set(run) != RUN_KEYS
        or RUN_ID_RE.fullmatch(run_id) is None
        or run.get("event") != "schedule"
        or type(run.get("id")) is not int
        or str(run.get("id")) != run_id
        or run.get("head_branch") != "main"
    ):
        raise ObservationError("invalid")
    contract = CRON_CONTRACT.get(cron)
    if contract is None or contract["desired_state"] != desired_state:
        raise ObservationError("invalid")
    created_at = _parse_utc(run.get("created_at"))
    started_at = _parse_utc(run.get("run_started_at"))
    expected_at = _expected_occurrence(created_at, cron)
    delivery_delay = _bounded_delay(
        (created_at - expected_at).total_seconds()
    )
    queue_delay = _bounded_delay((started_at - created_at).total_seconds())
    total_delay = _bounded_delay((started_at - expected_at).total_seconds())
    if delivery_delay + queue_delay != total_delay:
        raise ObservationError("invalid")
    return {
        "schema_version": 1,
        "run_id": run_id,
        "cron": cron,
        "desired_state": desired_state,
        "expected_at_utc": _format_utc(expected_at),
        "created_at_utc": _format_utc(created_at),
        "run_started_at_utc": _format_utc(started_at),
        "delivery_delay_seconds": delivery_delay,
        "queue_delay_seconds": queue_delay,
        "total_start_delay_seconds": total_delay,
    }


def validate_observation(
    value: Mapping[str, object],
    *,
    desired_state: str | None = None,
    expected_run_id: str | None = None,
    expected_cron: str | None = None,
) -> dict[str, object]:
    if (
        set(value) != OBSERVATION_KEYS
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
        or not isinstance(value.get("run_id"), str)
        or RUN_ID_RE.fullmatch(str(value["run_id"])) is None
        or not isinstance(value.get("cron"), str)
        or not isinstance(value.get("desired_state"), str)
        or any(
            not _is_bounded_integer(value.get(key))
            for key in (
                "delivery_delay_seconds",
                "queue_delay_seconds",
                "total_start_delay_seconds",
            )
        )
        or (
            desired_state is not None
            and value.get("desired_state") != desired_state
        )
        or (
            expected_run_id is not None
            and (
                RUN_ID_RE.fullmatch(expected_run_id) is None
                or value.get("run_id") != expected_run_id
            )
        )
        or (
            expected_cron is not None
            and (
                expected_cron not in CRON_CONTRACT
                or value.get("cron") != expected_cron
            )
        )
    ):
        raise ObservationError("invalid")
    expected = _parse_utc(value.get("expected_at_utc"))
    created = _parse_utc(value.get("created_at_utc"))
    started = _parse_utc(value.get("run_started_at_utc"))
    rebuilt = build_observation(
        {
            "event": "schedule",
            "id": int(str(value["run_id"])),
            "created_at": _format_utc(created),
            "run_started_at": _format_utc(started),
            "head_branch": "main",
        },
        run_id=str(value["run_id"]),
        cron=str(value["cron"]),
        desired_state=str(value["desired_state"]),
    )
    if rebuilt != dict(value) or expected != _parse_utc(
        rebuilt["expected_at_utc"]
    ):
        raise ObservationError("invalid")
    return rebuilt


def load_observation(
    path: Path,
    *,
    desired_state: str | None = None,
    expected_run_id: str | None = None,
    expected_cron: str | None = None,
) -> dict[str, object]:
    value = _load_json(path, maximum_bytes=MAX_OBSERVATION_BYTES)
    return validate_observation(
        value,
        desired_state=desired_state,
        expected_run_id=expected_run_id,
        expected_cron=expected_cron,
    )


def render_summary(value: Mapping[str, object]) -> str:
    observation = validate_observation(value)
    return (
        "### Shadow schedule observation\n\n"
        f"- action: `{observation['desired_state']}`\n"
        f"- expected UTC: `{observation['expected_at_utc']}`\n"
        "- total start delay: "
        f"`{observation['total_start_delay_seconds']}s`\n"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cron", required=True)
    parser.add_argument(
        "--desired-state", required=True, choices=("continuous", "stop")
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run = _load_json(args.run_json, maximum_bytes=MAX_RUN_INPUT_BYTES)
        observation = build_observation(
            run,
            run_id=args.run_id,
            cron=args.cron,
            desired_state=args.desired_state,
        )
        artifact = (
            json.dumps(observation, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        summary = render_summary(observation)
        if len(artifact.encode("utf-8")) > MAX_OBSERVATION_BYTES:
            raise ObservationError("invalid")
        if args.output.is_symlink() or args.summary.is_symlink():
            raise ObservationError("invalid")
        args.output.write_text(artifact, encoding="utf-8")
        with args.summary.open("a", encoding="utf-8") as stream:
            stream.write(summary)
    except (ObservationError, OSError, UnicodeError, ValueError):
        try:
            if args.output.is_file() and not args.output.is_symlink():
                args.output.unlink()
        except OSError:
            pass
        print("shadow schedule observation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
