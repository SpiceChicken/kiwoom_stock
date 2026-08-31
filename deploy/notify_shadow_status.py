#!/usr/bin/env python3
"""Send one fixed, redacted shadow control-plane status to Slack."""

from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import math
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

try:
    from .shadow_schedule_observation import (
        ObservationError,
        load_observation,
    )
except ImportError:
    from shadow_schedule_observation import (  # type: ignore[no-redef]
        ObservationError,
        load_observation,
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
MAX_LEGACY_CONFIG_BYTES = 65_536
MAX_RESPONSE_BYTES = 64
PHYSICAL_STATE_CATEGORY = "physical_state_validation_error"
STOP_TARGET_ABSENT_CATEGORY = "stop_target_absent"
MARKET_DATA_CATEGORY = "market_data_collection_error"
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
SAFE_FAILURE_CATEGORIES = {
    "image_pull_no_space",
    "image_pull_auth",
    "image_pull_not_found",
    "image_pull_network",
    "image_pull_failed",
    PHYSICAL_STATE_CATEGORY,
    STOP_TARGET_ABSENT_CATEGORY,
    MARKET_DATA_CATEGORY,
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


def _reject_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_non_json_constant(value: str) -> object:
    del value
    raise ValueError("non-JSON constant")


def _parse_finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("non-finite JSON number")
    return result


def _legacy_webhook() -> object:
    raw = os.environ.get("CONFIG_JSON")
    if raw is None:
        raise SlackStatusError("webhook_invalid")
    try:
        encoded = raw.encode("utf-8", errors="strict")
    except UnicodeError:
        raise SlackStatusError("webhook_invalid") from None
    if len(encoded) > MAX_LEGACY_CONFIG_BYTES:
        raise SlackStatusError("webhook_invalid")
    try:
        value = json.loads(
            encoded.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, TypeError, ValueError):
        raise SlackStatusError("webhook_invalid") from None
    if not isinstance(value, dict) or "webhook_url" not in value:
        raise SlackStatusError("webhook_invalid")
    return value["webhook_url"]


def resolve_webhook() -> str:
    dedicated = os.environ.get("KIWOOM_SHADOW_SLACK_WEBHOOK_URL")
    if dedicated not in (None, ""):
        return validate_webhook(dedicated)
    return validate_webhook(_legacy_webhook())


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
    if any(
        ord(character) <= 0x1F or ord(character) == 0x7F
        for character in value
    ):
        raise SlackStatusError("webhook_invalid")
    try:
        encoded = value.encode("utf-8", errors="strict")
        parsed = urlsplit(value)
        port = parsed.port
        hostname = parsed.hostname
    except (UnicodeError, ValueError):
        raise SlackStatusError("webhook_invalid") from None
    if len(encoded) > 1024:
        raise SlackStatusError("webhook_invalid")
    if (
        parsed.scheme != "https"
        or hostname != "hooks.slack.com"
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
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_constant,
            parse_float=_parse_finite_float,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _is_optional_int(value: object) -> bool:
    return value is None or type(value) is int


def _is_optional_nonnegative_int(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


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
        or type(value.get("ssm_response_code")) is not int
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
    required_keys = {
        "schema_version", "source_sha", "image_digest", "activation_id",
        "desired_state", "command_id", "ssm_status", "ssm_response_code",
        "stdout_bytes", "stderr_bytes", "failure_category", "terminal",
    }
    if (
        set(value) not in (required_keys, required_keys | {"market_data_failure"})
        or type(value.get("schema_version")) is not int
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
        or value.get("ssm_status") not in {
            None, "Success", "Failed", "Cancelled", "TimedOut",
        }
        or not _is_optional_int(value.get("ssm_response_code"))
        or not _is_optional_nonnegative_int(value.get("stdout_bytes"))
        or not _is_optional_nonnegative_int(value.get("stderr_bytes"))
        or not (
            value.get("terminal") is None
            or isinstance(value.get("terminal"), Mapping)
        )
        or value.get("failure_category") not in SAFE_FAILURE_CATEGORIES
        or not _valid_market_data_failure(value.get("market_data_failure"))
    ):
        return None
    category = value["failure_category"]
    if category == STOP_TARGET_ABSENT_CATEGORY and desired_state != "stop":
        return None
    if desired_state == "stop" and category == STOP_TARGET_ABSENT_CATEGORY:
        return (
            "[KIWOOM SHADOW] STOP TARGET ABSENT | action=stop | "
            f"category={category} | activation={activation_id} | "
            f"source={source_sha[:12]} | "
            "account/order/revoke=disabled | live-trading=disabled"
        )
    return (
        "[KIWOOM SHADOW] ACTION FAILED | "
        f"action={desired_state} | category={category} | "
        f"activation={activation_id} | source={source_sha[:12]} | "
        "account/order/revoke=disabled | live-trading=disabled"
    )


def _valid_market_data_failure(value: object) -> bool:
    if value is None:
        return True
    return (
        isinstance(value, Mapping)
        and set(value) == {"kind", "operation"}
        and isinstance(value.get("kind"), str)
        and value.get("kind") in SAFE_MARKET_DATA_FAILURE_KINDS
        and isinstance(value.get("operation"), str)
        and value.get("operation") in SAFE_MARKET_DATA_FAILURE_OPERATIONS
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
    diagnostic = _load_json(diagnostic_path)
    return build_message_values(
        evidence=evidence,
        diagnostic=diagnostic,
        source_sha=source_sha,
        image_digest=image_digest,
        activation_id=activation_id,
        desired_state=desired_state,
    )


def build_message_values(
    *,
    evidence: Mapping[str, object] | None,
    diagnostic: Mapping[str, object] | None,
    source_sha: str,
    image_digest: str,
    activation_id: str,
    desired_state: str,
) -> tuple[str, str]:
    """Build the fixed message from already strict-decoded artifacts."""

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


def _schedule_suffix(
    path: Path | None,
    *,
    desired_state: str,
    expected_run_id: str | None,
    expected_cron: str | None,
) -> tuple[str, str]:
    if path is None:
        return "", "n-a"
    if expected_run_id is None or expected_cron is None:
        return "", "invalid"
    try:
        observation = load_observation(
            path,
            desired_state=desired_state,
            expected_run_id=expected_run_id,
            expected_cron=expected_cron,
        )
    except ObservationError:
        return "", "invalid"
    delay = observation["total_start_delay_seconds"]
    return f" | schedule_delay={delay}s", "accepted"


def deliver(
    webhook: str,
    message: str,
    *,
    opener: Callable[..., ResponsePort] | None = None,
) -> None:
    payload = json.dumps(
        {"text": message}, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    try:
        request = Request(
            webhook,
            data=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
    except (HTTPException, UnicodeError, ValueError):
        raise SlackStatusError("webhook_invalid") from None
    if opener is None:
        try:
            context = ssl.create_default_context()
            client = build_opener(NoRedirect(), HTTPSHandler(context=context))
            opener = client.open
        except (HTTPException, OSError, ssl.SSLError):
            raise SlackStatusError("slack_network_error") from None
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
    except (HTTPException, URLError, TimeoutError, OSError, ssl.SSLError):
        raise SlackStatusError("slack_network_error") from None


def _receipt(
    *, source_sha: str, activation_id: str, desired_state: str,
    status: str, category: str, schedule_observation: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "event": "shadow-status-notification",
        "source_sha": source_sha,
        "activation_id": activation_id,
        "desired_state": desired_state,
        "delivery_status": status,
        "category": category,
        "schedule_observation": schedule_observation,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-webhook", action="store_true")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--diagnostic", type=Path)
    parser.add_argument("--schedule-observation", type=Path)
    parser.add_argument("--expected-run-id")
    parser.add_argument("--expected-cron")
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
    desired_state_for_observation = (
        args.desired_state if isinstance(args.desired_state, str) else ""
    )
    schedule_suffix, schedule_observation = _schedule_suffix(
        args.schedule_observation,
        desired_state=desired_state_for_observation,
        expected_run_id=args.expected_run_id,
        expected_cron=args.expected_cron,
    )
    try:
        webhook = resolve_webhook()
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
        message += schedule_suffix
        deliver(webhook, message)
        receipt = _receipt(
            source_sha=args.source_sha,
            activation_id=args.activation_id,
            desired_state=args.desired_state,
            status="DELIVERED",
            category=category,
            schedule_observation=schedule_observation,
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
            schedule_observation=schedule_observation,
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
