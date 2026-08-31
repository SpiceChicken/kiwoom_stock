"""Tests for the isolated Slack shadow status boundary."""

from http.client import InvalidURL
import json
from pathlib import Path

import pytest

import deploy.notify_shadow_status as notification
from deploy.notify_shadow_status import (
    SlackStatusError,
    build_message,
    deliver,
    main,
    resolve_webhook,
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


def _observation():
    return {
        "schema_version": 1,
        "run_id": "123",
        "cron": "50 23 * * 0-4",
        "desired_state": "continuous",
        "expected_at_utc": "2026-08-20T23:50:00Z",
        "created_at_utc": "2026-08-20T23:53:00Z",
        "run_started_at_utc": "2026-08-20T23:54:00Z",
        "delivery_delay_seconds": 180,
        "queue_delay_seconds": 60,
        "total_start_delay_seconds": 240,
    }


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def _legacy_config_of_size(size: int, fill: str = "x") -> str:
    base = json.dumps(
        {"webhook_url": WEBHOOK, "padding": ""},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    remaining = size - len(base.encode("utf-8"))
    assert remaining >= 0
    fill_bytes = len(fill.encode("utf-8"))
    padding = fill * (remaining // fill_bytes)
    trailing = " " * (remaining % fill_bytes)
    config = base.replace('"padding":""', f'"padding":"{padding}"') + trailing
    assert len(config.encode("utf-8")) == size
    return config


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


@pytest.mark.parametrize(
    "control", [*(chr(value) for value in range(0x20)), chr(0x7F)]
)
def test_webhook_rejects_every_c0_control_and_del(control):
    embedded = WEBHOOK.replace("services", f"serv{control}ices")
    with pytest.raises(SlackStatusError, match="webhook_invalid"):
        validate_webhook(embedded)


@pytest.mark.parametrize("control", ["\r", "\n", "\t"])
def test_legacy_webhook_rejects_embedded_cr_lf_and_tab(monkeypatch, control):
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv(
        "CONFIG_JSON",
        json.dumps({
            "webhook_url": WEBHOOK.replace(
                "services", f"serv{control}ices"
            ),
        }),
    )
    with pytest.raises(SlackStatusError, match="webhook_invalid"):
        resolve_webhook()


def test_dedicated_webhook_has_precedence(monkeypatch):
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("CONFIG_JSON", '{"webhook_url":"https://bad.invalid"}')
    assert resolve_webhook() == WEBHOOK


def test_invalid_dedicated_webhook_does_not_fallback(monkeypatch):
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", "not-a-webhook")
    monkeypatch.setenv("CONFIG_JSON", json.dumps({"webhook_url": WEBHOOK}))
    with pytest.raises(SlackStatusError, match="webhook_invalid"):
        resolve_webhook()


def test_empty_dedicated_webhook_uses_legacy_fallback(monkeypatch):
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", "")
    monkeypatch.setenv("CONFIG_JSON", json.dumps({"webhook_url": WEBHOOK}))
    assert resolve_webhook() == WEBHOOK


@pytest.mark.parametrize(
    "config",
    [
        json.dumps({"webhook_url": WEBHOOK}),
        '{"webhook_url":"' + WEBHOOK + '","webhook_url":"' + WEBHOOK + '"}',
        "[]",
        '{"other":"value"}',
        '{"webhook_url":123}',
        '{"webhook_url":null}',
        '{"webhook_url":"' + WEBHOOK,
        '{"webhook_url":NaN}',
        '{"webhook_url":Infinity}',
        '{"webhook_url":-Infinity}',
        '{"webhook_url":"\\ud800"}',
    ],
)
def test_legacy_config_is_strict_and_fail_closed(monkeypatch, config):
    monkeypatch.delenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("CONFIG_JSON", config)
    if config == json.dumps({"webhook_url": WEBHOOK}):
        assert resolve_webhook() == WEBHOOK
    else:
        with pytest.raises(SlackStatusError, match="webhook_invalid"):
            resolve_webhook()


def test_legacy_config_rejects_oversize_and_empty(monkeypatch):
    monkeypatch.delenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", raising=False)
    for config in ("", " " * 65_537):
        monkeypatch.setenv("CONFIG_JSON", config)
        with pytest.raises(SlackStatusError, match="webhook_invalid"):
            resolve_webhook()


@pytest.mark.parametrize("fill", ["x", "한"])
def test_legacy_config_enforces_exact_utf8_byte_boundary(monkeypatch, fill):
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", "")
    exact = _legacy_config_of_size(65_536, fill)
    monkeypatch.setenv("CONFIG_JSON", exact)
    assert resolve_webhook() == WEBHOOK
    monkeypatch.setenv("CONFIG_JSON", exact + " ")
    with pytest.raises(SlackStatusError, match="webhook_invalid"):
        resolve_webhook()


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


def test_shadow_status_messages_are_exact_control_plane_goldens():
    assert notification._success_message(
        _success(),
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) == (
        "[KIWOOM SHADOW] CONTINUOUS PASS | activation=slack-status-test | "
        "source=aaaaaaaaaaaa | cycles=1 | "
        "account/order/revoke=disabled | live-trading=disabled"
    )
    assert notification._failure_message(
        _diagnostic(),
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) == (
        "[KIWOOM SHADOW] ACTION FAILED | action=continuous | "
        "category=terminal_evidence_missing | activation=slack-status-test | "
        "source=aaaaaaaaaaaa | account/order/revoke=disabled | "
        "live-trading=disabled"
    )


def test_stop_target_absent_message_is_cause_neutral_exact_golden():
    diagnostic = {
        **_diagnostic(),
        "desired_state": "stop",
        "failure_category": "stop_target_absent",
    }
    assert notification._failure_message(
        diagnostic,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="stop",
    ) == (
        "[KIWOOM SHADOW] STOP TARGET ABSENT | action=stop | "
        "category=stop_target_absent | activation=slack-status-test | "
        "source=aaaaaaaaaaaa | account/order/revoke=disabled | "
        "live-trading=disabled"
    )


def test_retired_legacy_container_absent_category_is_rejected():
    diagnostic = {
        **_diagnostic(),
        "desired_state": "stop",
        "failure_category": "container_absent",
    }
    assert notification._failure_message(
        diagnostic,
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="stop",
    ) is None


def test_status_artifacts_reject_duplicate_keys_and_non_json_constants(
    tmp_path,
):
    evidence = tmp_path / "evidence.json"
    diagnostic = tmp_path / "diagnostic.json"

    success = json.dumps(_success(), separators=(",", ":"))
    evidence.write_text(
        success.replace(
            '{"source_sha":',
            '{"source_sha":"' + "c" * 40 + '","source_sha":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )

    evidence.write_text(
        success.replace('"http_attempts":6', '"http_attempts":NaN'),
        encoding="utf-8",
    )
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )

    evidence.write_text(
        success.replace('"http_attempts":6', '"http_attempts":1e999'),
        encoding="utf-8",
    )
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )

    evidence.unlink()
    failure = json.dumps(_diagnostic(), separators=(",", ":"))
    diagnostic.write_text(
        failure.replace(
            '"failure_category":',
            '"failure_category":"unsafe","failure_category":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )

    diagnostic.write_text(
        failure.replace('"terminal":null', '"terminal":NaN'),
        encoding="utf-8",
    )
    with pytest.raises(SlackStatusError, match="status_artifact_invalid"):
        build_message(
            evidence_path=evidence,
            diagnostic_path=diagnostic,
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
            desired_state="continuous",
        )


def test_status_artifact_integer_fields_reject_boolean_aliases():
    assert notification._success_message(
        {**_success(), "ssm_response_code": False},
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) is None
    assert notification._failure_message(
        {**_diagnostic(), "schema_version": True},
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) is None
    assert notification._failure_message(
        {**_diagnostic(), "stdout_bytes": True},
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) is None


def test_stop_target_absent_is_rejected_for_non_stop_action():
    assert notification._failure_message(
        {**_diagnostic(), "failure_category": "stop_target_absent"},
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    ) is None


def test_physical_state_category_is_allowlisted_without_error_reflection():
    message = notification._failure_message(
        {
            **_diagnostic(),
            "failure_category": "physical_state_validation_error",
        },
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    )
    assert message is not None
    assert "category=physical_state_validation_error" in message
    assert "error_type" not in message


def test_market_data_diagnostic_labels_are_accepted_without_message_reflection():
    message = notification._failure_message(
        {
            **_diagnostic(),
            "failure_category": "market_data_collection_error",
            "market_data_failure": {
                "kind": "timeout", "operation": "order_book",
            },
        },
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        desired_state="continuous",
    )
    assert message is not None
    assert "category=market_data_collection_error" in message
    assert "order_book" not in message
    assert "timeout" not in message


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


def test_schedule_suffix_is_exact_and_invalid_observation_is_not_zero(tmp_path):
    observation = tmp_path / "observation.json"
    _write(observation, _observation())
    assert notification._schedule_suffix(
        observation,
        desired_state="continuous",
        expected_run_id="123",
        expected_cron="50 23 * * 0-4",
    ) == (" | schedule_delay=240s", "accepted")

    _write(observation, {**_observation(), "unsafe": "secret-body"})
    assert notification._schedule_suffix(
        observation,
        desired_state="continuous",
        expected_run_id="123",
        expected_cron="50 23 * * 0-4",
    ) == ("", "invalid")
    assert notification._schedule_suffix(
        None,
        desired_state="continuous",
        expected_run_id=None,
        expected_cron=None,
    ) == ("", "n-a")


@pytest.mark.parametrize(
    ("expected_run_id", "expected_cron"),
    [
        (None, "50 23 * * 0-4"),
        ("123", None),
        ("124", "50 23 * * 0-4"),
        ("123", "35 6 * * 1-5"),
    ],
)
def test_schedule_suffix_requires_current_run_and_cron_binding(
    tmp_path, expected_run_id, expected_cron,
):
    observation = tmp_path / "observation.json"
    _write(observation, _observation())
    assert notification._schedule_suffix(
        observation,
        desired_state="continuous",
        expected_run_id=expected_run_id,
        expected_cron=expected_cron,
    ) == ("", "invalid")


@pytest.mark.parametrize(
    ("observation_value", "expected_status", "expected_suffix"),
    [
        (_observation(), "accepted", " | schedule_delay=240s"),
        ({**_observation(), "total_start_delay_seconds": 0}, "invalid", ""),
    ],
)
def test_main_records_schedule_observation_without_blocking_status_message(
    monkeypatch, tmp_path, observation_value, expected_status, expected_suffix,
):
    evidence = tmp_path / "evidence.json"
    diagnostic = tmp_path / "diagnostic.json"
    observation = tmp_path / "observation.json"
    receipt = tmp_path / "receipt.json"
    _write(evidence, _success())
    _write(diagnostic, _diagnostic())
    _write(observation, observation_value)
    delivered = []
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(
        notification,
        "deliver",
        lambda webhook, message: delivered.append((webhook, message)),
    )

    result = main([
        "--evidence", str(evidence),
        "--diagnostic", str(diagnostic),
        "--schedule-observation", str(observation),
        "--expected-run-id", "123",
        "--expected-cron", "50 23 * * 0-4",
        "--receipt", str(receipt),
        "--source-sha", SOURCE_SHA,
        "--image-digest", IMAGE,
        "--activation-id", ACTIVATION_ID,
        "--desired-state", "continuous",
    ])

    assert result == 0
    assert len(delivered) == 1
    assert delivered[0][1].endswith(
        expected_suffix or "live-trading=disabled"
    )
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_value["schema_version"] == 2
    assert set(receipt_value) == {
        "schema_version",
        "event",
        "source_sha",
        "activation_id",
        "desired_state",
        "delivery_status",
        "category",
        "schedule_observation",
    }
    assert receipt_value["schedule_observation"] == expected_status


def test_manual_notification_receipt_marks_schedule_observation_not_applicable(
    monkeypatch, tmp_path,
):
    evidence = tmp_path / "evidence.json"
    diagnostic = tmp_path / "diagnostic.json"
    receipt = tmp_path / "receipt.json"
    _write(evidence, _success())
    _write(diagnostic, _diagnostic())
    delivered = []
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(
        notification,
        "deliver",
        lambda webhook, message: delivered.append((webhook, message)),
    )
    result = main([
        "--evidence", str(evidence),
        "--diagnostic", str(diagnostic),
        "--receipt", str(receipt),
        "--source-sha", SOURCE_SHA,
        "--image-digest", IMAGE,
        "--activation-id", ACTIVATION_ID,
        "--desired-state", "continuous",
    ])
    assert result == 0
    assert "schedule_delay=" not in delivered[0][1]
    receipt_value = json.loads(receipt.read_text(encoding="utf-8"))
    assert receipt_value["schema_version"] == 2
    assert receipt_value["schedule_observation"] == "n-a"


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


def test_request_construction_error_is_value_free(monkeypatch):
    sentinel = "WEBHOOK_TOKEN_MUST_NOT_REFLECT"

    def reject_request(*_args, **_kwargs):
        raise ValueError(sentinel)

    monkeypatch.setattr(notification, "Request", reject_request)
    with pytest.raises(SlackStatusError) as raised:
        deliver(WEBHOOK, "fixed safe message")
    assert str(raised.value) == "webhook_invalid"
    assert sentinel not in str(raised.value)


def test_http_client_error_is_value_free():
    sentinel = "WEBHOOK_TOKEN_MUST_NOT_REFLECT"

    def reject_open(_request, timeout):
        del timeout
        raise InvalidURL(sentinel)

    with pytest.raises(SlackStatusError) as raised:
        deliver(WEBHOOK, "fixed safe message", opener=reject_open)
    assert str(raised.value) == "slack_network_error"
    assert sentinel not in str(raised.value)


def test_main_failure_receipt_and_logs_never_reflect_webhook(
    monkeypatch, tmp_path, capsys,
):
    sentinel = "WEBHOOK_TOKEN_MUST_NOT_REFLECT"
    receipt = tmp_path / "receipt.json"
    monkeypatch.setenv("KIWOOM_SHADOW_SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setenv("CONFIG_JSON", json.dumps({"webhook_url": WEBHOOK}))

    class RejectingClient:
        def open(self, _request, timeout):
            del timeout
            raise InvalidURL(sentinel)

    monkeypatch.setattr(
        notification, "build_opener", lambda *_args: RejectingClient()
    )

    result = main([
        "--evidence", str(tmp_path / "evidence.json"),
        "--diagnostic", str(tmp_path / "diagnostic.json"),
        "--receipt", str(receipt),
        "--source-sha", SOURCE_SHA,
        "--image-digest", IMAGE,
        "--activation-id", ACTIVATION_ID,
        "--desired-state", "continuous",
    ])

    captured = capsys.readouterr()
    receipt_text = receipt.read_text(encoding="utf-8")
    receipt_value = json.loads(receipt_text)
    assert result == 1
    assert captured.out == ""
    assert captured.err == (
        "shadow status notification failed: slack_network_error\n"
    )
    assert receipt_value["delivery_status"] == "FAILED"
    assert receipt_value["category"] == "slack_network_error"
    assert receipt_value["schema_version"] == 2
    assert all(
        sentinel not in output
        for output in (captured.out, captured.err, receipt_text)
    )
