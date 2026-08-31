#!/usr/bin/env python3
"""Build a bounded, non-sensitive diagnostic for one shadow SSM invocation."""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Mapping, TextIO

from shadow_runtime_evidence import (
    ACTIVATION_RE,
    IMAGE_RE,
    MAX_INPUT_BYTES,
    MAX_LINE_BYTES,
    MAX_LINES,
    SAFE_MARKET_DATA_FAILURE_KINDS,
    SAFE_MARKET_DATA_FAILURE_OPERATIONS,
    SOURCE_RE,
    EvidenceError,
    _json_lines,
    validate_diagnostic_terminal,
)


COMMAND_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SSM_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
PHYSICAL_STATE_SENTINEL = (
    "shadow worker failed: error_type=PhysicalStateValidationError"
)
STOP_TARGET_ABSENT_SENTINEL = (
    "shadow worker failed: shadow container is absent; "
    "stop identity cannot be proven"
)
PHYSICAL_STATE_CATEGORY = "physical_state_validation_error"
STOP_TARGET_ABSENT_CATEGORY = "stop_target_absent"
SAFE_TERMINAL_ERROR_TYPES = {
    "PhysicalStateValidationError", "MarketDataCollectionError",
}
MARKET_DATA_TYPE_SENTINEL = (
    "shadow worker failed: error_type=MarketDataCollectionError"
)
MARKET_DATA_SENTINEL_RE = re.compile(
    r"^shadow worker failed: error_type=MarketDataCollectionError "
    r"error_kind=(?P<kind>[a-z]+) "
    r"error_operation=(?P<operation>[a-z0-9_]+)$"
)
SAFE_FAILURE_MARKERS = (
    ("shadow worker failed: image_pull_category=image_pull_no_space",
     "image_pull_no_space"),
    ("shadow worker failed: image_pull_category=image_pull_auth",
     "image_pull_auth"),
    ("shadow worker failed: image_pull_category=image_pull_not_found",
     "image_pull_not_found"),
    ("shadow worker failed: image_pull_category=image_pull_network",
     "image_pull_network"),
    ("shadow worker failed: image_pull_category=image_pull_failed",
     "image_pull_failed"),
    ("container image mismatch", "container_identity_mismatch"),
    ("container source SHA mismatch", "container_identity_mismatch"),
    ("container digest label mismatch", "container_identity_mismatch"),
    ("container activation ID mismatch", "container_identity_mismatch"),
    ("container mode mismatch", "container_identity_mismatch"),
    ("container command tuple mismatch", "container_identity_mismatch"),
    ("shadow worker did not stop within", "graceful_stop_timeout"),
    ("terminal_contract_invalid", "runtime_terminal_nonoperational"),
    (
        "continuous terminal safe evidence is missing",
        "terminal_evidence_missing",
    ),
    ("shadow worker did not exit cleanly", "runtime_exit_nonzero"),
    ("shadow terminal state does not match", "terminal_transition_mismatch"),
    ("shadow container removal failed", "container_removal_failed"),
    ("shadow container remains after stop", "container_removal_failed"),
)


def _bounded_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value.encode("utf-8")) > MAX_INPUT_BYTES:
        return None
    return value


def _bounded_lines(value: str | None) -> tuple[str, ...] | None:
    if value is None:
        return None
    lines = value.splitlines()
    if len(lines) > MAX_LINES or any(
        len(line.encode("utf-8")) > MAX_LINE_BYTES for line in lines
    ):
        return None
    return tuple(lines)


def _market_data_sentinel_details(
    *streams: str | None,
) -> tuple[str, str] | None:
    matches: list[tuple[str, str]] = []
    for stream in streams:
        lines = _bounded_lines(stream)
        if lines is None:
            continue
        for line in lines:
            matched = MARKET_DATA_SENTINEL_RE.fullmatch(line)
            if matched is None:
                continue
            kind = matched.group("kind")
            operation = matched.group("operation")
            if (
                isinstance(kind, str)
                and kind in SAFE_MARKET_DATA_FAILURE_KINDS
                and isinstance(operation, str)
                and operation in SAFE_MARKET_DATA_FAILURE_OPERATIONS
            ):
                matches.append((kind, operation))
    unique = set(matches)
    return next(iter(unique)) if len(unique) == 1 else None


def _fallback_classification(
    status: object, terminal: Mapping[str, object] | None,
) -> str:
    if terminal is not None:
        return "runtime_terminal_nonoperational"
    if status == "Success":
        return "success_without_accepted_runtime_evidence"
    if status in SSM_STATUSES:
        return f"ssm_{str(status).lower()}_unclassified"
    return "invocation_envelope_invalid"


