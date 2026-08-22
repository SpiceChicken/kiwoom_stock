#!/usr/bin/env python3
"""Event-driven observer and bounded reconciler for C* sessions."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
import re
from typing import Any, Mapping, Protocol, cast
import urllib.request

try:
    from deploy.shadow_cstar_contract import (
        ContractError,
        diagnostic_category,
        metric_name,
        validate_state_transition,
    )
    from deploy.shadow_cstar_submitter import INSTANCE_ID, REGION
except ModuleNotFoundError:  # flat Lambda ZIP package
    from shadow_cstar_contract import (  # type: ignore[no-redef]
        ContractError,
        diagnostic_category,
        metric_name,
        validate_state_transition,
    )
    from shadow_cstar_submitter import INSTANCE_ID, REGION  # type: ignore[no-redef]


EVIDENCE_DOCUMENT_NAME = "KiwoomStock-ShadowEvidenceExport"
EVENT_TYPE = "EC2 Command Status-change Notification"
COMMAND_STATUSES = {
    "Pending", "InProgress", "Delayed", "Success", "Failed",
    "TimedOut", "Cancelled", "Undeliverable", "Terminated",
}
TERMINAL_STATUS_MAP = {
    "Success": "SUCCESS",
    "Failed": "FAILED",
    "TimedOut": "TIMED_OUT",
    "Cancelled": "CANCELLED",
    "Undeliverable": "UNDELIVERABLE",
    "Terminated": "TERMINATED",
}
OCCURRENCE_COMMENT_RE = re.compile(r"^cstar(?:-evidence)?:([0-9a-f]{64})$")


class ObserverError(ValueError):
    """A bounded event/evidence rejection."""


class ObserverLedger(Protocol):
    def get_occurrence(self, occurrence_id: str) -> Mapping[str, object] | None: ...
    def get_occurrence_by_command(self, command_id: str) -> Mapping[str, object] | None: ...
    def advance(
        self,
        *,
        occurrence_id: str,
        command_state: str,
        runtime_state: str,
        closure_state: str | None = None,
    ) -> None: ...
    def due_occurrences(self) -> list[Mapping[str, object]]: ...


class EvidenceCommandSender(Protocol):
    def send_evidence(
        self,
        *,
        session_date_kst: str,
        occurrence_id: str,
        release_id: str,
        offset: int,
        length: int,
    ) -> str: ...

    def read_output(self, *, command_id: str) -> bytes: ...


class EvidenceSink(Protocol):
    def put(self, *, key: str, content: bytes, metadata: Mapping[str, str]) -> None: ...
    def notify(self, *, category: str, message: str) -> None: ...


@dataclass(frozen=True)
class ObservationResult:
    occurrence_id: str
    command_state: str
    runtime_state: str
    closure_state: str | None
    evidence_requested: bool
    reason: str | None = None


def _invalid() -> ObserverError:
    return ObserverError("invalid")


def _bounded_text(value: object, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise _invalid()
    return value


def _event_fields(event: Mapping[str, object]) -> tuple[str, str | None, str, str]:
    if not isinstance(event, Mapping):
        raise _invalid()
    if event.get("detail-type") != EVENT_TYPE or not isinstance(event.get("detail"), Mapping):
        raise _invalid()
    if "source" in event and event.get("source") != "aws.ssm":
        raise ObserverError("foreign event")
    detail = cast(Mapping[str, object], event["detail"])
    expected = {"command-id", "instance-id", "status", "document-name"}
    if not expected.issubset(detail):
        raise _invalid()
    command_id = _bounded_text(detail["command-id"], 128)
    instance_id = _bounded_text(detail["instance-id"], 64)
    status = _bounded_text(detail["status"], 32)
    document_name = _bounded_text(detail["document-name"], 128)
    comment_value = detail.get("comment")
    if instance_id != INSTANCE_ID or document_name not in {
        "KiwoomStock-ShadowCStarActivation",
        EVIDENCE_DOCUMENT_NAME,
    }:
        raise ObserverError("foreign command")
    if status not in COMMAND_STATUSES:
        raise _invalid()
    if comment_value is None:
        return command_id, None, status, document_name
    comment = _bounded_text(comment_value, 128)
    match = OCCURRENCE_COMMENT_RE.fullmatch(comment)
    if match is None:
        raise _invalid()
    return command_id, match.group(1), status, document_name


class CStarObserver:
    def __init__(
        self,
        ledger: ObserverLedger,
        *,
        evidence_sender: EvidenceCommandSender | None = None,
        sink: EvidenceSink | None = None,
    ) -> None:
        self.ledger = ledger
        self.evidence_sender = evidence_sender
        self.sink = sink

    def process_ssm_event(self, event: Mapping[str, object]) -> ObservationResult:
        command_id, occurrence_id, status, document = _event_fields(event)
        if occurrence_id is None:
            lookup = getattr(self.ledger, "get_occurrence_by_command", None)
            occurrence_by_command = lookup(command_id) if callable(lookup) else None
            if occurrence_by_command is None:
                raise ObserverError("unknown command")
            occurrence_id = str(occurrence_by_command["occurrence_id"])
        occurrence = self.ledger.get_occurrence(occurrence_id)
        if occurrence is None:
            raise ObserverError("unknown occurrence")
        if document == EVIDENCE_DOCUMENT_NAME:
            current_command = str(occurrence.get("command_state", "SUCCESS"))
            current_runtime = str(occurrence.get("runtime_state", "STOPPED"))
            current_closure = str(occurrence.get("closure_state", "EVIDENCE_PENDING"))
            if current_closure in {"CLOSED", "ALERTED"}:
                return ObservationResult(
                    occurrence_id=occurrence_id,
                    command_state=current_command,
                    runtime_state=current_runtime,
                    closure_state=current_closure,
                    evidence_requested=False,
                    reason="terminal_duplicate",
                )
            closure = "EVIDENCE_PENDING"
            self.ledger.advance(
                occurrence_id=occurrence_id,
                command_state=current_command,
                runtime_state=current_runtime,
                closure_state=closure,
            )
            if status == "Success":
                try:
                    reader = getattr(self.evidence_sender, "read_output", None)
                    if self.sink is None or not callable(reader):
                        raise ObserverError("evidence reader unavailable")
                    self.archive_evidence(
                        occurrence=occurrence,
                        content=reader(command_id=command_id),
                    )
                    self.ledger.advance(
                        occurrence_id=occurrence_id,
                        command_state=current_command,
                        runtime_state=current_runtime,
                        closure_state="CLOSED",
                    )
                    closure = "CLOSED"
                except Exception:
                    self.ledger.advance(
                        occurrence_id=occurrence_id,
                        command_state=current_command,
                        runtime_state=current_runtime,
                        closure_state="ALERTED",
                    )
                    closure = "ALERTED"
            else:
                self.ledger.advance(
                    occurrence_id=occurrence_id,
                    command_state=current_command,
                    runtime_state=current_runtime,
                    closure_state="ALERTED",
                )
                closure = "ALERTED"
            return ObservationResult(
                occurrence_id=occurrence_id,
                command_state=current_command,
                runtime_state=current_runtime,
                closure_state=closure,
                evidence_requested=False,
                reason=None if status == "Success" else "evidence_failed",
            )
        command_state = {
            "Pending": "PENDING",
            "InProgress": "IN_PROGRESS",
            "Delayed": "PENDING",
            **TERMINAL_STATUS_MAP,
        }[status]
        if status == "Success":
            runtime_state = "STOPPED" if occurrence.get("phase") == "stop" else "ACCEPTED"
            closure_state = "EVIDENCE_PENDING" if occurrence.get("phase") == "stop" else None
        elif status in TERMINAL_STATUS_MAP:
            runtime_state = "FAILED" if status != "TimedOut" else "AMBIGUOUS"
            closure_state = "ALERTED"
        else:
            runtime_state = str(occurrence.get("runtime_state", "UNKNOWN"))
            closure_state = None
        self.ledger.advance(
            occurrence_id=occurrence_id,
            command_state=command_state,
            runtime_state=runtime_state,
            closure_state=closure_state,
        )
        evidence_requested = False
        reason: str | None = None
        if status == "Success" and occurrence.get("phase") == "stop":
            if self.evidence_sender is not None:
                try:
                    self.request_evidence(occurrence=occurrence)
                    evidence_requested = True
                except Exception:
                    reason = "evidence_request_failed"
        return ObservationResult(
            occurrence_id=occurrence_id,
            command_state=command_state,
            runtime_state=runtime_state,
            closure_state=closure_state,
            evidence_requested=evidence_requested,
            reason=reason,
        )

    def reconcile(self) -> list[ObservationResult]:
        results: list[ObservationResult] = []
        for occurrence in self.ledger.due_occurrences():
            command_state = str(occurrence.get("command_state", "UNKNOWN"))
            runtime_state = str(occurrence.get("runtime_state", "UNKNOWN"))
            closure_state = str(occurrence.get("closure_state", "OPEN"))
            results.append(
                ObservationResult(
                    occurrence_id=str(occurrence["occurrence_id"]),
                    command_state=command_state,
                    runtime_state=runtime_state,
                    closure_state=closure_state,
                    evidence_requested=False,
                    reason="event_missing_or_pending",
                )
            )
        return results

    def request_evidence(
        self,
        *,
        occurrence: Mapping[str, object],
        offset: int = 0,
        length: int = 12288,
    ) -> str:
        if self.evidence_sender is None:
            raise ObserverError("evidence sender unavailable")
        if type(offset) is not int or offset < 0 or offset > 99_999_999:
            raise _invalid()
        if type(length) is not int or length <= 0 or length > 12_288:
            raise _invalid()
        required = ("occurrence_id", "session_date_kst", "release_id")
        if any(not isinstance(occurrence.get(key), str) for key in required):
            raise _invalid()
        command_id = self.evidence_sender.send_evidence(
            session_date_kst=str(occurrence["session_date_kst"]),
            occurrence_id=str(occurrence["occurrence_id"]),
            release_id=str(occurrence["release_id"]),
            offset=offset,
            length=length,
        )
        return command_id

    def archive_evidence(
        self,
        *,
        occurrence: Mapping[str, object],
        content: bytes,
    ) -> str:
        if not isinstance(content, bytes) or len(content) > 12_288:
            raise _invalid()
        occurrence_id = _bounded_text(occurrence.get("occurrence_id"), 64)
        digest = hashlib.sha256(content).hexdigest()
        key = f"sessions/{occurrence_id}/evidence/{digest}.json"
        if self.sink is None:
            raise ObserverError("evidence sink unavailable")
        manifest = {
            "schema_version": 1,
            "occurrence_id": occurrence_id,
            "sha256": digest,
            "bytes": len(content),
        }
        try:
            self.sink.put(
                key=key,
                content=content,
                metadata={"sha256": digest, "occurrence_id": occurrence_id},
            )
            self.sink.notify(
                category="evidence_exported",
                message=json.dumps(manifest, sort_keys=True),
            )
        except Exception:
            current = self.ledger.get_occurrence(occurrence_id)
            if current is not None:
                self.ledger.advance(
                    occurrence_id=occurrence_id,
                    command_state=str(current.get("command_state", "SUCCESS")),
                    runtime_state=str(current.get("runtime_state", "STOPPED")),
                    closure_state="ALERTED",
                )
            raise
        return key


class Boto3EvidenceCommandSender:
    """Observer-only SSM adapter constrained to the evidence document."""

    def __init__(self, client: Any, *, instance_id: str = INSTANCE_ID) -> None:
        self.client = client
        self.instance_id = instance_id

    def send_evidence(self, *, session_date_kst, occurrence_id, release_id, offset, length) -> str:
        response = self.client.send_command(
            DocumentName=EVIDENCE_DOCUMENT_NAME,
            InstanceIds=[self.instance_id],
            Parameters={
                "SessionDateKst": [session_date_kst],
                "OccurrenceId": [occurrence_id],
                "ReleaseId": [release_id],
                "EvidenceOffset": [str(offset)],
                "EvidenceLength": [str(length)],
                "ExpectedInstanceId": [self.instance_id],
                "Region": [REGION],
            },
            Comment=f"cstar-evidence:{occurrence_id}",
            TimeoutSeconds=120,
        )
        command = response.get("Command") if isinstance(response, Mapping) else None
        command_id = command.get("CommandId") if isinstance(command, Mapping) else None
        if not isinstance(command_id, str) or not command_id:
            raise ObserverError("evidence command id missing")
        return command_id

    def read_output(self, *, command_id: str) -> bytes:
        response = self.client.get_command_invocation(
            CommandId=command_id,
            InstanceId=self.instance_id,
        )
        if (
            not isinstance(response, Mapping)
            or response.get("Status") != "Success"
            or response.get("ResponseCode") != 0
        ):
            raise ObserverError("evidence invocation failed")
        output = response.get("StandardOutputContent")
        if not isinstance(output, str) or len(output.encode("utf-8")) > 12_288:
            raise ObserverError("evidence output invalid")
        return output.encode("utf-8")


class Boto3EvidenceSink:
    """S3 evidence sink plus metrics-first optional Slack notification."""

    def __init__(
        self,
        *,
        s3_client: Any,
        cloudwatch_client: Any,
        bucket: str,
        alert_mode: str = "metrics-only",
        secrets_client: Any | None = None,
        slack_secret_arn: str = "",
    ) -> None:
        if not bucket or alert_mode not in {"metrics-only", "slack"}:
            raise ObserverError("evidence sink configuration invalid")
        self.s3_client = s3_client
        self.cloudwatch_client = cloudwatch_client
        self.bucket = bucket
        self.alert_mode = alert_mode
        self.secrets_client = secrets_client
        self.slack_secret_arn = slack_secret_arn

    def put(self, *, key: str, content: bytes, metadata: Mapping[str, str]) -> None:
        if not key.startswith("sessions/") or len(content) > 12_288:
            raise ObserverError("evidence object boundary invalid")
        self.s3_client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            Metadata={str(k): str(v)[:128] for k, v in metadata.items()},
            ContentType="application/json",
        )

    def notify(self, *, category: str, message: str) -> None:
        metric = {
            "evidence_exported": "cstar_evidence_exported",
            "evidence_failure": "cstar_observer_alerted",
        }.get(category)
        if metric is None:
            raise ObserverError("notification category invalid")
        self.cloudwatch_client.put_metric_data(
            Namespace="Kiwoom/ShadowCStar",
            MetricData=[{"MetricName": metric, "Value": 1, "Unit": "Count"}],
        )
        if self.alert_mode != "slack":
            return
        if self.secrets_client is None or not self.slack_secret_arn:
            raise ObserverError("slack secret unavailable")
        secret_response = self.secrets_client.get_secret_value(
            SecretId=self.slack_secret_arn
        )
        secret_text = secret_response.get("SecretString")
        if not isinstance(secret_text, str) or len(secret_text) > 4096:
            raise ObserverError("slack secret invalid")
        secret = json.loads(secret_text)
        webhook = secret.get("webhook_url") if isinstance(secret, Mapping) else None
        if (
            not isinstance(webhook, str)
            or not webhook.startswith("https://hooks.slack.com/")
            or len(webhook) > 512
        ):
            raise ObserverError("slack webhook invalid")
        body = json.dumps({"text": message[:512]}).encode("utf-8")
        request = urllib.request.Request(
            webhook,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            if response.status < 200 or response.status >= 300:
                raise ObserverError("slack delivery failed")


class InMemoryObserverLedger:
    def __init__(self, occurrences: Mapping[str, Mapping[str, object]]) -> None:
        self.occurrences = {key: dict(value) for key, value in occurrences.items()}

    def get_occurrence(self, occurrence_id: str) -> Mapping[str, object] | None:
        return self.occurrences.get(occurrence_id)

    def get_occurrence_by_command(self, command_id: str) -> Mapping[str, object] | None:
        return next(
            (
                item for item in self.occurrences.values()
                if item.get("command_id") == command_id
            ),
            None,
        )

    def advance(self, *, occurrence_id, command_state, runtime_state, closure_state=None):
        item = self.occurrences[occurrence_id]
        validate_state_transition(
            "command",
            str(item.get("command_state", "UNKNOWN")),
            command_state,
        )
        validate_state_transition(
            "runtime",
            str(item.get("runtime_state", "UNKNOWN")),
            runtime_state,
        )
        current_closure = str(item.get("closure_state", "OPEN"))
        target_closure = current_closure if closure_state is None else closure_state
        validate_state_transition("closure", current_closure, target_closure)
        item["command_state"] = command_state
        item["runtime_state"] = runtime_state
        item["closure_state"] = target_closure

    def due_occurrences(self):
        return [
            item for item in self.occurrences.values()
            if item.get("closure_state") in {"OPEN", "EVIDENCE_PENDING"}
        ]


class DynamoCStarObserverLedger:
    """Optimistic conditional-update adapter for observer state dimensions."""

    def __init__(self, table: Any, *, due_index: str = "closure-due-index") -> None:
        self.table = table
        self.due_index = due_index

    def get_occurrence(self, occurrence_id: str) -> Mapping[str, object] | None:
        response = self.table.get_item(
            Key={"PK": f"OCC#{occurrence_id}", "SK": "META"},
            ConsistentRead=True,
        )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return dict(item) if isinstance(item, Mapping) else None

    def get_occurrence_by_command(self, command_id: str) -> Mapping[str, object] | None:
        from boto3.dynamodb.conditions import Key

        response = self.table.query(
            IndexName="command-index",
            KeyConditionExpression=Key("command_id").eq(command_id),
            Limit=2,
        )
        items = response.get("Items", []) if isinstance(response, Mapping) else []
        if len(items) != 1 or not isinstance(items[0], Mapping):
            return None
        return dict(items[0])

    def advance(
        self,
        *,
        occurrence_id: str,
        command_state: str,
        runtime_state: str,
        closure_state: str | None = None,
    ) -> None:
        current = self.get_occurrence(occurrence_id)
        if current is None:
            raise ObserverError("unknown occurrence")
        old_command = str(current.get("command_state", "UNKNOWN"))
        old_runtime = str(current.get("runtime_state", "UNKNOWN"))
        old_closure = str(current.get("closure_state", "OPEN"))
        validate_state_transition("command", old_command, command_state)
        validate_state_transition("runtime", old_runtime, runtime_state)
        target_closure = old_closure if closure_state is None else closure_state
        validate_state_transition("closure", old_closure, target_closure)
        import time
        terminal = target_closure in {"CLOSED", "ALERTED"}
        update_expression = (
            "SET command_state = :new_command, runtime_state = :new_runtime, "
            "closure_state = :new_closure"
        )
        values: dict[str, object] = {
            ":new_command": command_state,
            ":new_runtime": runtime_state,
            ":new_closure": target_closure,
            ":old_command": old_command,
            ":old_runtime": old_runtime,
            ":old_closure": old_closure,
        }
        if terminal:
            update_expression += " , expires_at = :expires REMOVE next_action_at"
            values[":expires"] = int(time.time()) + 400 * 86_400
        self.table.update_item(
            Key={"PK": f"OCC#{occurrence_id}", "SK": "META"},
            UpdateExpression=update_expression,
            ConditionExpression=(
                "command_state = :old_command AND runtime_state = :old_runtime "
                "AND closure_state = :old_closure"
            ),
            ExpressionAttributeValues=values,
        )

    def due_occurrences(self) -> list[Mapping[str, object]]:
        from boto3.dynamodb.conditions import Key
        import time

        result: list[Mapping[str, object]] = []
        for state in ("OPEN", "EVIDENCE_PENDING"):
            response = self.table.query(
                IndexName=self.due_index,
                KeyConditionExpression=(
                    Key("closure_state").eq(state)
                    & Key("next_action_at").lte(int(time.time()))
                ),
                Limit=100,
            )
            items = response.get("Items", []) if isinstance(response, Mapping) else []
            result.extend(item for item in items if isinstance(item, Mapping))
        return result


def lambda_handler(event: Mapping[str, object], _context: object) -> dict[str, object]:
    """AWS Lambda entrypoint for status events and bounded reconciliation."""

    try:
        import boto3
    except ImportError as error:
        raise ObserverError("boto3 runtime dependency missing") from error
    table_name = os.environ.get("CSTAR_TABLE_NAME", "")
    if not table_name:
        raise ObserverError("CSTAR_TABLE_NAME is not configured")
    table = boto3.resource("dynamodb").Table(table_name)
    ledger = DynamoCStarObserverLedger(table)
    alert_mode = os.environ.get("CSTAR_ALERT_MODE", "metrics-only")
    slack_secret_arn = os.environ.get("CSTAR_SLACK_SECRET_ARN", "")
    observer = CStarObserver(
        ledger,
        evidence_sender=Boto3EvidenceCommandSender(
            boto3.client("ssm", region_name=REGION)
        ),
        sink=Boto3EvidenceSink(
            s3_client=boto3.client("s3", region_name=REGION),
            cloudwatch_client=boto3.client("cloudwatch", region_name=REGION),
            bucket=os.environ.get("CSTAR_EVIDENCE_BUCKET", ""),
            alert_mode=alert_mode,
            secrets_client=(
                boto3.client("secretsmanager", region_name=REGION)
                if slack_secret_arn
                else None
            ),
            slack_secret_arn=slack_secret_arn,
        ),
    )
    if event.get("kind") == "reconcile":
        return {
            "schema_version": 1,
            "kind": "reconcile",
            "results": [asdict(result) for result in observer.reconcile()],
        }
    return {"schema_version": 1, "kind": "event", **asdict(observer.process_ssm_event(event))}
