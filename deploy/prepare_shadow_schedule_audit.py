#!/usr/bin/env python3
"""Select one bounded artifact for a completed scheduled Shadow run."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sys
from typing import Mapping, cast


REPOSITORY = "SpiceChicken/kiwoom_stock"
WORKFLOW_NAME = "Shadow worker activation"
WORKFLOW_PATH = ".github/workflows/cd-shadow-worker-activation.yml"
MAX_EVENT_BYTES = 16 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_ARTIFACT_LIST_BYTES = 128 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 100
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)
ARTIFACT_NAME_RE = re.compile(
    r"shadow-worker-(?P<source>[0-9a-f]{40})-"
    r"(?P<activation>shadow-session-[0-9]{8})"
)
EVENT_KEYS = {"repository", "workflow_run"}
EVENT_REPOSITORY_KEYS = {"full_name"}
EVENT_RUN_KEYS = {
    "id", "run_attempt", "event", "status", "conclusion", "name", "path",
    "head_sha", "head_branch", "head_repository", "created_at",
    "run_started_at",
}
EVENT_HEAD_REPOSITORY_KEYS = {"full_name"}
RUN_KEYS = {
    "id", "event", "status", "conclusion", "head_sha", "head_branch",
    "path", "created_at", "run_started_at",
}
ARTIFACT_LIST_KEYS = {"total_count", "artifacts"}
ARTIFACT_KEYS = {
    "id", "name", "size_in_bytes", "expired", "digest", "workflow_run",
}
ARTIFACT_RUN_KEYS = {"id", "head_sha"}
SELECTION_KEYS = {"artifact_id", "artifact"}


class AuditPreparationError(RuntimeError):
    """A bounded, value-free preparation rejection."""


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> object:
    raise ValueError("constant")


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("float")
    return result


def _load_json(
    path: Path, *, maximum_bytes: int, category: str,
) -> dict[str, object]:
    try:
        if not path.is_file() or path.is_symlink():
            raise AuditPreparationError(category)
        size = path.stat().st_size
        if size < 1 or size > maximum_bytes:
            raise AuditPreparationError(category)
        raw = path.read_bytes()
    except OSError:
        raise AuditPreparationError(category) from None
    if len(raw) != size:
        raise AuditPreparationError(category)
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise AuditPreparationError(category) from None
    if type(value) is not dict:
        raise AuditPreparationError(category)
    return value


def _parse_timestamp(value: object, *, category: str) -> datetime:
    if type(value) is not str or TIMESTAMP_RE.fullmatch(value) is None:
        raise AuditPreparationError(category)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise AuditPreparationError(category) from None
    return parsed.replace(tzinfo=timezone.utc)


def _positive_run_id(value: object) -> bool:
    return type(value) is int and RUN_ID_RE.fullmatch(str(value)) is not None


def _validate_event(event: Mapping[str, object]) -> Mapping[str, object]:
    repository = event.get("repository")
    workflow_run = event.get("workflow_run")
    if (
        set(event) != EVENT_KEYS
        or type(repository) is not dict
        or set(repository) != EVENT_REPOSITORY_KEYS
        or repository.get("full_name") != REPOSITORY
        or type(workflow_run) is not dict
        or set(workflow_run) != EVENT_RUN_KEYS
    ):
        raise AuditPreparationError("event_invalid")
    head_repository = workflow_run.get("head_repository")
    if (
        not _positive_run_id(workflow_run.get("id"))
        or not _positive_run_id(workflow_run.get("run_attempt"))
        or workflow_run.get("event") != "schedule"
        or workflow_run.get("status") != "completed"
        or workflow_run.get("conclusion") != "success"
        or workflow_run.get("name") != WORKFLOW_NAME
        or workflow_run.get("path") != WORKFLOW_PATH
        or type(workflow_run.get("head_sha")) is not str
        or SOURCE_RE.fullmatch(str(workflow_run["head_sha"])) is None
        or workflow_run.get("head_branch") != "main"
        or type(head_repository) is not dict
        or set(head_repository) != EVENT_HEAD_REPOSITORY_KEYS
        or head_repository.get("full_name") != REPOSITORY
    ):
        raise AuditPreparationError("event_invalid")
    created = _parse_timestamp(
        workflow_run.get("created_at"), category="event_invalid"
    )
    started = _parse_timestamp(
        workflow_run.get("run_started_at"), category="event_invalid"
    )
    if started < created:
        raise AuditPreparationError("event_invalid")
    return workflow_run


def _validate_run(run: Mapping[str, object]) -> None:
    if (
        set(run) != RUN_KEYS
        or not _positive_run_id(run.get("id"))
        or run.get("event") != "schedule"
        or run.get("status") != "completed"
        or run.get("conclusion") != "success"
        or type(run.get("head_sha")) is not str
        or SOURCE_RE.fullmatch(str(run["head_sha"])) is None
        or run.get("head_branch") != "main"
        or run.get("path") != WORKFLOW_PATH
    ):
        raise AuditPreparationError("run_invalid")
    created = _parse_timestamp(run.get("created_at"), category="run_invalid")
    started = _parse_timestamp(
        run.get("run_started_at"), category="run_invalid"
    )
    if started < created:
        raise AuditPreparationError("run_invalid")


def _validate_artifact(
    artifact: Mapping[str, object], *, run_id: int, head_sha: str,
    expected_source_sha: str,
) -> None:
    workflow_run = artifact.get("workflow_run")
    name = artifact.get("name")
    match = ARTIFACT_NAME_RE.fullmatch(name) if type(name) is str else None
    if (
        set(artifact) != ARTIFACT_KEYS
        or not _positive_run_id(artifact.get("id"))
        or match is None
        or match.group("source") != expected_source_sha
        or type(artifact.get("size_in_bytes")) is not int
        or not 0 < cast(int, artifact["size_in_bytes"]) <= MAX_ARCHIVE_BYTES
        or artifact.get("expired") is not False
        or type(artifact.get("digest")) is not str
        or DIGEST_RE.fullmatch(str(artifact["digest"])) is None
        or type(workflow_run) is not dict
        or set(workflow_run) != ARTIFACT_RUN_KEYS
        or workflow_run.get("id") != run_id
        or workflow_run.get("head_sha") != head_sha
    ):
        raise AuditPreparationError("artifact_invalid")


def prepare_audit(
    *, event: Mapping[str, object], run: Mapping[str, object],
    artifact_list: Mapping[str, object], expected_source_sha: str,
    artifact_metadata: Mapping[str, object] | None = None,
) -> dict[str, object]:
    if (
        type(expected_source_sha) is not str
        or SOURCE_RE.fullmatch(expected_source_sha) is None
    ):
        raise AuditPreparationError("source_invalid")
    event_run = _validate_event(event)
    _validate_run(run)
    for key in (
        "id", "event", "status", "conclusion", "path", "head_sha",
        "head_branch", "created_at", "run_started_at",
    ):
        if event_run[key] != run[key]:
            raise AuditPreparationError("event_run_mismatch")

    artifacts = artifact_list.get("artifacts")
    total_count = artifact_list.get("total_count")
    if (
        set(artifact_list) != ARTIFACT_LIST_KEYS
        or type(total_count) is not int
        or not 0 <= total_count <= MAX_ARTIFACTS
        or type(artifacts) is not list
        or len(artifacts) != total_count
        or total_count != 1
        or any(type(item) is not dict for item in artifacts)
    ):
        raise AuditPreparationError("artifact_list_invalid")
    selected = cast(dict[str, object], artifacts[0])
    _validate_artifact(
        selected,
        run_id=cast(int, run["id"]),
        head_sha=cast(str, run["head_sha"]),
        expected_source_sha=expected_source_sha,
    )
    if artifact_metadata is not None:
        _validate_artifact(
            artifact_metadata,
            run_id=cast(int, run["id"]),
            head_sha=cast(str, run["head_sha"]),
            expected_source_sha=expected_source_sha,
        )
        if dict(artifact_metadata) != selected:
            raise AuditPreparationError("artifact_metadata_mismatch")
    return {"artifact_id": selected["id"], "artifact": selected}


def _write_selection(path: Path, selection: Mapping[str, object]) -> None:
    if set(selection) != SELECTION_KEYS or path.is_symlink():
        raise AuditPreparationError("output_invalid")
    encoded = (
        json.dumps(
            selection, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise AuditPreparationError("output_invalid")
    try:
        path.write_bytes(encoded)
    except OSError:
        raise AuditPreparationError("output_invalid") from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event-json", required=True, type=Path)
    parser.add_argument("--run-json", required=True, type=Path)
    parser.add_argument("--artifact-list-json", required=True, type=Path)
    parser.add_argument("--artifact-json", type=Path)
    parser.add_argument("--expected-source-sha", required=True)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        event = _load_json(
            args.event_json,
            maximum_bytes=MAX_EVENT_BYTES,
            category="event_input_invalid",
        )
        run = _load_json(
            args.run_json,
            maximum_bytes=MAX_METADATA_BYTES,
            category="run_input_invalid",
        )
        artifact_list = _load_json(
            args.artifact_list_json,
            maximum_bytes=MAX_ARTIFACT_LIST_BYTES,
            category="artifact_list_input_invalid",
        )
        artifact_metadata = (
            _load_json(
                args.artifact_json,
                maximum_bytes=MAX_METADATA_BYTES,
                category="artifact_input_invalid",
            )
            if args.artifact_json is not None
            else None
        )
        selection = prepare_audit(
            event=event,
            run=run,
            artifact_list=artifact_list,
            expected_source_sha=args.expected_source_sha,
            artifact_metadata=artifact_metadata,
        )
        _write_selection(args.output, selection)
    except AuditPreparationError as error:
        print(
            f"shadow schedule audit preparation failed: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