def _classification(
    status: object,
    stdout: str | None,
    stderr: str | None,
    terminal: Mapping[str, object] | None,
    *,
    desired_state: str,
) -> str:
    categories: set[str] = set()
    stdout_lines = _bounded_lines(stdout)
    stderr_lines = _bounded_lines(stderr)
    for lines in (stdout_lines, stderr_lines):
        if lines is None:
            continue
        if MARKET_DATA_TYPE_SENTINEL in lines:
            categories.add("market_data_collection_error")
        if _market_data_sentinel_details(stdout, stderr) is not None:
            categories.add("market_data_collection_error")
        if PHYSICAL_STATE_SENTINEL in lines:
            categories.add(PHYSICAL_STATE_CATEGORY)
        if (
            desired_state == "stop"
            and STOP_TARGET_ABSENT_SENTINEL in lines
        ):
            categories.add(STOP_TARGET_ABSENT_CATEGORY)
    if terminal is not None and (
        terminal.get("error_type") == "PhysicalStateValidationError"
    ):
        categories.add(PHYSICAL_STATE_CATEGORY)
    if terminal is not None and (
        terminal.get("error_type") == "MarketDataCollectionError"
    ):
        categories.add("market_data_collection_error")
    if stderr is not None:
        for marker, category in SAFE_FAILURE_MARKERS:
            if marker in stderr:
                categories.add(category)
    if len(categories) == 1:
        return categories.pop()
    return _fallback_classification(status, terminal)


def build_diagnostic(
    invocation: object,
    *,
    source_sha: str,
    image_digest: str,
    activation_id: str,
    desired_state: str,
    command_id: str,
) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": 1,
        "source_sha": source_sha,
        "image_digest": image_digest,
        "activation_id": activation_id,
        "desired_state": desired_state,
        "command_id": command_id,
        "ssm_status": None,
        "ssm_response_code": None,
        "stdout_bytes": None,
        "stderr_bytes": None,
        "failure_category": "invocation_envelope_invalid",
        "terminal": None,
    }
    if not isinstance(invocation, Mapping):
        return result
    status = invocation.get("Status")
    response_code = invocation.get("ResponseCode")
    stdout = _bounded_text(invocation.get("StandardOutputContent"))
    stderr = _bounded_text(invocation.get("StandardErrorContent"))
    result.update(
        {
            "ssm_status": status if status in SSM_STATUSES else None,
            "ssm_response_code": (
                response_code if type(response_code) is int else None
            ),
            "stdout_bytes": (
                len(stdout.encode("utf-8")) if stdout is not None else None
            ),
            "stderr_bytes": (
                len(stderr.encode("utf-8")) if stderr is not None else None
            ),
        }
    )
    terminal: Mapping[str, object] | None = None
    if stdout is not None:
        try:
            terminal = validate_diagnostic_terminal(
                _json_lines(stdout),
                source_sha=source_sha,
                image_digest=image_digest,
                activation_id=activation_id,
            )
        except EvidenceError:
            terminal = None
    if terminal is not None:
        terminal_summary = {
            key: terminal.get(key)
            for key in (
                "status",
                "reason",
                "cycles",
                "db_reopens",
                "resources_closed",
                "elapsed_seconds",
            )
        }
        terminal_error_type = terminal.get("error_type")
        terminal_summary["error_type"] = (
            terminal_error_type
            if terminal_error_type in SAFE_TERMINAL_ERROR_TYPES
            else None
        )
        if terminal_error_type == "MarketDataCollectionError":
            terminal_summary["error_kind"] = terminal.get("error_kind")
            terminal_summary["error_operation"] = terminal.get("error_operation")
        result["terminal"] = terminal_summary
    sentinel_details = _market_data_sentinel_details(stdout, stderr)
    terminal_details = None
    if terminal is not None and terminal.get("error_type") == "MarketDataCollectionError":
        kind = terminal.get("error_kind")
        operation = terminal.get("error_operation")
        if (
            isinstance(kind, str)
            and kind in SAFE_MARKET_DATA_FAILURE_KINDS
            and isinstance(operation, str)
            and operation in SAFE_MARKET_DATA_FAILURE_OPERATIONS
        ):
            terminal_details = (kind, operation)
    if (
        sentinel_details is not None
        and terminal_details is not None
        and sentinel_details != terminal_details
    ):
        sentinel_details = None
        terminal_details = None
    details = terminal_details or sentinel_details
    if details is not None:
        result["market_data_failure"] = {
            "kind": details[0], "operation": details[1],
        }
    result["failure_category"] = _classification(
        status,
        stdout,
        stderr,
        terminal,
        desired_state=desired_state,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--activation-id", required=True)
    parser.add_argument(
        "--desired-state",
        required=True,
        choices=("oneshot", "continuous", "stop"),
    )
    parser.add_argument("--command-id", required=True)
    return parser


def _read_invocation(stream: TextIO) -> object:
    content = stream.read(MAX_INPUT_BYTES + 1)
    if len(content.encode("utf-8")) > MAX_INPUT_BYTES:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeError):
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (
        SOURCE_RE.fullmatch(args.source_sha) is None
        or IMAGE_RE.fullmatch(args.image_digest) is None
        or ACTIVATION_RE.fullmatch(args.activation_id) is None
        or COMMAND_RE.fullmatch(args.command_id) is None
    ):
        print("shadow diagnostic setup failed", file=sys.stderr)
        return 2
    diagnostic = build_diagnostic(
        _read_invocation(sys.stdin),
        source_sha=args.source_sha,
        image_digest=args.image_digest,
        activation_id=args.activation_id,
        desired_state=args.desired_state,
        command_id=args.command_id,
    )
    print(json.dumps(diagnostic, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
