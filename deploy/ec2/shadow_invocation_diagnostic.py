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
    SOURCE_RE,
    EvidenceError,
    _json_lines,
    validate_diagnostic_terminal,
)


COMMAND_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SSM_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
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
    ("shadow container is absent", "container_absent"),
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


def _classification(status: object, stderr: str | None) -> str:
    if stderr is not None:
        for marker, category in SAFE_FAILURE_MARKERS:
            if marker in stderr:
                return category
    if status == "Success":
        return "success_without_accepted_runtime_evidence"
    if status in SSM_STATUSES:
        return f"ssm_{str(status).lower()}_unclassified"
    return "invocation_envelope_invalid"


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
            "failure_category": _classification(status, stderr),
        }
    )
    if stdout is None:
        return result
    try:
        terminal = validate_diagnostic_terminal(
            _json_lines(stdout),
            source_sha=source_sha,
            image_digest=image_digest,
            activation_id=activation_id,
        )
    except EvidenceError:
        return result
    result["terminal"] = {
        key: terminal.get(key)
        for key in (
            "status",
            "reason",
            "error_type",
            "cycles",
            "db_reopens",
            "resources_closed",
            "elapsed_seconds",
        )
    }
    result["failure_category"] = "runtime_terminal_nonoperational"
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
