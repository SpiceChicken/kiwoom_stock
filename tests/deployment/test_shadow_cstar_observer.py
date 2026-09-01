"""Observer ordering, bounded export, and exact SSM document tests."""

import hashlib

import pytest

from deploy.shadow_cstar_contract import release_id_for
from deploy.shadow_cstar_observer import (
    Boto3EvidenceCommandSender,
    Boto3EvidenceSink,
    CStarObserver,
    DynamoCStarObserverLedger,
    EVIDENCE_DOCUMENT_NAME,
    InMemoryObserverLedger,
    ObserverError,
)


RELEASE = {
    "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
    "source_sha": "d" * 40,
    "compose_shadow_sha256": "e" * 64,
    "worker_sha256": "f" * 64,
    "validator_sha256": "0" * 64,
    "shadow_document_sha256": "1" * 64,
    "rollout_attempt_id": "rollout-1",
}
RELEASE_ID = release_id_for(RELEASE)


OCCURRENCE = {
    "occurrence_id": "a" * 64,
    "phase": "start",
    "session_date_kst": "2026-08-24",
    "release_id": RELEASE_ID,
    "command_id": "command-1",
    "command_state": "PENDING",
    "runtime_state": "UNKNOWN",
    "closure_state": "OPEN",
}


def _event(status="Success", occurrence_id="a" * 64):
    return {
        "detail-type": "EC2 Command Status-change Notification",
        "detail": {
            "command-id": "command-1",
            "instance-id": "i-0e42e09d6c087ba29",
            "status": status,
            "document-name": "KiwoomStock-ShadowCStarActivation",
            "comment": f"cstar:{occurrence_id}",
        },
    }


def _evidence_event(status="Success", occurrence_id="a" * 64, command_id="evidence-1"):
    return {
        "detail-type": "EC2 Command Status-change Notification",
        "detail": {
            "command-id": command_id,
            "instance-id": "i-0e42e09d6c087ba29",
            "status": status,
            "document-name": EVIDENCE_DOCUMENT_NAME,
            "comment": f"cstar-evidence:{occurrence_id}",
        },
    }


def test_success_start_advances_runtime_without_closing_session():
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger).process_ssm_event(_event())
    assert result.runtime_state == "ACCEPTED"
    assert result.closure_state is None
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["closure_state"] == "OPEN"


def test_success_start_does_not_emit_observer_alert():
    class Sink:
        def __init__(self):
            self.notifications = []

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    sink = Sink()
    result = CStarObserver(ledger, sink=sink).process_ssm_event(_event())
    assert result.runtime_state == "ACCEPTED"
    assert result.closure_state is None
    assert sink.notifications == []


def test_success_stop_enters_evidence_pending():
    occurrence = {**OCCURRENCE, "phase": "stop"}
    ledger = InMemoryObserverLedger(
        {occurrence["occurrence_id"]: occurrence},
        {RELEASE_ID: RELEASE},
    )
    result = CStarObserver(ledger).process_ssm_event(_event())
    assert result.runtime_state == "STOPPED"
    assert result.closure_state == "EVIDENCE_PENDING"


def test_success_stop_requests_exact_evidence_document_when_sender_is_available():
    class Sender:
        def __init__(self):
            self.calls = []

        def send_evidence(self, **kwargs):
            self.calls.append(kwargs)
            return "evidence-1"

    occurrence = {**OCCURRENCE, "phase": "stop"}
    ledger = InMemoryObserverLedger(
        {occurrence["occurrence_id"]: occurrence},
        {RELEASE_ID: RELEASE},
    )
    sender = Sender()
    result = CStarObserver(ledger, evidence_sender=sender).process_ssm_event(_event())
    assert result.evidence_requested is True
    assert sender.calls[0]["occurrence_id"] == occurrence["occurrence_id"]
    assert sender.calls[0]["image_digest"] == RELEASE["image_digest"]
    assert sender.calls[0]["source_sha"] == RELEASE["source_sha"]
    assert ledger.occurrences[occurrence["occurrence_id"]]["evidence_command_id"] == "evidence-1"


def test_evidence_nonterminal_status_remains_pending():
    occurrence = {
        **OCCURRENCE,
        "phase": "stop",
        "command_state": "SUCCESS",
        "runtime_state": "STOPPED",
        "closure_state": "EVIDENCE_PENDING",
        "evidence_command_id": "evidence-1",
    }
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    result = CStarObserver(ledger).process_ssm_event(_evidence_event("InProgress"))
    assert result.reason == "evidence_pending"
    assert result.closure_state == "EVIDENCE_PENDING"
    assert ledger.occurrences[occurrence["occurrence_id"]]["closure_state"] == "EVIDENCE_PENDING"


