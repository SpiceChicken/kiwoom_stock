#!/usr/bin/env python3
"""Send one fixed, redacted shadow control-plane status to Slack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import ssl
import sys
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    Request,
    build_opener,
)


SOURCE_RE = re.compile(r"[0-9a-f]{40}")
IMAGE_RE = re.compile(
    r"ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}"
)
ACTIVATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
COMMAND_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
SLACK_PATH_RE = re.compile(
    r"/services/[A-Za-z0-9_-]{6,128}/[A-Za-z0-9_-]{6,128}/"
    r"[A-Za-z0-9_-]{16,256}"
)
MAX_ARTIFACT_BYTES = 65_536
MAX_RESPONSE_BYTES = 64
SAFE_FAILURE_CATEGORIES = {
    "container_absent",
    "container_identity_mismatch",
    "graceful_stop_timeout",
    "runtime_terminal_nonoperational",
    "terminal_evidence_missing",
    "runtime_exit_nonzero",
    "terminal_transition_mismatch",
    "container_removal_failed",
    "success_without_accepted_runtime_evidence",
    "ssm_failed_unclassified",
    "ssm_cancelled_unclassified",
    "ssm_timedout_unclassified",
    "invocation_envelope_invalid",
}
SAFE_RUNTIME_STATUSES = {"PASS", "CLOSED", "STOPPED", "DEADLINE"}


class SlackStatusError(RuntimeError):
    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


class ResponsePort(Protocol):
    status: int

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "ResponsePort": ...

    def __exit__(self, *args: object) -> None: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        return None


def validate_webhook(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise SlackStatusError("webhook_invalid")
    if len(value.encode("utf-8")) > 1024:
        raise SlackStatusError("webhook_invalid")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError:
        raise SlackStatusError("webhook_invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "hooks.slack.com"
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or SLACK_PATH_RE.fullmatch(parsed.path) is None
    ):
        raise SlackStatusError("webhook_invalid")
    return value


def _load_json(path: Path) -> Mapping[str, object] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_ARTIFACT_BYTES:
        return None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _matching_tuple(
    value: Mapping[str, object], *, source_sha: str, image_digest: str,
    activation_id: str, desired_state: str,
) -> bool:
    return (
        value.get("source_sha") == source_sha
        and value.get("image_digest") == image_digest
        and value.get("activation_id") == activation_id
        and value.get("desired_state") == desired_state
    )


def _success_message(
    value: Mapping[str, object], *, source_sha: str, image_digest: str,
    activation_id: str, desired_state: str,
) -> str | None:
    if not _matching_tuple(
        value,
        source_sha=source_sha,
        image_digest=image_digest,
        activation_id=activation_id,
        desired_state=desired_state,
    ):
        return None
    status = value.get("runtime_status")
    cycles = value.get("cycles")
    command_id = value.get("command_id")
    side = value.get("side_effects")
    if (
        status not in SAFE_RUNTIME_STATUSES
        or type(cycles) is not int
        or cycles < 0
        or not isinstance(command_id, str)
        or COMMAND_RE.fullmatch(command_id) is None
        or value.get("ssm_status") != "Success"
        or value.get("ssm_response_code") != 0
        or not isinstance(side, Mapping)
        or set(side) != {
            "orders", "account", "revoke", "database", "notifications",
            "reports", "s3",
        }
        or any(
            side.get(name) is not False
            for name in (
                "orders", "account", "revoke", "notifications", "reports", "s3"
            )
        )
    ):
        return None
    return (
        "[KIWOOM SHADOW] "
        f"{desired_state.upper()} {status} | activation={activation_id} | "
        f"source={source_sha[:12]} | cycles={cycles} | "
        "account/order/revoke=disabled | live-trading=disabled"
    )


def _failure_message(
    value: Mapping[str, object], *, source_sha: str, image_digest: str,
    activation_id: str, desired_state: str,
) -> str | None:
    if (
        set(value) != {
            "schema_version", "source_sha", "image_digest", "activation_id",
            "desired_state", "command_id", "ssm_status", "ssm_response_code",
            "stdout_bytes", "stderr_bytes", "failure_category", "terminal",
        }
        or value.get("schema_version") != 1
        or not _matching_tuple(
            value,
            source_sha=source_sha,
            image_digest=image_digest,
            activation_id=activation_id,
            desired_state=desired_state,
        )
        or not isinstance(value.get("command_id"), str)
        or COMMAND_RE.fullmatch(str(value["command_id"])) is None
        or value.get("failure_category") not in SAFE_FAILURE_CATEGORIES
    ):
        return None
    category = value["failure_category"]
    return (
        "[KIWOOM SHADOW] ACTION FAILED | "
        f"action={desired_state} | category={category} | "
        f"activation={activation_id} | source={source_sha[:12]} | "
        "account/order/revoke=disabled | live-trading=disabled"
    )


def build_message(
    *,
    evidence_path: Path,
    diagnostic_path: Path,
    source_sha: str,
    image_digest: str,
    activation_id: str,
    desired_state: str,
) -> tuple[str, str]:
    evidence = _load_json(evidence_path)
    if evidence is not None:
        message = _success_message(
            evidence,
            source_sha=source_sha,
            image_digest=image_digest,
            activation_id=activation_id,
            desired_state=desired_state,
        )
        if message is not None:
            return "runtime_accepted", message
    diagnostic = _load_json(diagnostic_path)
    if diagnostic is not None:
        message = _failure_message(
            diagnostic,
            source_sha=source_sha,
            image_digest=image_digest,
            activation_id=activation_id,
            desired_state=desired_state,
        )
        if message is not None:
            return "runtime_rejected", message
    raise SlackStatusError("status_artifact_invalid")


def deliver(
    webhook: str,
    message: str,
    *,
    opener: Callable[..., ResponsePort] | None = None,
) -> None:
    payload = json.dumps(
        {"text": message}, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    request = Request(
        webhook,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    if opener is None:
        context = ssl.create_default_context()
        client = build_opener(NoRedirect(), HTTPSHandler(context=context))
        opener = client.open
    try:
        with opener(request, timeout=5.0) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if response.status != 200 or body != b"ok":
                raise SlackStatusError("slack_response_rejected")
    except SlackStatusError:
        raise
    except HTTPError as error:
        category = (
            "slack_redirect_rejected"
            if 300 <= error.code < 400
            else "slack_http_error"
        )
        raise SlackStatusError(category) from None
    except (URLError, TimeoutError, OSError, ssl.SSLError):
        raise SlackStatusError("slack_network_error") from None


def _receipt(
    *, source_sha: str, activation_id: str, desired_state: str,
    status: str, category: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "event": "shadow-status-notification",
        "source_sha": source_sha,
        "activation_id": activation_id,
        "desired_state": desired_state,
        "delivery_status": status,
        "category": category,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-webhook", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--diagnostic", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--source-sha")
    parser.add_argument("--image-digest")
    parser.add_argument("--activation-id")
    parser.add_argument(
        "--desired-state", choices=("oneshot", "continuous", "stop")
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        webhook = validate_webhook(
            os.environ.get("KIWOOM_SHADOW_SLACK_WEBHOOK_URL")
        )
        if args.check_webhook:
            return 0
        if (
            args.evidence is None
            or args.diagnostic is None
            or args.receipt is None
            or not isinstance(args.source_sha, str)
            or SOURCE_RE.fullmatch(args.source_sha) is None
            or not isinstance(args.image_digest, str)
            or IMAGE_RE.fullmatch(args.image_digest) is None
            or not isinstance(args.activation_id, str)
            or ACTIVATION_RE.fullmatch(args.activation_id) is None
            or args.desired_state is None
        ):
            raise SlackStatusError("notification_setup_invalid")
        try:
            category, message = build_message(
                evidence_path=args.evidence,
                diagnostic_path=args.diagnostic,
                source_sha=args.source_sha,
                image_digest=args.image_digest,
                activation_id=args.activation_id,
                desired_state=args.desired_state,
            )
        except SlackStatusError as error:
            if error.category != "status_artifact_invalid":
                raise
            category = "control_plane_evidence_missing"
            message = (
                "[KIWOOM SHADOW] CONTROL PLANE FAILED | "
                f"action={args.desired_state} | activation={args.activation_id} | "
                f"source={args.source_sha[:12]} | "
                "account/order/revoke=disabled | live-trading=disabled"
            )
        deliver(webhook, message)
        receipt = _receipt(
            source_sha=args.source_sha,
            activation_id=args.activation_id,
            desired_state=args.desired_state,
            status="DELIVERED",
            category=category,
        )
    except SlackStatusError as error:
        source_sha = args.source_sha if isinstance(args.source_sha, str) else ""
        activation_id = (
            args.activation_id if isinstance(args.activation_id, str) else ""
        )
        desired_state = (
            args.desired_state if isinstance(args.desired_state, str) else ""
        )
        receipt = _receipt(
            source_sha=source_sha,
            activation_id=activation_id,
            desired_state=desired_state,
            status="FAILED",
            category=error.category,
        )
        if args.receipt is not None:
            args.receipt.write_text(
                json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
        print(f"shadow status notification failed: {error.category}", file=sys.stderr)
        return 1
    args.receipt.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
