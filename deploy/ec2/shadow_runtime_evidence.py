#!/usr/bin/env python3
"""Validate bounded shadow runtime evidence without project dependencies."""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
import math
import re
import sys
from typing import Iterable, TextIO


MAX_INPUT_BYTES = 1_048_576
MAX_LINE_BYTES = 65_536
MAX_LINES = 4_096
MAX_RECORDS = 256
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_RE = re.compile(
    r"ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}"
)
ACTIVATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SIDE_EFFECT_KEYS = (
    "broker_orders", "account", "oauth_revoke", "slack", "gemini", "s3",
    "reports",
)
EXPECTED_API_COUNTS = {
    "token": 1,
    "stock_basic": 1,
    "stock_chart_5m": 1,
    "proxy_chart_60m": 1,
    "stock_strength": 1,
    "stock_orderbook": 1,
}
EXPECTED_LOCAL_KEYS = {"status", "paper_buy", "paper_sell", "error", "critical"}
ONESHOT_KEYS = {
    "schema_version", "status", "mode", "kst_date", "calendar", "source_sha",
    "image_digest", "activation_id", "stock_code", "proxy_code", "cycles",
    "http_attempts", "api_counts", "db_identity", "resources_closed",
    "side_effects", "local_counts", "continuity", "decision_telemetry",
}
CYCLE_KEYS = ONESHOT_KEYS | {
    "event", "cycle_index", "elapsed_seconds", "interval_seconds",
    "cycle_start_elapsed_seconds", "observed_interval_seconds", "db_reopened",
    "db_reopens",
}
TERMINAL_KEYS = {
    "schema_version", "event", "status", "mode", "source_sha", "image_digest",
    "activation_id", "cycles", "elapsed_seconds",
    "first_cycle_start_elapsed_seconds", "second_cycle_start_elapsed_seconds",
    "second_cycle_interval_seconds", "minimum_cycle_interval_seconds",
    "db_reopens", "resources_closed", "side_effects", "reason",
}
TERMINAL_OPTIONAL_KEYS = {"error_type"}
DECISION_TELEMETRY_KEYS = {
    "market_regime", "strategy_reason_code", "strategy_intent", "paper_action",
    "position_before", "trading_window", "session_phase", "net_force_band",
    "current_velocity_band", "jerk_band", "strength_band", "trend_rsi_band",
    "price_vwap_relation",
}
DIAGNOSTIC_TERMINAL_OUTCOMES = {
    ("FAILED", "failure"),
    ("FAILED", "shutdown-deadline"),
    ("FAILED", "stop-requested"),
    ("FAILED", "run-deadline"),
    ("CLOSED", "calendar-closed"),
}


