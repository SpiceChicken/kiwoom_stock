"""Cloud submitter tests with deterministic ledger and SSM fakes."""

from pathlib import Path
import json

import pytest
from botocore.exceptions import ClientError

from deploy.shadow_cstar_contract import release_id_for
from deploy.shadow_cstar_submitter import (
    ACTIVATION_DOCUMENT_NAME,
    Boto3SsmCommandSender,
    CStarSubmitter,
    DynamoCStarLedger,
    InMemoryCStarLedger,
    SubmitterError,
)


def _payload(**updates):
    value = {
        "schema_version": 1,
        "phase": "start",
        "schedule_generation": "cstar-g000001",
        "schedule_arn": "arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/start",
        "scheduled_time": "2026-08-23T23:50:00Z",
        "execution_id": "execution-1",
        "attempt_number": "0",
    }
    value.update(updates)
    return value


def _intent():
    return {
        "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "e" * 64,
        "source_sha": "a" * 40,
        "compose_shadow_sha256": "f" * 64,
        "worker_sha256": "b" * 64,
        "validator_sha256": "c" * 64,
        "shadow_document_sha256": "d" * 64,
        "rollout_attempt_id": "rollout-1",
    }


class FakeSender:
    def __init__(self, command_id="command-1", error=None):
        self.command_id = command_id
        self.error = error
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.command_id


class FailingLedger(InMemoryCStarLedger):
    def prepare_occurrence(self, **kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "transaction cancelled",
                },
                "CancellationReasons": [
                    {"Code": "ValidationError", "Message": "invalid request"},
                    {"Code": "None"},
                ],
            },
            "TransactWriteItems",
        )


class RejectionFailingLedger(InMemoryCStarLedger):
    def record_rejection(self, **kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "TransactionCanceledException",
                    "Message": "transaction cancelled",
                },
                "CancellationReasons": [
                    {"Code": "ValidationError", "Message": "invalid rejection"},
                ],
            },
            "TransactWriteItems",
        )


def _configured(ledger=None):
    ledger = ledger or InMemoryCStarLedger()
    intent = _intent()
    release_id = release_id_for(intent)
    ledger.generations["cstar-g000001"] = {
        "schedule_arn": _payload()["schedule_arn"],
        "schedule_arns": {
            "start": _payload()["schedule_arn"],
            "stop": "arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
        },
        "protocol_sha256": "1" * 64,
    }
    ledger.active_release = {"release_id": release_id}
    ledger.releases[release_id] = intent
    return ledger, release_id


def test_start_uses_active_release_and_submits_once():
    ledger, release_id = _configured()
    sender = FakeSender()
    result = CStarSubmitter(ledger, sender).submit(_payload())
    assert result.submission_state == "SUBMITTED"
    assert result.release_id == release_id
    assert len(sender.calls) == 1
    assert ledger.occurrences[result.occurrence_id]["submission_state"] == "SUBMITTED"


def test_stop_uses_same_daily_session_and_never_reads_active_pointer():
    ledger, release_id = _configured()
    sender = FakeSender()
    start = CStarSubmitter(ledger, sender).submit(_payload())
    ledger.active_release = None
    stop = CStarSubmitter(ledger, sender).submit(
        _payload(
            phase="stop",
            schedule_arn="arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
            scheduled_time="2026-08-24T06:35:00Z",
            execution_id="stop-1",
        )
    )
    assert start.release_id == stop.release_id == release_id
    assert stop.submission_state == "SUBMITTED"
    assert len(sender.calls) == 2


def test_stop_without_daily_session_is_durable_rejection_without_ssm():
    ledger, _ = _configured()
    sender = FakeSender()
    result = CStarSubmitter(ledger, sender).submit(
        _payload(
            phase="stop",
            schedule_arn="arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
            scheduled_time="2026-08-25T06:35:00Z",
        )
    )
    assert result.submission_state == "REJECTED"
    assert result.reason == "REJECTED_NO_SESSION"
    assert sender.calls == []
    assert ledger.rejections[result.occurrence_id]["ssm_sent"] is False
    assert ledger.rejections[result.occurrence_id]["reason"] == "REJECTED_NO_SESSION"