def test_reconcile_reads_pending_evidence_status_when_event_is_missing():
    class Sender:
        def read_status(self, **kwargs):
            assert kwargs == {"command_id": "evidence-1"}
            return "Failed"

    occurrence = {
        **OCCURRENCE,
        "phase": "stop",
        "command_state": "SUCCESS",
        "runtime_state": "STOPPED",
        "closure_state": "EVIDENCE_PENDING",
        "evidence_command_id": "evidence-1",
    }
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    result = CStarObserver(ledger, evidence_sender=Sender()).reconcile()
    assert result[0].reason == "evidence_failed"
    assert result[0].closure_state == "ALERTED"
    assert ledger.occurrences[occurrence["occurrence_id"]]["closure_state"] == "ALERTED"


def test_evidence_terminal_failure_emits_observer_alert():
    class Sink:
        def __init__(self):
            self.notifications = []

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    occurrence = {
        **OCCURRENCE,
        "phase": "stop",
        "command_state": "SUCCESS",
        "runtime_state": "STOPPED",
        "closure_state": "EVIDENCE_PENDING",
        "evidence_command_id": "evidence-1",
    }
    sink = Sink()
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    result = CStarObserver(ledger, sink=sink).process_ssm_event(_evidence_event("Failed"))
    assert result.reason == "evidence_failed"
    assert sink.notifications == [{
        "category": "observer_alert",
        "message": '{"command_id":"evidence-1","document":"' + EVIDENCE_DOCUMENT_NAME
        + '","occurrence_id":"' + "a" * 64 + '","status":"Failed"}',
    }]


def test_evidence_event_cannot_update_a_different_tracked_command():
    occurrence = {
        **OCCURRENCE,
        "phase": "stop",
        "command_state": "SUCCESS",
        "runtime_state": "STOPPED",
        "closure_state": "EVIDENCE_PENDING",
        "evidence_command_id": "evidence-1",
    }
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    with pytest.raises(ObserverError, match="evidence command mismatch"):
        CStarObserver(ledger).process_ssm_event(
            _evidence_event("Failed", command_id="evidence-2")
        )


def test_evidence_request_failure_closes_occurrence_as_alerted():
    class Sender:
        def send_evidence(self, **kwargs):
            raise RuntimeError("send failed")

    class Sink:
        def __init__(self):
            self.notifications = []

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    occurrence = {**OCCURRENCE, "phase": "stop"}
    sink = Sink()
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    result = CStarObserver(
        ledger,
        evidence_sender=Sender(),
        sink=sink,
    ).process_ssm_event(_event())
    assert result.reason == "evidence_request_failed"
    assert result.closure_state == "ALERTED"
    assert ledger.occurrences[occurrence["occurrence_id"]]["closure_state"] == "ALERTED"
    assert sink.notifications[0]["category"] == "observer_alert"


@pytest.mark.parametrize("status", ["Failed", "TimedOut", "Cancelled", "Undeliverable", "Terminated"])
def test_terminal_failure_never_becomes_runtime_success(status):
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger).process_ssm_event(_event(status))
    assert result.runtime_state in {"FAILED", "AMBIGUOUS"}
    assert result.closure_state == "ALERTED"


def test_activation_failure_emits_observer_alert_after_terminal_state_is_saved():
    class Sink:
        def __init__(self):
            self.notifications = []

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    sink = Sink()
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger, sink=sink).process_ssm_event(_event("Failed"))
    assert result.reason is None
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["closure_state"] == "ALERTED"
    assert sink.notifications == [{
        "category": "observer_alert",
        "message": '{"command_id":"command-1","document":"KiwoomStock-ShadowCStarActivation","occurrence_id":"' + "a" * 64 + '","status":"Failed"}',
    }]


def test_activation_failure_survives_alert_sink_failure():
    class Sink:
        def notify(self, **kwargs):
            raise RuntimeError("metric unavailable")

    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger, sink=Sink()).process_ssm_event(_event("Failed"))
    assert result.reason == "observer_alert_failed"
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["closure_state"] == "ALERTED"