class EvidenceError(ValueError):
    """A stable, operator-safe evidence rejection category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _reject_json_constant(_value: str) -> None:
    raise EvidenceError("json_non_finite")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceError("json_duplicate_key")
        result[key] = value
    return result


def _strict_json_loads(value: str, category: str) -> object:
    try:
        return json.loads(
            value, parse_constant=_reject_json_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except EvidenceError:
        raise
    except json.JSONDecodeError as error:
        raise EvidenceError(category) from error


def _bounded_read(stream: TextIO) -> str:
    try:
        value = stream.read(MAX_INPUT_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise EvidenceError("input_unavailable") from error
    if len(value.encode("utf-8")) > MAX_INPUT_BYTES:
        raise EvidenceError("input_too_large")
    return value


def _json_lines(content: str) -> Iterable[object]:
    lines = content.splitlines()
    if len(lines) > MAX_LINES:
        raise EvidenceError("input_too_many_lines")
    records = 0
    for raw in lines:
        if len(raw.encode("utf-8")) > MAX_LINE_BYTES:
            raise EvidenceError("input_line_too_large")
        line = raw.split("|", 1)[-1].strip()
        if not line.startswith(("{", "[", '"')) and line not in {
            "null", "true", "false",
        } and not re.match(r"^-?[0-9]", line):
            continue
        value = _strict_json_loads(line, "record_json_invalid")
        records += 1
        if records > MAX_RECORDS:
            raise EvidenceError("input_too_many_records")
        if not isinstance(value, dict):
            raise EvidenceError("record_not_object")
        yield value


def _records(
    content: str, input_format: str,
) -> tuple[Iterable[object], dict[str, object] | None]:
    if input_format == "json-lines":
        return _json_lines(content), None
    invocation = _strict_json_loads(content, "invocation_json_invalid")
    if not isinstance(invocation, dict):
        raise EvidenceError("invocation_not_object")
    status = invocation.get("Status")
    if type(status) is not str or status != "Success":
        raise EvidenceError("invocation_status_invalid")
    response_code = invocation.get("ResponseCode")
    if type(response_code) is not int or response_code != 0:
        raise EvidenceError("invocation_response_code_invalid")
    stdout = invocation.get("StandardOutputContent")
    if not isinstance(stdout, str):
        raise EvidenceError("invocation_stdout_invalid")
    if len(stdout.encode("utf-8")) > MAX_INPUT_BYTES:
        raise EvidenceError("invocation_stdout_too_large")
    return _json_lines(stdout), {
        "ssm_status": status,
        "ssm_response_code": response_code,
    }


def _aware_iso_or_none(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _valid_continuity(value: object) -> bool:
    expected = {
        "schema_version", "hydration_source", "previous_observed_at",
        "history_depth", "baseline_source", "baseline_sample_index",
        "baseline_time_estimated",
    }
    return (
        isinstance(value, dict)
        and set(value) == expected
        and type(value.get("schema_version")) is int
        and value.get("schema_version") == 1
        and value.get("hydration_source") in {
            "initial", "legacy_cold_start", "persisted",
        }
        and _aware_iso_or_none(value.get("previous_observed_at"))
        and type(value.get("history_depth")) is int
        and value.get("history_depth", -1) >= 0
        and value.get("baseline_source") == "row_4_fixed_cadence"
        and type(value.get("baseline_sample_index")) is int
        and value.get("baseline_sample_index") == 4
        and value.get("baseline_time_estimated") is True
    )


def _valid_decision_consistency(value: dict[str, object]) -> bool:
    reason = value.get("strategy_reason_code")
    intent = value.get("strategy_intent")
    entry_reasons = {
        "BREAKOUT_OVERRIDE", "UPTREND_ENTRY", "REVERSAL_ENTRY",
    }
    if (reason in entry_reasons) != (intent == "ENTRY_SIGNAL"):
        return False
    if intent == "ENTRY_SIGNAL" and value.get("jerk_band") != "POSITIVE":
        return False
    if (
        reason in {"UPTREND_ENTRY", "REVERSAL_ENTRY"}
        and value.get("net_force_band") in {"STRONG_NEGATIVE", "NEGATIVE"}
    ):
        return False
    if (
        reason == "NET_FORCE_NEGATIVE"
        and value.get("net_force_band") not in {"STRONG_NEGATIVE", "NEGATIVE"}
    ):
        return False
    if reason == "JERK_NON_POSITIVE" and value.get("jerk_band") == "POSITIVE":
        return False
    if value.get("paper_action") == "BUY" and (
        value.get("position_before") != "FLAT"
        or intent != "ENTRY_SIGNAL"
        or value.get("trading_window") != "OPEN"
    ):
        return False
    if (value.get("trading_window") == "OPEN") != (
        value.get("session_phase") == "ENTRY"
    ):
        return False
    return not (
        value.get("paper_action") == "SELL"
        and value.get("position_before") != "OPEN"
    )


def _valid_decision_telemetry(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == DECISION_TELEMETRY_KEYS
        and value.get("market_regime") in {
            "STABLE_BULL", "VOLATILE_BULL", "QUIET_BEAR", "PANIC_BEAR",
            "NEUTRAL",
        }
        and value.get("strategy_intent") in {
            "ENTRY_SIGNAL", "NO_ENTRY_SIGNAL",
        }
        and value.get("strategy_reason_code") in {
            "VI_WAIT", "CLIMAX_SHIELD", "BREAKOUT_OVERRIDE", "THRUST_LOW",
            "NET_FORCE_NEGATIVE", "STALL_SHIELD", "LOW_QUALITY_TREND",
            "VOLUME_EXHAUSTED", "UPTREND_ENTRY", "REVERSAL_ENTRY",
            "WARMING_UP", "JERK_NON_POSITIVE",
        }
        and value.get("paper_action") in {"BUY", "SELL", "HOLD"}
        and value.get("position_before") in {"FLAT", "OPEN", "OVERNIGHT"}
        and value.get("trading_window") in {"OPEN", "CLOSED"}
        and value.get("session_phase") in {"ENTRY", "EXIT_ONLY", "CLOSED"}
        and value.get("net_force_band") in {
            "STRONG_NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE",
            "STRONG_POSITIVE",
        }
        and value.get("current_velocity_band") in {
            "STRONG_NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE",
            "STRONG_POSITIVE",
        }
        and value.get("jerk_band") in {"NEGATIVE", "NEUTRAL", "POSITIVE"}
        and value.get("strength_band") in {
            "BELOW_100", "AT_100", "ABOVE_100",
        }
        and value.get("trend_rsi_band") in {
            "OVERSOLD", "NEUTRAL", "OVERBOUGHT",
        }
        and value.get("price_vwap_relation") in {"BELOW", "AT", "ABOVE"}
        and _valid_decision_consistency(value)
    )


def _valid_local_counts(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == EXPECTED_LOCAL_KEYS
        and all(type(item) is int for item in value.values())
        and value["status"] == 1
        and value["error"] == 0
        and value["critical"] == 0
        and value["paper_buy"] in (0, 1)
        and value["paper_sell"] in (0, 1)
        and value["paper_buy"] + value["paper_sell"] <= 1
    )


def _valid_api_counts(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == set(EXPECTED_API_COUNTS)
        and all(type(item) is int for item in value.values())
        and value == EXPECTED_API_COUNTS
    )


def _valid_date(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _finite_float(value: object, *, minimum: float = 0.0) -> bool:
    return type(value) is float and math.isfinite(value) and value >= minimum


def _exact_keys(item: dict[str, object], expected: set[str], category: str) -> None:
    if set(item) != expected:
        raise EvidenceError(category)


def _validate_oneshot(item: dict[str, object]) -> None:
    _exact_keys(item, ONESHOT_KEYS, "oneshot_keys_invalid")
    attempts = item.get("http_attempts")
    counts = item.get("api_counts")
    local_counts = item.get("local_counts")
    common = (
        type(item.get("schema_version")) is int
        and item.get("schema_version") == 2
        and item.get("stock_code") == "005930"
        and item.get("proxy_code") == "069500"
        and _valid_date(item.get("kst_date"))
    )
    passed = (
        item.get("status") == "PASS"
        and item.get("calendar") == "OPEN"
        and type(item.get("cycles")) is int
        and item.get("cycles") == 1
        and type(attempts) is int
        and attempts == 6
        and item.get("db_identity") == "/var/lib/kiwoom/shadow-trades.db"
        and _valid_api_counts(counts)
        and _valid_local_counts(local_counts)
        and _valid_continuity(item.get("continuity"))
        and _valid_decision_telemetry(item.get("decision_telemetry"))
    )
    closed = (
        item.get("status") == "CLOSED"
        and item.get("calendar") == "CLOSED"
        and type(item.get("cycles")) is int
        and item.get("cycles") == 0
        and type(attempts) is int
        and attempts == 0
        and counts == {}
        and local_counts == {}
        and item.get("db_identity") is None
        and item.get("continuity") is None
        and item.get("decision_telemetry") is None
    )
    if not common or not (passed or closed):
        raise EvidenceError("oneshot_contract_invalid")


def _validate_cycle(item: dict[str, object]) -> None:
    _exact_keys(item, CYCLE_KEYS, "cycle_keys_invalid")
    integer_fields = (
        item.get("schema_version"), item.get("cycle_index"), item.get("cycles"),
        item.get("http_attempts"), item.get("db_reopens"),
    )
    if (
        any(type(value) is not int for value in integer_fields)
        or item.get("schema_version") != 3
        or item.get("event") != "cycle"
        or item.get("status") != "PASS"
        or item.get("cycle_index") != 1
        or item.get("cycles") != 1
        or item.get("http_attempts") != 6
        or item.get("db_identity") != "/var/lib/kiwoom/shadow-trades.db"
        or not _valid_date(item.get("kst_date"))
        or not _valid_api_counts(item.get("api_counts"))
        or not _valid_local_counts(item.get("local_counts"))
        or item.get("interval_seconds") != 60.0
        or not _finite_float(item.get("elapsed_seconds"))
        or not _finite_float(item.get("cycle_start_elapsed_seconds"))
        or item.get("observed_interval_seconds") is not None
        or item.get("db_reopened") is not False
        or item.get("db_reopens") != 0
        or not _valid_continuity(item.get("continuity"))
        or not _valid_decision_telemetry(item.get("decision_telemetry"))
    ):
        raise EvidenceError("cycle_contract_invalid")


def _validate_terminal_shape(item: dict[str, object]) -> None:
    keys = set(item)
    if keys not in (TERMINAL_KEYS, TERMINAL_KEYS | TERMINAL_OPTIONAL_KEYS):
        raise EvidenceError("terminal_keys_invalid")
    error_type = item.get("error_type")
    if "error_type" in item and (
        not isinstance(error_type, str)
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", error_type) is None
    ):
        raise EvidenceError("terminal_error_type_invalid")


def _validate_terminal_timing(
    item: dict[str, object], *, allow_zero_cycles: bool = False
) -> None:
    cycles = item.get("cycles")
    reopens = item.get("db_reopens")
    first = item.get("first_cycle_start_elapsed_seconds")
    second = item.get("second_cycle_start_elapsed_seconds")
    interval = item.get("second_cycle_interval_seconds")
    minimum = item.get("minimum_cycle_interval_seconds")
    minimum_cycles = 0 if allow_zero_cycles else 1
    if (
        not _finite_float(item.get("elapsed_seconds"))
        or type(cycles) is not int or cycles < minimum_cycles
        or type(reopens) is not int or reopens != max(cycles - 1, 0)
    ):
        raise EvidenceError("terminal_timing_invalid")
    if cycles == 0:
        if any(value is not None for value in (first, second, interval, minimum)):
            raise EvidenceError("terminal_zero_cycle_invalid")
        return
    if not _finite_float(first):
        raise EvidenceError("terminal_timing_invalid")
    if cycles == 1:
        if any(value is not None for value in (second, interval, minimum)):
            raise EvidenceError("terminal_single_cycle_invalid")
    elif (
        not _finite_float(second, minimum=60.0) or second - first < 60.0
        or not _finite_float(interval, minimum=60.0)
        or abs((second - first) - interval) > 0.000001
        or not _finite_float(minimum, minimum=60.0)
    ):
        raise EvidenceError("terminal_multi_cycle_invalid")


def _validate_terminal(item: dict[str, object]) -> None:
    _validate_terminal_shape(item)
    if (
        item.get("schema_version") != 3
        or (item.get("status"), item.get("reason")) not in {
            ("STOPPED", "stop-requested"), ("DEADLINE", "run-deadline"),
        }
    ):
        raise EvidenceError("terminal_contract_invalid")
    _validate_terminal_timing(item)


def _validate_diagnostic_terminal(item: dict[str, object]) -> None:
    """Validate a redacted terminal that is not proof of successful operation."""

    _validate_terminal_shape(item)
    outcome = (item.get("status"), item.get("reason"))
    if item.get("schema_version") != 3 or outcome not in DIAGNOSTIC_TERMINAL_OUTCOMES:
        raise EvidenceError("diagnostic_terminal_contract_invalid")
    if type(item.get("resources_closed")) is not bool:
        raise EvidenceError("diagnostic_terminal_resources_invalid")
    _validate_terminal_timing(item, allow_zero_cycles=True)


def validate(
    records: Iterable[object], *, mode: str, event: str, source_sha: str,
    image_digest: str, activation_id: str,
) -> dict[str, object]:
    matches: list[dict[str, object]] = []
    for value in records:
        if not isinstance(value, dict):
            raise EvidenceError("record_not_object")
        event_matches = (
            event == "oneshot" and "event" not in value
        ) or value.get("event") == event
        if value.get("mode") == mode and event_matches:
            matches.append(value)
    if not matches:
        raise EvidenceError("record_not_found")
    item = matches[-1]
    if (
        item.get("source_sha") != source_sha
        or item.get("image_digest") != image_digest
        or item.get("activation_id") != activation_id
    ):
        raise EvidenceError("activation_tuple_mismatch")
    side_effects = item.get("side_effects")
    if (
        not isinstance(side_effects, dict)
        or set(side_effects) != set(SIDE_EFFECT_KEYS)
        or any(
            side_effects.get(name) is not False for name in SIDE_EFFECT_KEYS
        )
    ):
        raise EvidenceError("side_effects_unsafe")
    if item.get("resources_closed") is not True:
        raise EvidenceError("resources_not_closed")
    if event == "oneshot":
        _validate_oneshot(item)
    elif event == "cycle":
        _validate_cycle(item)
    else:
        _validate_terminal(item)
    expected_keys = {
        "oneshot": ONESHOT_KEYS,
        "cycle": CYCLE_KEYS,
        "terminal": set(item),
    }[event]
    return {key: item[key] for key in sorted(expected_keys)}


def validate_diagnostic_terminal(
    records: Iterable[object], *, source_sha: str, image_digest: str,
    activation_id: str,
) -> dict[str, object]:
    """Accept only safe non-operational terminal records for diagnostics.

    This is intentionally separate from :func:`validate`: callers cannot use a
    FAILED/CLOSED record as activation success evidence.
    """

    matches: list[dict[str, object]] = []
    for value in records:
        if not isinstance(value, dict):
            raise EvidenceError("record_not_object")
        if (
            value.get("mode") == "shadow-continuous"
            and value.get("event") == "terminal"
        ):
            matches.append(value)
    if not matches:
        raise EvidenceError("diagnostic_terminal_not_found")
    item = matches[-1]
    if (
        item.get("source_sha") != source_sha
        or item.get("image_digest") != image_digest
        or item.get("activation_id") != activation_id
    ):
        raise EvidenceError("activation_tuple_mismatch")
    side_effects = item.get("side_effects")
    if (
        not isinstance(side_effects, dict)
        or set(side_effects) != set(SIDE_EFFECT_KEYS)
        or any(
            side_effects.get(name) is not False for name in SIDE_EFFECT_KEYS
        )
    ):
        raise EvidenceError("side_effects_unsafe")
    _validate_diagnostic_terminal(item)
    return {key: item[key] for key in sorted(item)}


def activation_summary(item: dict[str, object]) -> dict[str, object]:
    side = item["side_effects"]
    assert isinstance(side, dict)
    database = bool(item.get("db_identity"))
    return {
        "runtime_status": item.get("status"),
        "cycles": item.get("cycles"),
        "http_attempts": item.get("http_attempts"),
        "first_cycle_start_elapsed_seconds": item.get(
            "first_cycle_start_elapsed_seconds"
        ),
        "second_cycle_start_elapsed_seconds": item.get(
            "second_cycle_start_elapsed_seconds"
        ),
        "second_cycle_interval_seconds": item.get("second_cycle_interval_seconds"),
        "minimum_cycle_interval_seconds": item.get(
            "minimum_cycle_interval_seconds"
        ),
        "db_reopens": item.get("db_reopens"),
        "database": database,
        "decision_telemetry": item.get("decision_telemetry"),
        "side_effects": {
            "orders": side["broker_orders"],
            "account": side["account"],
            "revoke": side["oauth_revoke"],
            "database": database,
            "notifications": side["slack"] or side["gemini"],
            "reports": side["reports"],
            "s3": side["s3"],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=("shadow-once", "shadow-continuous"))
    parser.add_argument("--event", required=True, choices=("oneshot", "cycle", "terminal"))
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--activation-id", required=True)
    parser.add_argument(
        "--input-format", required=True, choices=("json-lines", "ssm-invocation")
    )
    parser.add_argument(
        "--output", required=True, choices=("accepted-record", "activation-summary")
    )
    parser.add_argument(
        "--terminal-policy",
        choices=("operational", "diagnostic"),
        default="operational",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        SOURCE_RE.fullmatch(args.source_sha) is None
        or IMAGE_RE.fullmatch(args.image_digest) is None
        or ACTIVATION_RE.fullmatch(args.activation_id) is None
        or (args.event == "oneshot") != (args.mode == "shadow-once")
    ):
        print("shadow evidence setup failed", file=sys.stderr)
        return 2
    try:
        content = _bounded_read(sys.stdin)
        records, invocation_result = _records(content, args.input_format)
        if args.terminal_policy == "diagnostic":
            if args.mode != "shadow-continuous" or args.event != "terminal":
                raise EvidenceError("diagnostic_terminal_setup_invalid")
            item = validate_diagnostic_terminal(
                records, source_sha=args.source_sha,
                image_digest=args.image_digest, activation_id=args.activation_id,
            )
        else:
            item = validate(
                records, mode=args.mode, event=args.event,
                source_sha=args.source_sha, image_digest=args.image_digest,
                activation_id=args.activation_id,
            )
    except EvidenceError as error:
        print(f"shadow evidence invalid: {error.category}", file=sys.stderr)
        return 1
    output = item if args.output == "accepted-record" else activation_summary(item)
    if args.output == "activation-summary" and invocation_result is not None:
        output.update(invocation_result)
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