def test_sender_failure_is_ambiguous_and_not_claimed_as_success():
    ledger, _ = _configured()
    sender = FakeSender(error=RuntimeError("network"))
    result = CStarSubmitter(ledger, sender).submit(_payload())
    assert result.submission_state == "AMBIGUOUS"
    assert ledger.occurrences[result.occurrence_id]["submission_state"] == "AMBIGUOUS"


def test_generation_schedule_arn_mismatch_rejects_without_ssm():
    ledger, _ = _configured()
    sender = FakeSender()
    result = CStarSubmitter(ledger, sender).submit(
        _payload(schedule_arn="arn:aws:scheduler:ap-northeast-2:380648615401:schedule/other")
    )
    assert result.submission_state == "REJECTED"
    assert result.reason == "STALE_GENERATION"
    assert sender.calls == []
    assert ledger.rejections[result.occurrence_id]["reason"] == "STALE_GENERATION"


def test_missing_active_release_is_durable_rejection_without_ssm():
    ledger, _ = _configured()
    ledger.active_release = None
    sender = FakeSender()
    result = CStarSubmitter(ledger, sender).submit(_payload())
    assert result.submission_state == "REJECTED"
    assert result.reason == "NO_ACTIVE_RELEASE"
    assert sender.calls == []
    assert ledger.rejections[result.occurrence_id]["reason"] == "NO_ACTIVE_RELEASE"


def test_ledger_failure_logs_bounded_cancellation_reasons(capsys):
    ledger, _ = _configured(FailingLedger())
    sender = FakeSender()

    with pytest.raises(SubmitterError, match="ledger failure"):
        CStarSubmitter(ledger, sender).submit(_payload())

    record = json.loads(capsys.readouterr().out.strip())
    assert record == {
        "cancellation_reasons": [
            {"code": "ValidationError", "message": "invalid request"},
            {"code": "None"},
        ],
        "error_code": "TransactionCanceledException",
        "error_type": "ClientError",
        "event": "cstar-ledger-failure",
        "operation": "prepare_occurrence",
    }


def test_rejection_failure_logs_bounded_cancellation_reasons(capsys):
    ledger, _ = _configured(RejectionFailingLedger())
    sender = FakeSender()

    with pytest.raises(ClientError):
        CStarSubmitter(ledger, sender).submit(
            _payload(
                phase="stop",
                schedule_arn="arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
                scheduled_time="2026-08-24T06:35:00Z",
            )
        )

    record = json.loads(capsys.readouterr().out.strip())
    assert record["event"] == "cstar-ledger-failure"
    assert record["operation"] == "record_rejection"
    assert record["error_code"] == "TransactionCanceledException"
    assert record["cancellation_reasons"] == [
        {"code": "ValidationError", "message": "invalid rejection"},
    ]


def test_dynamo_ledger_serializes_transaction_items_for_low_level_client():
    class Client:
        def __init__(self):
            self.kwargs = None

        def transact_write_items(self, **kwargs):
            self.kwargs = kwargs

    client = Client()
    ledger = DynamoCStarLedger("table", client)

    ledger._transact([
        {
            "Put": {
                "TableName": "table",
                "Item": {"PK": "key", "SK": "meta", "count": 1, "enabled": False},
                "ConditionExpression": "count = :count",
                "ExpressionAttributeValues": {":count": 1},
            }
        }
    ])

    assert client.kwargs == {
        "TransactItems": [
            {
                "Put": {
                    "TableName": "table",
                    "Item": {
                        "PK": {"S": "key"},
                        "SK": {"S": "meta"},
                        "count": {"N": "1"},
                        "enabled": {"BOOL": False},
                    },
                    "ConditionExpression": "count = :count",
                    "ExpressionAttributeValues": {":count": {"N": "1"}},
                }
            }
        ]
    }


