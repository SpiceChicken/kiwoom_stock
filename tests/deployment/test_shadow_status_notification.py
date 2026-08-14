"""Tests for the isolated Slack shadow status boundary."""

import json
from pathlib import Path

import pytest

from deploy.notify_shadow_status import (
    SlackStatusError,
    build_message,
    deliver,
    validate_webhook,
)


SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "slack-status-test"
COMMAND_ID = "00000000-0000-0000-0000-000000000001"
WEBHOOK = "https://hooks.slack.com/services/T123456/B123456/" + "x" * 24


def _success():
    return {
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "build_run_id": "123",
        "activation_id": ACTIVATION_ID,
        "desired_state": "continuous",
        "command_id": COMMAND_ID,
        "document_version": "3",
        "worker_sha256": "c" * 64,
        "validator_sha256": "d" * 64,
        "shadow_document_sha256": "e" * 64,
        "runtime_status": "PASS",
        "cycles": 1,
        "http_attempts": 6,
        "first_cycle_start_elapsed_seconds": None,
        "second_cycle_start_elapsed_seconds": None,
        "second_cycle_interval_seconds": None,
        "minimum_cycle_interval_seconds": None,
        "db_reopens": 0,
        "database": True,
        "side_effects": {
            "orders": False,
            "account": False,
            "revoke": False,
            "database": True,
            "notifications": False,
            "reports": False,
            "s3": False,
        },
        "ssm_status": "Success",
        "ssm_response_code": 0,
    }


def _diagnostic():
    return {
        "schema_version": 1,
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "activation_id": ACTIVATION_ID,
        "desired_state": "continuous",
        "command_id": COMMAND_ID,
        "ssm_status": "Failed",
        "ssm_response_code": 1,
        "stdout_bytes": 0,
        "stderr_bytes": 42,
        "failure_category": "terminal_evidence_missing",
        "terminal": None,
    }


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_webhook_is_exactly_slack_https_without_redirectable_components():
    assert validate_webhook(WEBHOOK) == WEBHOOK
    for value in (
        None,
        "",
        "http://hooks.slack.com/services/T123456/B123456/" + "x" * 24,
        "https://example.com/services/T123456/B123456/" + "x" * 24,
        WEBHOOK + "?redirect=https://example.invalid",
        "https://hooks.slack.com.evil.invalid/services/T123456/B123456/"
        + "x" * 24,
    ):
        with pytest.raises(SlackStatusError, match="webhook_invalid"):
            validate_webhook(value)


def test_success_message_uses_only_accepted_summary(tmp_path):
    evidence = tmp_path / "evidence.json"
    diagnostic = tmp_path / "diagnostic.json"
    _write(evidence, _success())
    _write(diagnostic, _diagnostic())
    category, message = build_message(
        evidence_path=evidence,
        diagnostic_path=diagnostic,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    )
    assert category == "runtime_accepted"
    assert "CONTINUOUS PASS" in message
    assert SOURCE_SHA[:12] in message
    assert SOURCE_SHA not in message
    assert "live-trading=disabled" in message


def test_rejected_runtime_uses_only_allowlisted_diagnostic_category(tmp_path):
    evidence = tmp_path / "missing-evidence.json"
    diagnostic = tmp_path / "diagnostic.json"
    _write(diagnostic, _diagnostic())
    category, message = build_message(
        evidence_path=evidence,
        diagnostic_path=diagnostic,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    )
    assert category == "runtime_rejected"
    assert "terminal_evidence_missing" in message
    assert "Failed" not in message


def test_wrong_tuple_or_unsafe_side_effect_artifact_fails_closed(tmp_path):
    evidence = tmp_path / "evidence.json"
    diagnostic = tmp_path / "diagnostic.json"
    unsafe = _success()
    unsafe["side_effects"]["orders"] = True
    _write(evidence, unsafe)
    _write(diagnostic, {**_diagnostic(), "source_sha": "c" * 40})
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )


class FakeResponse:
    def __init__(self, status=200, body=b"ok"):
        self.status = status
        self.body = body

    def read(self, amount=-1):
        return self.body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def test_delivery_is_one_bounded_json_post_without_webhook_reflection():
    requests = []

    def open_request(request, timeout):
        requests.append((request, timeout))
        return FakeResponse()

    deliver(WEBHOOK, "fixed safe message", opener=open_request)
    assert len(requests) == 1
    request, timeout = requests[0]
    assert timeout == 5.0
    assert request.full_url == WEBHOOK
    assert request.method == "POST"
    assert json.loads(request.data) == {"text": "fixed safe message"}


@pytest.mark.parametrize(
    ("status", "body"),
    [(204, b""), (200, b"not-ok"), (200, b"x" * 65)],
)
def test_delivery_rejects_non_exact_slack_ack(status, body):
    def open_request(_request, timeout):
        del timeout
        return FakeResponse(status=status, body=body)

    with pytest.raises(SlackStatusError, match="slack_response_rejected"):
        deliver(WEBHOOK, "fixed safe message", opener=open_request)
