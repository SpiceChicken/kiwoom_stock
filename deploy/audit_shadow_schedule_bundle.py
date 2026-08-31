#!/usr/bin/env python3
"""Validate one successful scheduled Shadow run and its GitHub artifact."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import gzip
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import sys
from typing import cast, Mapping
import zipfile

try:
    from .notify_shadow_status import (
        ACTIVATION_RE,
        COMMAND_RE,
        IMAGE_RE,
        SOURCE_RE,
        SlackStatusError,
        build_message_values,
    )
    from .shadow_schedule_observation import (
        CRON_CONTRACT,
        RUN_ID_RE,
        ObservationError,
        build_observation,
        validate_observation,
    )
except ImportError:
    from notify_shadow_status import (  # type: ignore[no-redef]
        ACTIVATION_RE,
        COMMAND_RE,
        IMAGE_RE,
        SOURCE_RE,
        SlackStatusError,
        build_message_values,
    )
    from shadow_schedule_observation import (  # type: ignore[no-redef]
        CRON_CONTRACT,
        RUN_ID_RE,
        ObservationError,
        build_observation,
        validate_observation,
    )


MAX_METADATA_BYTES = 16 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
MAX_OBSERVATION_BYTES = 4 * 1024
MAX_TELEMETRY_BYTES = 4 * 1024 * 1024
MAX_TELEMETRY_JSONL_BYTES = 32 * 1024 * 1024
MAX_RUNTIME_INPUT_BYTES = 1_048_576
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SESSION_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SCHEDULE_ACTIVATION_RE = re.compile(r"shadow-session-[0-9]{8}")
IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
STOCK_CODE_RE = re.compile(r"[0-9]{6}")
RUN_KEYS = {
    "id", "event", "status", "conclusion", "head_sha", "head_branch",
    "path", "created_at", "run_started_at",
}
ARTIFACT_KEYS = {
    "id", "name", "size_in_bytes", "expired", "digest", "workflow_run",
}
ARTIFACT_RUN_KEYS = {"id", "head_sha"}
RECEIPT_KEYS = {
    "schema_version", "event", "source_sha", "activation_id",
    "desired_state", "delivery_status", "category",
    "schedule_observation",
}
MANIFEST_KEYS = {
    "event", "schema_version", "activation_id", "session_date_kst",
    "row_count", "first_cycle", "last_cycle", "source_sha",
    "image_digest", "config_sha256", "database_bytes",
    "database_page_size", "database_page_count",
    "finalized_session_count", "session_sha256", "compressed_sha256",
    "compressed_bytes",
}
TELEMETRY_ROW_KEYS = {
    "schema_version", "activation_id", "session_date_kst", "cycle_index",
    "observed_at", "stock_code", "proxy_code", "source_sha",
    "image_digest", "config_sha256", "strategy_slot", "candidate_id",
    "current_price", "vwap", "strength", "trend_rsi", "atr_percent",
    "down_atr_percent", "volume_ratio", "forces", "decision",
    "position_after", "paper_position_id", "continuity", "row_sha256",
    "committed_at",
}
ROW_HASH_KEYS = (
    "schema_version", "activation_id", "session_date_kst", "cycle_index",
    "observed_at", "stock_code", "proxy_code", "source_sha",
    "image_digest", "config_sha256", "strategy_slot", "current_price",
    "vwap", "strength", "trend_rsi", "atr_percent",
    "down_atr_percent", "volume_ratio", "forces", "decision",
    "position_after", "continuity", "candidate_id", "paper_position_id",
)
TELEMETRY_NUMERIC_KEYS = {
    "current_price", "vwap", "strength", "trend_rsi", "atr_percent",
    "down_atr_percent", "volume_ratio",
}
FORCE_KEYS = {
    "thrust", "gravity", "drag", "magnetic", "jerk", "impulse",
    "net_force", "current_velocity", "volume_drop_ratio",
}
DECISION_ALLOWED = {
    "market_regime": {
        "STABLE_BULL", "VOLATILE_BULL", "QUIET_BEAR", "PANIC_BEAR",
        "NEUTRAL",
    },
    "strategy_reason_code": {
        "VI_WAIT", "CLIMAX_SHIELD", "BREAKOUT_OVERRIDE", "THRUST_LOW",
        "NET_FORCE_NEGATIVE", "STALL_SHIELD", "LOW_QUALITY_TREND",
        "VOLUME_EXHAUSTED", "UPTREND_ENTRY", "REVERSAL_ENTRY",
        "WARMING_UP", "JERK_NON_POSITIVE",
    },
    "strategy_intent": {"ENTRY_SIGNAL", "NO_ENTRY_SIGNAL"},
    "paper_action": {"BUY", "SELL", "HOLD"},
    "position_before": {"FLAT", "OPEN", "OVERNIGHT"},
    "trading_window": {"OPEN", "CLOSED"},
    "session_phase": {"ENTRY", "EXIT_ONLY", "CLOSED"},
    "net_force_band": {
        "STRONG_NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE",
        "STRONG_POSITIVE",
    },
    "current_velocity_band": {
        "STRONG_NEGATIVE", "NEGATIVE", "NEUTRAL", "POSITIVE",
        "STRONG_POSITIVE",
    },
    "thrust_band": {
        "BELOW_0_8", "FROM_0_8_TO_1_0", "FROM_1_0_TO_1_5",
        "AT_LEAST_1_5",
    },
    "jerk_band": {"NEGATIVE", "NEUTRAL", "POSITIVE"},
    "strength_band": {"BELOW_100", "AT_100", "ABOVE_100"},
    "trend_rsi_band": {"OVERSOLD", "NEUTRAL", "OVERBOUGHT"},
    "price_vwap_relation": {"BELOW", "AT", "ABOVE"},
}
CONTINUITY_KEYS = {
    "schema_version", "hydration_source", "previous_observed_at",
    "history_depth", "baseline_source", "baseline_sample_index",
    "baseline_time_estimated",
}
TERMINAL_DIAGNOSTIC_KEYS = {
    "status", "reason", "cycles", "db_reopens", "resources_closed",
    "elapsed_seconds", "error_type",
}
TERMINAL_TIMING_KEYS = (
    "first_cycle_start_elapsed_seconds",
    "second_cycle_start_elapsed_seconds",
    "second_cycle_interval_seconds",
    "minimum_cycle_interval_seconds",
)
DIAGNOSTIC_KEYS = {
    "schema_version", "source_sha", "image_digest", "activation_id",
    "desired_state", "command_id", "ssm_status", "ssm_response_code",
    "stdout_bytes", "stderr_bytes", "failure_category", "terminal",
}
DIAGNOSTIC_OPTIONAL_KEYS = {"market_data_failure"}
SAFE_MARKET_DATA_FAILURE_KINDS = {
    "empty", "fetch", "timeout", "parse", "malformed",
}
SAFE_MARKET_DATA_FAILURE_OPERATIONS = {
    "auth_preflight", "top_trading_value", "stock_basic",
    "minute_chart_1m", "minute_chart_5m", "minute_chart_60m",
    "tick_strength", "program_trade", "foreign_window_trade",
    "order_book", "recent_ticks", "market_snapshot", "market_regime_60m",
    "chart_true_range",
}
BASE_MEMBERS = {
    "shadow-worker-evidence.json": MAX_JSON_BYTES,
    "shadow-worker-diagnostic.json": MAX_JSON_BYTES,
    "shadow-status-notification.json": MAX_JSON_BYTES,
}
TELEMETRY_MEMBERS = {
    "shadow-telemetry.manifest.json": MAX_JSON_BYTES,
    "shadow-telemetry.jsonl.gz": MAX_TELEMETRY_BYTES,
}


class ScheduleAuditError(RuntimeError):
    """A bounded, value-free scheduled-run audit rejection."""


@dataclass(frozen=True)
class AuditExpectation:
    run_id: str
    cron: str
    desired_state: str
    control_plane_sha: str
    source_sha: str
    image_digest: str
    activation_id: str

    def validate(self) -> None:
        contract = CRON_CONTRACT.get(self.cron)
        if (
            RUN_ID_RE.fullmatch(self.run_id) is None
            or contract is None
            or contract["desired_state"] != self.desired_state
            or SOURCE_RE.fullmatch(self.control_plane_sha) is None
            or SOURCE_RE.fullmatch(self.source_sha) is None
            or IMAGE_RE.fullmatch(self.image_digest) is None
            or ACTIVATION_RE.fullmatch(self.activation_id) is None
            or SCHEDULE_ACTIVATION_RE.fullmatch(self.activation_id) is None
        ):
            raise ScheduleAuditError("expectation_invalid")


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate")
        value[key] = item
    return value


def _reject_non_json_constant(_value: str) -> object:
    raise ValueError("constant")


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("float")
    return result


def _load_json_bytes(
    raw: bytes, *, maximum_bytes: int, category: str,
) -> dict[str, object]:
    if len(raw) > maximum_bytes:
        raise ScheduleAuditError(category)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise ScheduleAuditError(category) from None
    if type(value) is not dict:
        raise ScheduleAuditError(category)
    return value


def _read_file(path: Path, *, maximum_bytes: int, category: str) -> bytes:
    try:
        if not path.is_file() or path.is_symlink():
            raise ScheduleAuditError(category)
        size = path.stat().st_size
        if size < 1 or size > maximum_bytes:
            raise ScheduleAuditError(category)
        raw = path.read_bytes()
    except OSError:
        raise ScheduleAuditError(category) from None
    if len(raw) != size:
        raise ScheduleAuditError(category)
    return raw


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ScheduleAuditError("telemetry_row_invalid") from None
    return rendered.encode("utf-8")


def _validate_run(
    run: Mapping[str, object], expected: AuditExpectation,
) -> dict[str, object]:
    if (
        set(run) != RUN_KEYS
        or type(run.get("id")) is not int
        or str(run.get("id")) != expected.run_id
        or run.get("event") != "schedule"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or run.get("head_sha") != expected.control_plane_sha
        or run.get("head_branch") != "main"
        or run.get("path")
        != ".github/workflows/cd-shadow-worker-activation.yml"
    ):
        raise ScheduleAuditError("run_invalid")
    try:
        observation = cast(dict[str, object], build_observation(
            {
                "event": run["event"],
                "id": run["id"],
                "created_at": run["created_at"],
                "run_started_at": run["run_started_at"],
                "head_branch": run["head_branch"],
            },
            run_id=expected.run_id,
            cron=expected.cron,
            desired_state=expected.desired_state,
        ))
    except ObservationError:
        raise ScheduleAuditError("run_timing_invalid") from None
    expected_at = observation.get("expected_at_utc")
    if not isinstance(expected_at, str):
        raise ScheduleAuditError("run_timing_invalid")
    try:
        expected_kst = datetime.strptime(
            expected_at, "%Y-%m-%dT%H:%M:%SZ"
        ) + timedelta(hours=9)
    except ValueError:
        raise ScheduleAuditError("run_timing_invalid") from None
    if expected.activation_id != (
        "shadow-session-" + expected_kst.strftime("%Y%m%d")
    ):
        raise ScheduleAuditError("activation_schedule_mismatch")
    return observation


def _validate_artifact_contract(
    artifact: Mapping[str, object], archive: bytes,
    expected: AuditExpectation,
) -> int:
    workflow_run = artifact.get("workflow_run")
    artifact_id = artifact.get("id")
    if (
        set(artifact) != ARTIFACT_KEYS
        or type(artifact_id) is not int
        or artifact_id <= 0
        or artifact.get("name")
        != f"shadow-worker-{expected.source_sha}-{expected.activation_id}"
        or type(artifact.get("size_in_bytes")) is not int
        or artifact.get("size_in_bytes") != len(archive)
        or not 0 < len(archive) <= MAX_ARCHIVE_BYTES
        or artifact.get("expired") is not False
        or not isinstance(artifact.get("digest"), str)
        or ARTIFACT_DIGEST_RE.fullmatch(str(artifact["digest"])) is None
        or artifact.get("digest")
        != "sha256:" + hashlib.sha256(archive).hexdigest()
        or type(workflow_run) is not dict
        or set(workflow_run) != ARTIFACT_RUN_KEYS
        or workflow_run.get("id") != int(expected.run_id)
        or workflow_run.get("head_sha") != expected.control_plane_sha
    ):
        raise ScheduleAuditError("artifact_contract_invalid")
    return artifact_id


def _read_archive(
    archive: bytes, expected: AuditExpectation,
) -> dict[str, bytes]:
    observation_name = (
        f"shadow-schedule-observation-{expected.run_id}.json"
    )
    limits = {**BASE_MEMBERS, observation_name: MAX_OBSERVATION_BYTES}
    if expected.desired_state == "stop":
        limits.update(TELEMETRY_MEMBERS)
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if (
                len(infos) != len(limits)
                or {info.filename for info in infos} != set(limits)
            ):
                raise ScheduleAuditError("artifact_member_set_invalid")
            total_size = 0
            for info in infos:
                path = PurePosixPath(info.filename)
                maximum = limits[info.filename]
                total_size += info.file_size
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or info.is_dir()
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.flag_bits & 0x1
                    or info.file_size < 1
                    or info.file_size > maximum
                    or info.compress_size > MAX_ARCHIVE_BYTES
                    or total_size
                    > MAX_TELEMETRY_JSONL_BYTES + 4 * MAX_JSON_BYTES
                ):
                    raise ScheduleAuditError("artifact_member_unsafe")
            return {name: bundle.read(name) for name in limits}
    except ScheduleAuditError:
        raise
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        raise ScheduleAuditError("artifact_archive_invalid") from None


def _validate_receipt(
    receipt: Mapping[str, object], expected: AuditExpectation,
) -> None:
    if (
        set(receipt) != RECEIPT_KEYS
        or type(receipt.get("schema_version")) is not int
        or receipt.get("schema_version") != 2
        or receipt.get("event") != "shadow-status-notification"
        or receipt.get("source_sha") != expected.source_sha
        or receipt.get("activation_id") != expected.activation_id
        or receipt.get("desired_state") != expected.desired_state
        or receipt.get("delivery_status") != "DELIVERED"
        or receipt.get("category") != "runtime_accepted"
        or receipt.get("schedule_observation") != "accepted"
    ):
        raise ScheduleAuditError("notification_receipt_invalid")


def _is_finite_number(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(cast(int | float, value)))
    except (OverflowError, ValueError):
        return False


def _is_finite_float(value: object, *, minimum: float = 0.0) -> bool:
    return type(value) is float and math.isfinite(value) and value >= minimum


def _is_aware_timestamp(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_session_date(value: object) -> bool:
    if type(value) is not str or SESSION_DATE_RE.fullmatch(value) is None:
        return False
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime(
            "%Y-%m-%d"
        ) == value
    except ValueError:
        return False


def _is_optional_identity(value: object) -> bool:
    return value is None or (
        type(value) is str and IDENTITY_RE.fullmatch(value) is not None
    )


def _valid_decision(value: object) -> bool:
    return (
        type(value) is dict
        and set(value) == set(DECISION_ALLOWED)
        and all(
            type(value.get(key)) is str
            and value.get(key) in allowed
            for key, allowed in DECISION_ALLOWED.items()
        )
        and _valid_decision_consistency(value)
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
        and value.get("net_force_band") not in {
            "STRONG_NEGATIVE", "NEGATIVE",
        }
    ):
        return False
    thrust_band = value.get("thrust_band")
    if reason == "THRUST_LOW" and thrust_band != "BELOW_0_8":
        return False
    if reason == "CLIMAX_SHIELD" and thrust_band != "AT_LEAST_1_5":
        return False
    if reason == "BREAKOUT_OVERRIDE" and thrust_band not in {
        "FROM_1_0_TO_1_5", "AT_LEAST_1_5",
    }:
        return False
    if reason == "STALL_SHIELD" and thrust_band != "FROM_0_8_TO_1_0":
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


def _valid_continuity(value: object) -> bool:
    if type(value) is not dict or set(value) != CONTINUITY_KEYS:
        return False
    previous = value.get("previous_observed_at")
    return (
        type(value.get("schema_version")) is int
        and value.get("schema_version") == 1
        and value.get("hydration_source") in {
            "initial", "legacy_cold_start", "persisted",
        }
        and (previous is None or _is_aware_timestamp(previous))
        and type(value.get("history_depth")) is int
        and cast(int, value["history_depth"]) >= 0
        and value.get("baseline_source") == "row_4_fixed_cadence"
        and type(value.get("baseline_sample_index")) is int
        and value.get("baseline_sample_index") == 4
        and value.get("baseline_time_estimated") is True
    )


def _valid_terminal_diagnostic(
    diagnostic: Mapping[str, object], *, status: str, reason: str,
    cycles: int,
) -> bool:
    terminal = diagnostic.get("terminal")
    return (
        type(terminal) is dict
        and set(terminal) == TERMINAL_DIAGNOSTIC_KEYS
        and terminal.get("status") == status
        and terminal.get("reason") == reason
        and type(terminal.get("cycles")) is int
        and terminal.get("cycles") == cycles
        and type(terminal.get("db_reopens")) is int
        and terminal.get("db_reopens") == max(cycles - 1, 0)
        and terminal.get("resources_closed") is True
        and _is_finite_float(terminal.get("elapsed_seconds"))
        and terminal.get("error_type") is None
    )


def _valid_market_data_failure(value: object) -> bool:
    if value is None:
        return True
    return (
        type(value) is dict
        and set(value) == {"kind", "operation"}
        and isinstance(value.get("kind"), str)
        and value.get("kind") in SAFE_MARKET_DATA_FAILURE_KINDS
        and isinstance(value.get("operation"), str)
        and value.get("operation") in SAFE_MARKET_DATA_FAILURE_OPERATIONS
    )


def _valid_terminal_timing(
    evidence: Mapping[str, object], *, cycles: int,
) -> bool:
    first, second, interval, minimum = (
        evidence.get(key) for key in TERMINAL_TIMING_KEYS
    )
    if cycles == 0:
        return all(value is None for value in (first, second, interval, minimum))
    if not _is_finite_float(first):
        return False
    if cycles == 1:
        return all(value is None for value in (second, interval, minimum))
    if not all(
        _is_finite_float(value, minimum=60.0)
        for value in (second, interval, minimum)
    ):
        return False
    return (
        cast(float, second) - cast(float, first) >= 60
        and abs(
            cast(float, second)
            - cast(float, first)
            - cast(float, interval)
        ) <= 0.000001
    )


def _valid_stop_success_diagnostic(
    diagnostic: Mapping[str, object], expected: AuditExpectation,
    *, command_id: str,
) -> bool:
    stdout_bytes = diagnostic.get("stdout_bytes")
    stderr_bytes = diagnostic.get("stderr_bytes")
    return (
        set(diagnostic) in (
            DIAGNOSTIC_KEYS,
            DIAGNOSTIC_KEYS | DIAGNOSTIC_OPTIONAL_KEYS,
        )
        and type(diagnostic.get("schema_version")) is int
        and diagnostic.get("schema_version") == 1
        and diagnostic.get("source_sha") == expected.source_sha
        and diagnostic.get("image_digest") == expected.image_digest
        and diagnostic.get("activation_id") == expected.activation_id
        and diagnostic.get("desired_state") == "stop"
        and diagnostic.get("command_id") == command_id
        and diagnostic.get("ssm_status") == "Success"
        and type(diagnostic.get("ssm_response_code")) is int
        and diagnostic.get("ssm_response_code") == 0
        and type(stdout_bytes) is int
        and 0 < stdout_bytes <= MAX_RUNTIME_INPUT_BYTES
        and type(stderr_bytes) is int
        and 0 <= stderr_bytes <= MAX_RUNTIME_INPUT_BYTES
        and diagnostic.get("failure_category")
        == "success_without_accepted_runtime_evidence"
        and diagnostic.get("terminal") is None
        and _valid_market_data_failure(diagnostic.get("market_data_failure"))
    )


def _validate_runtime(
    evidence: Mapping[str, object],
    diagnostic: Mapping[str, object],
    expected: AuditExpectation,
) -> tuple[str, int, int | None, str]:
    try:
        diagnostic_category, _diagnostic_message = build_message_values(
            evidence=None,
            diagnostic=diagnostic,
            source_sha=expected.source_sha,
            image_digest=expected.image_digest,
            activation_id=expected.activation_id,
            desired_state=expected.desired_state,
        )
        category, _message = build_message_values(
            evidence=evidence,
            diagnostic=diagnostic,
            source_sha=expected.source_sha,
            image_digest=expected.image_digest,
            activation_id=expected.activation_id,
            desired_state=expected.desired_state,
        )
    except SlackStatusError:
        raise ScheduleAuditError("runtime_artifact_invalid") from None
    status = evidence.get("runtime_status")
    cycles = evidence.get("cycles")
    attempts = evidence.get("http_attempts")
    command_id = evidence.get("command_id")
    if (
        diagnostic_category != "runtime_rejected"
        or category != "runtime_accepted"
        or not isinstance(command_id, str)
        or COMMAND_RE.fullmatch(command_id) is None
        or diagnostic.get("command_id") != command_id
        or evidence.get("ssm_status") != "Success"
        or type(evidence.get("ssm_response_code")) is not int
        or evidence.get("ssm_response_code") != 0
        or diagnostic.get("ssm_status") != "Success"
        or type(diagnostic.get("ssm_response_code")) is not int
        or diagnostic.get("ssm_response_code") != 0
    ):
        raise ScheduleAuditError("runtime_not_operational")
    if expected.desired_state == "continuous" and status == "CLOSED":
        side_effects = evidence.get("side_effects")
        if (
            type(cycles) is not int
            or cycles != 0
            or attempts is not None
            or type(evidence.get("db_reopens")) is not int
            or evidence.get("db_reopens") != 0
            or evidence.get("database") is not False
            or evidence.get("decision_telemetry") is not None
            or type(side_effects) is not dict
            or side_effects.get("database") is not False
            or not _valid_terminal_timing(evidence, cycles=cycles)
            or not _valid_terminal_diagnostic(
                diagnostic,
                status="CLOSED",
                reason="calendar-closed",
                cycles=cycles,
            )
        ):
            raise ScheduleAuditError("runtime_not_operational")
        return status, cycles, 0, command_id
    if expected.desired_state == "stop":
        side_effects = evidence.get("side_effects")
        if (
            status != "STOPPED"
            or type(cycles) is not int
            or cycles < 1
            or attempts is not None
            or type(evidence.get("db_reopens")) is not int
            or evidence.get("db_reopens") != cycles - 1
            or evidence.get("database") is not False
            or evidence.get("decision_telemetry") is not None
            or type(side_effects) is not dict
            or side_effects.get("database") is not False
            or not _valid_terminal_timing(evidence, cycles=cycles)
            or not _valid_stop_success_diagnostic(
                diagnostic, expected, command_id=command_id,
            )
        ):
            raise ScheduleAuditError("runtime_not_operational")
        return status, cycles, None, command_id
    if (
        status != "PASS"
        or type(cycles) is not int
        or cycles != 1
        or type(attempts) is not int
        or attempts != cycles * 6
        or type(evidence.get("db_reopens")) is not int
        or evidence.get("db_reopens") != cycles - 1
        or evidence.get("database") is not True
        or not isinstance(evidence.get("decision_telemetry"), Mapping)
    ):
        raise ScheduleAuditError("runtime_not_operational")
    return str(status), cycles, attempts, command_id


def _validate_telemetry_row(
    row: Mapping[str, object], expected: AuditExpectation,
    *, session_date: str, expected_cycle: int, config_sha256: str,
) -> None:
    forces = row.get("forces")
    decision = row.get("decision")
    continuity = row.get("continuity")
    if (
        set(row) != TELEMETRY_ROW_KEYS
        or type(row.get("schema_version")) is not int
        or row.get("schema_version") != 1
        or row.get("activation_id") != expected.activation_id
        or row.get("session_date_kst") != session_date
        or type(row.get("cycle_index")) is not int
        or row.get("cycle_index") != expected_cycle
        or not _is_aware_timestamp(row.get("observed_at"))
        or type(row.get("stock_code")) is not str
        or STOCK_CODE_RE.fullmatch(str(row["stock_code"])) is None
        or type(row.get("proxy_code")) is not str
        or STOCK_CODE_RE.fullmatch(str(row["proxy_code"])) is None
        or row.get("source_sha") != expected.source_sha
        or row.get("image_digest") != expected.image_digest
        or row.get("config_sha256") != config_sha256
        or type(row.get("strategy_slot")) is not str
        or IDENTITY_RE.fullmatch(str(row["strategy_slot"])) is None
        or not _is_optional_identity(row.get("candidate_id"))
        or any(
            not _is_finite_number(row.get(key))
            for key in TELEMETRY_NUMERIC_KEYS
        )
        or type(forces) is not dict
        or set(forces) != FORCE_KEYS
        or any(not _is_finite_number(forces.get(key)) for key in FORCE_KEYS)
        or not _valid_decision(decision)
        or row.get("position_after") not in {
            "FLAT", "OPEN", "OVERNIGHT", "CLOSED",
        }
        or not _is_optional_identity(row.get("paper_position_id"))
        or not _valid_continuity(continuity)
        or type(row.get("row_sha256")) is not str
        or SHA256_RE.fullmatch(str(row["row_sha256"])) is None
        or not _is_aware_timestamp(row.get("committed_at"))
    ):
        raise ScheduleAuditError("telemetry_row_invalid")
    payload = {key: row[key] for key in ROW_HASH_KEYS}
    if hashlib.sha256(_canonical_json(payload)).hexdigest() != row["row_sha256"]:
        raise ScheduleAuditError("telemetry_row_hash_mismatch")


def _validate_manifest_schema(
    manifest: Mapping[str, object], expected: AuditExpectation, *, cycles: int,
) -> tuple[str, str]:
    session_date = manifest.get("session_date_kst")
    config_sha256 = manifest.get("config_sha256")
    database_bytes = manifest.get("database_bytes")
    database_page_size = manifest.get("database_page_size")
    database_page_count = manifest.get("database_page_count")
    finalized_session_count = manifest.get("finalized_session_count")
    if (
        set(manifest) != MANIFEST_KEYS
        or manifest.get("event") != "telemetry_manifest"
        or type(manifest.get("schema_version")) is not int
        or manifest.get("schema_version") != 1
        or manifest.get("activation_id") != expected.activation_id
        or not _is_session_date(session_date)
        or cast(str, session_date).replace("-", "")
        != expected.activation_id.removeprefix("shadow-session-")
        or type(manifest.get("row_count")) is not int
        or manifest.get("row_count") != cycles
        or type(manifest.get("first_cycle")) is not int
        or manifest.get("first_cycle") != 1
        or type(manifest.get("last_cycle")) is not int
        or manifest.get("last_cycle") != cycles
        or manifest.get("source_sha") != expected.source_sha
        or manifest.get("image_digest") != expected.image_digest
        or type(config_sha256) is not str
        or SHA256_RE.fullmatch(config_sha256) is None
        or type(database_bytes) is not int
        or not 0 < database_bytes <= 32 * 1024 * 1024
        or type(database_page_size) is not int
        or database_page_size <= 0
        or type(database_page_count) is not int
        or database_page_count <= 0
        or database_bytes != database_page_size * database_page_count
        or type(finalized_session_count) is not int
        or not 1 <= finalized_session_count <= 20
        or type(manifest.get("compressed_bytes")) is not int
        or type(manifest.get("compressed_sha256")) is not str
        or SHA256_RE.fullmatch(str(manifest["compressed_sha256"])) is None
        or type(manifest.get("session_sha256")) is not str
        or SHA256_RE.fullmatch(str(manifest["session_sha256"])) is None
    ):
        raise ScheduleAuditError("telemetry_manifest_invalid")
    return cast(str, session_date), cast(str, config_sha256)


def _validate_telemetry(
    manifest: Mapping[str, object], compressed: bytes,
    expected: AuditExpectation, *, cycles: int,
) -> tuple[str, int]:
    session_date, config_sha256 = _validate_manifest_schema(
        manifest, expected, cycles=cycles,
    )
    if (
        manifest.get("compressed_bytes") != len(compressed)
        or manifest.get("compressed_sha256")
        != hashlib.sha256(compressed).hexdigest()
    ):
        raise ScheduleAuditError("telemetry_manifest_invalid")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            jsonl = stream.read(MAX_TELEMETRY_JSONL_BYTES + 1)
    except (EOFError, OSError):
        raise ScheduleAuditError("telemetry_archive_invalid") from None
    if (
        not jsonl
        or len(jsonl) > MAX_TELEMETRY_JSONL_BYTES
        or hashlib.sha256(jsonl).hexdigest()
        != manifest["session_sha256"]
    ):
        raise ScheduleAuditError("telemetry_archive_invalid")
    lines = jsonl.splitlines()
    if len(lines) != cycles or any(not line for line in lines):
        raise ScheduleAuditError("telemetry_row_count_mismatch")
    for cycle, line in enumerate(lines, start=1):
        row = _load_json_bytes(
            line,
            maximum_bytes=MAX_JSON_BYTES,
            category="telemetry_row_invalid",
        )
        _validate_telemetry_row(
            row,
            expected,
            session_date=session_date,
            expected_cycle=cycle,
            config_sha256=config_sha256,
        )
    return session_date, len(lines)


def validate_bundle(
    *,
    run: Mapping[str, object],
    artifact: Mapping[str, object],
    archive: bytes,
    expected: AuditExpectation,
) -> dict[str, object]:
    expected.validate()
    expected_observation = _validate_run(run, expected)
    artifact_id = _validate_artifact_contract(artifact, archive, expected)
    members = _read_archive(archive, expected)
    observation_name = (
        f"shadow-schedule-observation-{expected.run_id}.json"
    )
    observation = _load_json_bytes(
        members[observation_name],
        maximum_bytes=MAX_OBSERVATION_BYTES,
        category="schedule_observation_invalid",
    )
    try:
        validated_observation = validate_observation(
            observation,
            desired_state=expected.desired_state,
            expected_run_id=expected.run_id,
            expected_cron=expected.cron,
        )
    except ObservationError:
        raise ScheduleAuditError("schedule_observation_invalid") from None
    if validated_observation != expected_observation:
        raise ScheduleAuditError("schedule_observation_run_mismatch")
    receipt = _load_json_bytes(
        members["shadow-status-notification.json"],
        maximum_bytes=MAX_JSON_BYTES,
        category="notification_receipt_invalid",
    )
    _validate_receipt(receipt, expected)
    evidence = _load_json_bytes(
        members["shadow-worker-evidence.json"],
        maximum_bytes=MAX_JSON_BYTES,
        category="runtime_artifact_invalid",
    )
    diagnostic = _load_json_bytes(
        members["shadow-worker-diagnostic.json"],
        maximum_bytes=MAX_JSON_BYTES,
        category="runtime_artifact_invalid",
    )
    status, cycles, attempts, command_id = _validate_runtime(
        evidence, diagnostic, expected,
    )
    session_date: str | None = None
    telemetry_rows: int | None = None
    if expected.desired_state == "stop":
        manifest = _load_json_bytes(
            members["shadow-telemetry.manifest.json"],
            maximum_bytes=MAX_JSON_BYTES,
            category="telemetry_manifest_invalid",
        )
        session_date, telemetry_rows = _validate_telemetry(
            manifest,
            members["shadow-telemetry.jsonl.gz"],
            expected,
            cycles=cycles,
        )
        attempts = cycles * 6
    return {
        "schema_version": 1,
        "status": "PASS",
        "run_id": expected.run_id,
        "artifact_id": artifact_id,
        "control_plane_sha": expected.control_plane_sha,
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "activation_id": expected.activation_id,
        "desired_state": expected.desired_state,
        "runtime_status": status,
        "cycles": cycles,
        "http_attempts": attempts,
        "command_id": command_id,
        "schedule_delay_seconds": validated_observation[
            "total_start_delay_seconds"
        ],
        "notification_delivery_status": receipt["delivery_status"],
        "session_date_kst": session_date,
        "telemetry_rows": telemetry_rows,
    }


def _auto_run_identity(
    run: Mapping[str, object],
) -> tuple[str, str]:
    run_id = run.get("id")
    control_plane_sha = run.get("head_sha")
    if (
        set(run) != RUN_KEYS
        or type(run_id) is not int
        or RUN_ID_RE.fullmatch(str(run_id)) is None
        or run.get("event") != "schedule"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(control_plane_sha) is not str
        or SOURCE_RE.fullmatch(control_plane_sha) is None
        or run.get("head_branch") != "main"
        or run.get("path")
        != ".github/workflows/cd-shadow-worker-activation.yml"
    ):
        raise ScheduleAuditError("auto_schedule_run_invalid")
    return str(run_id), control_plane_sha


def _auto_expectation(
    run: Mapping[str, object], *, run_id: str, cron: str,
    desired_state: str, control_plane_sha: str, source_sha: str,
    image_digest: str,
) -> AuditExpectation:
    try:
        observation = build_observation(
            {
                "event": run["event"],
                "id": run["id"],
                "created_at": run["created_at"],
                "run_started_at": run["run_started_at"],
                "head_branch": run["head_branch"],
            },
            run_id=run_id,
            cron=cron,
            desired_state=desired_state,
        )
        expected_at = observation.get("expected_at_utc")
        if type(expected_at) is not str:
            raise ObservationError("invalid")
        expected_kst = datetime.strptime(
            expected_at, "%Y-%m-%dT%H:%M:%SZ"
        ) + timedelta(hours=9)
    except (KeyError, ObservationError, ValueError):
        raise ScheduleAuditError("auto_schedule_candidate_invalid") from None
    return AuditExpectation(
        run_id=run_id,
        cron=cron,
        desired_state=desired_state,
        control_plane_sha=control_plane_sha,
        source_sha=source_sha,
        image_digest=image_digest,
        activation_id="shadow-session-" + expected_kst.strftime("%Y%m%d"),
    )


def validate_auto_schedule_bundle(
    *, run: Mapping[str, object], artifact: Mapping[str, object],
    archive: bytes, source_sha: str, image_digest: str,
) -> dict[str, object]:
    """Fully validate both cron candidates and accept exactly one."""
    if (
        type(source_sha) is not str
        or SOURCE_RE.fullmatch(source_sha) is None
        or type(image_digest) is not str
        or IMAGE_RE.fullmatch(image_digest) is None
    ):
        raise ScheduleAuditError("auto_schedule_trust_anchor_invalid")
    run_id, control_plane_sha = _auto_run_identity(run)
    passed: list[dict[str, object]] = []
    for cron, contract in CRON_CONTRACT.items():
        try:
            expected = _auto_expectation(
                run,
                run_id=run_id,
                cron=cron,
                desired_state=contract["desired_state"],
                control_plane_sha=control_plane_sha,
                source_sha=source_sha,
                image_digest=image_digest,
            )
            passed.append(validate_bundle(
                run=run,
                artifact=artifact,
                archive=archive,
                expected=expected,
            ))
        except ScheduleAuditError:
            continue
    if not passed:
        raise ScheduleAuditError("auto_schedule_unresolved")
    if len(passed) != 1:
        raise ScheduleAuditError("auto_schedule_ambiguous")
    return passed[0]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--artifact-json", required=True, type=Path)
    parser.add_argument("--artifact-zip", required=True, type=Path)
    parser.add_argument("--auto-schedule", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--cron")
    parser.add_argument(
        "--desired-state", choices=("continuous", "stop")
    )
    parser.add_argument("--control-plane-sha")
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--activation-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        run = _load_json_bytes(
            _read_file(
                args.run_json,
                maximum_bytes=MAX_METADATA_BYTES,
                category="run_input_invalid",
            ),
            maximum_bytes=MAX_METADATA_BYTES,
            category="run_input_invalid",
        )
        artifact = _load_json_bytes(
            _read_file(
                args.artifact_json,
                maximum_bytes=MAX_METADATA_BYTES,
                category="artifact_input_invalid",
            ),
            maximum_bytes=MAX_METADATA_BYTES,
            category="artifact_input_invalid",
        )
        archive = _read_file(
            args.artifact_zip,
            maximum_bytes=MAX_ARCHIVE_BYTES,
            category="artifact_archive_invalid",
        )
        explicit = (
            args.run_id,
            args.cron,
            args.desired_state,
            args.control_plane_sha,
            args.activation_id,
        )
        if args.auto_schedule:
            if any(value is not None for value in explicit):
                raise ScheduleAuditError("audit_mode_invalid")
            result = validate_auto_schedule_bundle(
                run=run,
                artifact=artifact,
                archive=archive,
                source_sha=args.source_sha,
                image_digest=args.image_digest,
            )
        else:
            if any(value is None for value in explicit):
                raise ScheduleAuditError("audit_mode_invalid")
            result = validate_bundle(
                run=run,
                artifact=artifact,
                archive=archive,
                expected=AuditExpectation(
                    run_id=cast(str, args.run_id),
                    cron=cast(str, args.cron),
                    desired_state=cast(str, args.desired_state),
                    control_plane_sha=cast(str, args.control_plane_sha),
                    source_sha=args.source_sha,
                    image_digest=args.image_digest,
                    activation_id=cast(str, args.activation_id),
                ),
            )
    except ScheduleAuditError as error:
        print(f"shadow schedule audit failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