def test_dynamo_ledger_does_not_double_serialize_resource_table_transactions(monkeypatch):
    import boto3
    from botocore.stub import Stubber

    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    table = boto3.resource("dynamodb", region_name="ap-northeast-2").Table("table")
    client = table.meta.client
    stubber = Stubber(client)
    stubber.add_response("transact_write_items", {})
    stubber.activate()
    captured = {}

    def capture(**kwargs):
        captured["params"] = kwargs["params"]

    client.meta.events.register(
        "before-parameter-build.dynamodb.TransactWriteItems",
        capture,
        unique_id="test-resource-transact-shape",
    )
    try:
        DynamoCStarLedger("table", table)._transact([
            {
                "Put": {
                    "TableName": "table",
                    "Item": {"PK": "key", "SK": "meta", "count": 1},
                    "ConditionExpression": "count = :count",
                    "ExpressionAttributeValues": {":count": 1},
                }
            },
            {
                "Update": {
                    "TableName": "table",
                    "Key": {"PK": "key", "SK": "meta"},
                    "UpdateExpression": "SET enabled = :enabled",
                    "ExpressionAttributeValues": {":enabled": False},
                }
            }
        ])
    finally:
        client.meta.events.unregister(
            "before-parameter-build.dynamodb.TransactWriteItems",
            unique_id="test-resource-transact-shape",
        )
        stubber.deactivate()

    put = captured["params"]["TransactItems"][0]["Put"]
    item = put["Item"]
    assert item == {
        "PK": {"S": "key"},
        "SK": {"S": "meta"},
        "count": {"N": "1"},
    }
    assert put["ExpressionAttributeValues"] == {":count": {"N": "1"}}
    update = captured["params"]["TransactItems"][1]["Update"]
    assert update["Key"] == {"PK": {"S": "key"}, "SK": {"S": "meta"}}
    assert update["ExpressionAttributeValues"] == {":enabled": {"BOOL": False}}


def test_dynamo_ledger_records_rejection_with_exact_key_and_no_ssm():
    class Client:
        def __init__(self):
            self.kwargs = None

        def transact_write_items(self, **kwargs):
            self.kwargs = kwargs

    client = Client()
    ledger = DynamoCStarLedger("table", client)
    payload = _payload(
        phase="stop",
        schedule_arn="arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
        scheduled_time="2026-08-24T06:35:00Z",
    )

    ledger.record_rejection(
        occurrence_id="a" * 64,
        payload=payload,
        reason="REJECTED_NO_SESSION",
        release_id=None,
    )

    item = client.kwargs["TransactItems"][0]["Put"]["Item"]
    assert item["PK"] == {"S": "REJ#" + "a" * 64}
    assert item["SK"] == {"S": "META"}
    assert item["reason"] == {"S": "REJECTED_NO_SESSION"}
    assert item["ssm_sent"] == {"BOOL": False}


def test_boto_sender_uses_only_exact_document_and_exact_instance():
    class Client:
        def __init__(self):
            self.kwargs = None

        def send_command(self, **kwargs):
            self.kwargs = kwargs
            return {"Command": {"CommandId": "command-1"}}

    client = Client()
    sender = Boto3SsmCommandSender(client)
    ledger, release_id = _configured()
    result = CStarSubmitter(ledger, sender).submit(_payload())
    assert result.submission_state == "SUBMITTED"
    assert client.kwargs["DocumentName"] == ACTIVATION_DOCUMENT_NAME
    assert client.kwargs["InstanceIds"] == ["i-0e42e09d6c087ba29"]
    assert client.kwargs["Parameters"]["ReleaseId"] == [release_id]
    assert client.kwargs["Parameters"]["DesiredState"] == ["continuous"]


def test_missing_command_id_is_ambiguous():
    class Client:
        def send_command(self, **kwargs):
            return {"Command": {}}

    ledger, _ = _configured()
    result = CStarSubmitter(ledger, Boto3SsmCommandSender(Client())).submit(_payload())
    assert result.submission_state == "AMBIGUOUS"
