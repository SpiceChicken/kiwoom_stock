"""Observer ordering, bounded export, and exact SSM document tests."""

import hashlib

import pytest

from deploy.shadow_cstar_observer import (
    Boto3EvidenceCommandSender,
    Boto3EvidenceSink,
    CStarObserver,
    DynamoCStarObserverLedger,
    EVIDENCE_DOCUMENT_NAME,
    InMemoryObserverLedger,
    ObserverError,
)


OCCURRENCE = {
    "occurrence_id": "a" * 64,
    "phase": "start",
    "session_date_kst": "2026-08-24",
    "release_id": "b" * 64,
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


def test_success_start_advances_runtime_without_closing_session():
    ledger = InMemoryObserverLedger({OCCURRENCE["occurrence_id"]: OCCURRENCE})
    result = CStarObserver(ledger).process_ssm_event(_event())
    assert result.runtime_state == "ACCEPTED"
    assert result.closure_state is None
    assert ledger.occurrences[OCCURRENCE["occurrence_id"]]["closure_state"] == "OPEN"


def test_success_stop_enters_evidence_pending():
    occurrence = {**OCCURRENCE, "phase": "stop"}
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
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
    ledger = InMemoryObserverLedger({occurrence["occurrence_id"]: occurrence})
    sender = Sender()
    result = CStarObserver(ledger, evidence_sender=sender).process_ssm_event(_event())
    assert result.evidence_requested is True
    assert sender.calls[0]["occurrence_id"] == occurrence["occurrence_id"]


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
        release_id="b" * 64,
        offset=0,
        length=256,
    )
    assert command_id == "evidence-1"
    assert client.kwargs["DocumentName"] == EVIDENCE_DOCUMENT_NAME
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
