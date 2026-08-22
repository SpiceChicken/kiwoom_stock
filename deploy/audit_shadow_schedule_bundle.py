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
SHA256_RE = re.compile(r"[0-9a-f]{64}")
ARTIFACT_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SESSION_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
SCHEDULE_ACTIVATION_RE = re.compile(r"shadow-session-[0-9]{8}")
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


def _validate_runtime(
    evidence: Mapping[str, object],
    diagnostic: Mapping[str, object],
    expected: AuditExpectation,
) -> tuple[str, int, int, str]:
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
    expected_status = (
        "PASS" if expected.desired_state == "continuous" else "STOPPED"
    )
    if (
        diagnostic_category != "runtime_rejected"
        or category != "runtime_accepted"
        or status != expected_status
        or type(cycles) is not int
        or cycles < 1
        or (
            expected.desired_state == "continuous"
            and cycles != 1
        )
        or type(attempts) is not int
        or attempts != cycles * 6
        or type(evidence.get("db_reopens")) is not int
        or evidence.get("db_reopens") != cycles - 1
        or evidence.get("database") is not True
        or not isinstance(command_id, str)
        or COMMAND_RE.fullmatch(command_id) is None
        or (
            expected.desired_state == "continuous"
            and not isinstance(evidence.get("decision_telemetry"), Mapping)
        )
    ):
        raise ScheduleAuditError("runtime_not_operational")
    return str(status), cycles, attempts, command_id


def _validate_telemetry_row(
    row: Mapping[str, object], expected: AuditExpectation,
    *, session_date: str, expected_cycle: int, config_sha256: str,
) -> None:
    if (
        set(row) != TELEMETRY_ROW_KEYS
        or type(row.get("schema_version")) is not int
        or row.get("schema_version") != 1
        or row.get("activation_id") != expected.activation_id
        or row.get("session_date_kst") != session_date
        or row.get("cycle_index") != expected_cycle
        or row.get("source_sha") != expected.source_sha
        or row.get("image_digest") != expected.image_digest
        or row.get("config_sha256") != config_sha256
        or not isinstance(row.get("row_sha256"), str)
        or SHA256_RE.fullmatch(str(row["row_sha256"])) is None
    ):
        raise ScheduleAuditError("telemetry_row_invalid")
    payload = {key: row[key] for key in ROW_HASH_KEYS}
    if hashlib.sha256(_canonical_json(payload)).hexdigest() != row["row_sha256"]:
        raise ScheduleAuditError("telemetry_row_hash_mismatch")


def _validate_telemetry(
    manifest: Mapping[str, object], compressed: bytes,
    expected: AuditExpectation, *, cycles: int,
) -> tuple[str, int]:
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
        or not isinstance(session_date, str)
        or SESSION_DATE_RE.fullmatch(session_date) is None
        or session_date.replace("-", "")
        != expected.activation_id.removeprefix("shadow-session-")
        or manifest.get("row_count") != cycles
        or manifest.get("first_cycle") != 1
        or manifest.get("last_cycle") != cycles
        or manifest.get("source_sha") != expected.source_sha
        or manifest.get("image_digest") != expected.image_digest
        or not isinstance(config_sha256, str)
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
        or manifest.get("compressed_bytes") != len(compressed)
        or manifest.get("compressed_sha256")
        != hashlib.sha256(compressed).hexdigest()
        or not isinstance(manifest.get("session_sha256"), str)
        or SHA256_RE.fullmatch(str(manifest["session_sha256"])) is None
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--artifact-json", required=True, type=Path)
    parser.add_argument("--artifact-zip", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--cron", required=True)
    parser.add_argument(
        "--desired-state", required=True, choices=("continuous", "stop")
    )
    parser.add_argument("--control-plane-sha", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--activation-id", required=True)
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
        result = validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=AuditExpectation(
                run_id=args.run_id,
                cron=args.cron,
                desired_state=args.desired_state,
                control_plane_sha=args.control_plane_sha,
                source_sha=args.source_sha,
                image_digest=args.image_digest,
                activation_id=args.activation_id,
            ),
        )
    except ScheduleAuditError as error:
        print(f"shadow schedule audit failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