def test_activation_failure_persists_only_safe_market_data_diagnostic():
    class Sender:
        def read_failure_output(self, **kwargs):
            assert kwargs == {"command_id": "command-1"}
            sentinel = (
                "shadow worker failed: error_type=MarketDataCollectionError "
                "error_kind=timeout error_operation=stock_basic"
            )
            return sentinel, "raw provider details must not be persisted"

    class Sink:
        def __init__(self):
            self.notifications = []

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    sink = Sink()
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(
        ledger,
        evidence_sender=Sender(),
        sink=sink,
    ).process_ssm_event(_event("Failed"))

    assert result.closure_state == "ALERTED"
    expected = {
        "category": "market_data_collection_error",
        "error_kind": "timeout",
        "error_operation": "stock_basic",
    }
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["failure_diagnostic"] == expected
    assert sink.notifications == [{
        "category": "observer_alert",
        "message": '{"command_id":"command-1","document":"KiwoomStock-ShadowCStarActivation",'
        '"failure_diagnostic":{"category":"market_data_collection_error",'
        '"error_kind":"timeout","error_operation":"stock_basic"},"occurrence_id":"'
        + "a" * 64 + '","status":"Failed"}',
    }]


def test_activation_failure_ignores_unallowlisted_market_data_diagnostic():
    class Sender:
        def read_failure_output(self, **kwargs):
            return (
                "shadow worker failed: error_type=MarketDataCollectionError "
                "error_kind=timeout error_operation=secret_endpoint",
                None,
            )

    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    CStarObserver(ledger, evidence_sender=Sender()).process_ssm_event(_event("Failed"))
    assert "failure_diagnostic" not in ledger.occurrences[OCCURRENCE["occurrence_id"]]


def test_foreign_document_and_unknown_occurrence_are_rejected():
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    with pytest.raises(ObserverError, match="foreign command"):
        CStarObserver(ledger).process_ssm_event({
            **_event(),
            "detail": {**_event()["detail"], "document-name": "KiwoomStock-ShadowWorker"},
        })
    with pytest.raises(ObserverError, match="unknown occurrence"):
        CStarObserver(ledger).process_ssm_event(_event(occurrence_id="c" * 64))


def test_status_event_without_comment_uses_command_ledger_binding():
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    event = _event()
    event["detail"] = {
        key: value for key, value in event["detail"].items() if key != "comment"
    }
    result = CStarObserver(ledger).process_ssm_event(event)
    assert result.occurrence_id == OCCURRENCE["occurrence_id"]


def test_reconcile_reads_pending_ssm_status_when_event_is_missing():
    class Sender:
        def read_status(self, **kwargs):
            assert kwargs == {"command_id": "command-1"}
            return "Failed"

    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger, evidence_sender=Sender()).reconcile()
    assert result[0].command_state == "FAILED"
    assert result[0].runtime_state == "FAILED"
    assert result[0].closure_state == "ALERTED"
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["closure_state"] == "ALERTED"


def test_reconcile_binds_polled_status_to_known_occurrence():
    class Sender:
        def read_status(self, **kwargs):
            return "Failed"

    occurrence = {**OCCURRENCE, "occurrence_id": "d" * 64}
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    result = CStarObserver(ledger, evidence_sender=Sender()).reconcile()
    assert result[0].occurrence_id == occurrence["occurrence_id"]
    assert result[0].closure_state == "ALERTED"


def test_dynamo_command_lookup_selects_occurrence_meta_over_command_audit_item():
    class Table:
        def query(self, **kwargs):
            assert kwargs["IndexName"] == "command-index"
            return {
                "Items": [
                    {
                        "PK": "OCC#a" * 64,
                        "SK": "CMD#command-1",
                        "command_id": "command-1",
                        "occurrence_id": "a" * 64,
                    },
                    {
                        "PK": "OCC#" + "a" * 64,
                        "SK": "META",
                        "command_id": "command-1",
                        "occurrence_id": "a" * 64,
                        "phase": "start",
                    },
                ]
            }

    result = DynamoCStarObserverLedger(Table()).get_occurrence_by_command("command-1")
    assert result is not None
    assert result["SK"] == "META"


def test_dynamo_records_evidence_command_with_pending_condition():
    class Table:
        def __init__(self):
            self.kwargs = None

        def update_item(self, **kwargs):
            self.kwargs = kwargs

    table = Table()
    DynamoCStarObserverLedger(table).record_evidence_command(
        occurrence_id="a" * 64,
        command_id="evidence-1",
    )
    assert table.kwargs == {
        "Key": {"PK": "OCC#" + "a" * 64, "SK": "META"},
        "UpdateExpression": "SET evidence_command_id = :command_id",
        "ConditionExpression": (
            "closure_state = :pending AND "
            "(attribute_not_exists(evidence_command_id) "
            "OR evidence_command_id = :command_id)"
        ),
        "ExpressionAttributeValues": {
            ":command_id": "evidence-1",
            ":pending": "EVIDENCE_PENDING",
        },
    }


def test_dynamo_records_bounded_failure_diagnostic_on_occurrence_meta():
    class Table:
        def __init__(self):
            self.kwargs = None

        def update_item(self, **kwargs):
            self.kwargs = kwargs

    diagnostic = {
        "category": "market_data_collection_error",
        "error_kind": "empty",
        "error_operation": "recent_ticks",
    }
    table = Table()
    DynamoCStarObserverLedger(table).record_failure_diagnostic(
        occurrence_id="a" * 64,
        diagnostic=diagnostic,
    )
    assert table.kwargs == {
        "Key": {"PK": "OCC#" + "a" * 64, "SK": "META"},
        "UpdateExpression": "SET failure_diagnostic = :diagnostic",
        "ConditionExpression": "attribute_exists(PK) AND attribute_exists(SK)",
        "ExpressionAttributeValues": {":diagnostic": diagnostic},
    }


def test_evidence_export_is_bounded_and_content_addressed():
    class Sink:
        def __init__(self):
            self.puts = []
            self.notifications = []

        def put(self, **kwargs):
            self.puts.append(kwargs)

        def notify(self, **kwargs):
            self.notifications.append(kwargs)

    sink = Sink()
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    observer = CStarObserver(ledger, sink=sink)
    content = b'{"bounded":true}'
    key = observer.archive_evidence(occurrence=OCCURRENCE, content=content)
    digest = hashlib.sha256(content).hexdigest()
    assert key == f"sessions/{OCCURRENCE['occurrence_id']}/evidence/{digest}.json"
    assert sink.puts[0]["metadata"]["sha256"] == digest


def test_evidence_sender_uses_exact_document_and_no_activation_fields():
    class Client:
        def __init__(self):
            self.kwargs = None

        def send_command(self, **kwargs):
            self.kwargs = kwargs
            return {"Command": {"CommandId": "evidence-1"}}

    client = Client()
    command_id = Boto3EvidenceCommandSender(client).send_evidence(
        session_date_kst="2026-08-24",
        occurrence_id="a" * 64,
        release_id=RELEASE_ID,
        image_digest=RELEASE["image_digest"],
        source_sha=RELEASE["source_sha"],
        worker_sha256=RELEASE["worker_sha256"],
        validator_sha256=RELEASE["validator_sha256"],
        shadow_document_sha256=RELEASE["shadow_document_sha256"],
        offset=0,
        length=256,
    )
    assert command_id == "evidence-1"
    assert client.kwargs["DocumentName"] == EVIDENCE_DOCUMENT_NAME
    assert client.kwargs["Parameters"]["ImageDigest"] == [RELEASE["image_digest"]]
    assert client.kwargs["Parameters"]["ExpectedWorkerSha256"] == [RELEASE["worker_sha256"]]
    assert "DesiredState" not in client.kwargs["Parameters"]


def test_evidence_sender_reads_bounded_ssm_status():
    class Client:
        def get_command_invocation(self, **kwargs):
            assert kwargs == {
                "CommandId": "command-1",
                "InstanceId": "i-0e42e09d6c087ba29",
            }
            return {"Status": "Failed"}

    sender = Boto3EvidenceCommandSender(Client())
    assert sender.read_status(command_id="command-1") == "Failed"


def test_evidence_sender_reads_bounded_activation_failure_output():
    class Client:
        def get_command_invocation(self, **kwargs):
            return {
                "Status": "Failed",
                "ResponseCode": 1,
                "StandardOutputContent": "safe stdout",
                "StandardErrorContent": "safe stderr",
            }

    sender = Boto3EvidenceCommandSender(Client())
    assert sender.read_failure_output(command_id="command-1") == (
        "safe stdout", "safe stderr",
    )


def test_metrics_only_sink_does_not_read_or_send_slack_secret():
    class S3:
        def put_object(self, **kwargs):
            self.kwargs = kwargs

    class CloudWatch:
        def put_metric_data(self, **kwargs):
            self.kwargs = kwargs

    sink = Boto3EvidenceSink(
        s3_client=S3(),
        cloudwatch_client=CloudWatch(),
        bucket="evidence",
    )
    sink.put(key="sessions/a/evidence/x.json", content=b"{}", metadata={"sha256": "x"})
    sink.notify(category="evidence_exported", message="bounded")
