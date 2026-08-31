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
NON_TERMINAL_STATUSES = {"Pending", "InProgress", "Delayed"}
TERMINAL_STATUS_MAP = {
    "Success": "SUCCESS",
    "Failed": "FAILED",
    "TimedOut": "TIMED_OUT",
    "Cancelled": "CANCELLED",
    "Undeliverable": "UNDELIVERABLE",
    "Terminated": "TERMINATED",
}
OCCURRENCE_COMMENT_RE = re.compile(r"^cstar(?:-evidence)?:([0-9a-f]{64})$")
MAX_FAILURE_OUTPUT_BYTES = 65_536
SAFE_MARKET_DATA_FAILURE_KINDS = frozenset(
    {"empty", "fetch", "timeout", "parse", "malformed"}
)
SAFE_MARKET_DATA_FAILURE_OPERATIONS = frozenset(
    {
        "auth_preflight", "top_trading_value", "stock_basic",
        "minute_chart_1m", "minute_chart_5m", "minute_chart_60m",
        "tick_strength", "program_trade", "foreign_window_trade",
        "order_book", "recent_ticks", "market_snapshot", "market_regime_60m",
        "chart_true_range",
    }
)
MARKET_DATA_FAILURE_SENTINEL_RE = re.compile(
    r"^shadow worker failed: error_type=MarketDataCollectionError "
    r"error_kind=(?P<kind>[a-z]+) error_operation=(?P<operation>[a-z0-9_]+)$"
)


class ObserverError(ValueError):
    """A bounded event/evidence rejection."""


class ObserverLedger(Protocol):
    def get_occurrence(self, occurrence_id: str) -> Mapping[str, object] | None: ...
    def get_occurrence_by_command(self, command_id: str) -> Mapping[str, object] | None: ...
    def record_evidence_command(self, *, occurrence_id: str, command_id: str) -> None: ...
    def advance(
        self,
        *,
        occurrence_id: str,
        command_state: str,
        runtime_state: str,
        closure_state: str | None = None,
    ) -> None: ...
    def record_failure_diagnostic(
        self,
        *,
        occurrence_id: str,
        diagnostic: Mapping[str, str],
    ) -> None: ...
    def due_occurrences(self) -> list[Mapping[str, object]]: ...


class EvidenceCommandSender(Protocol):
    def read_status(self, *, command_id: str) -> str: ...

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

    def read_failure_output(
        self,
        *,
        command_id: str,
    ) -> tuple[str | None, str | None]: ...


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


def _market_data_failure_diagnostic(
    streams: tuple[str | None, str | None],
) -> dict[str, str] | None:
    matches: set[tuple[str, str]] = set()
    for stream in streams:
        if not isinstance(stream, str):
            continue
        if len(stream.encode("utf-8")) > MAX_FAILURE_OUTPUT_BYTES:
            continue
        for line in stream.splitlines():
            match = MARKET_DATA_FAILURE_SENTINEL_RE.fullmatch(line)
            if match is None:
                continue
            kind = match.group("kind")
            operation = match.group("operation")
            if (
                kind in SAFE_MARKET_DATA_FAILURE_KINDS
                and operation in SAFE_MARKET_DATA_FAILURE_OPERATIONS
            ):
                matches.add((kind, operation))
    if len(matches) != 1:
        return None
    kind, operation = next(iter(matches))
    return {
        "category": "market_data_collection_error",
        "error_kind": kind,
        "error_operation": operation,
    }


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

    def _notify_failure(
        self,
        *,
        occurrence_id: str,
        command_id: str,
        document: str,
        status: str,
        diagnostic: Mapping[str, str] | None = None,
    ) -> str | None:
        if self.sink is None:
            return None
        try:
            message_payload: dict[str, object] = {
                "occurrence_id": occurrence_id,
                "command_id": command_id,
                "document": document,
                "status": status,
            }
            if diagnostic is not None:
                message_payload["failure_diagnostic"] = dict(diagnostic)
            self.sink.notify(
                category="observer_alert",
                message=json.dumps(message_payload, sort_keys=True, separators=(",", ":")),
            )
        except Exception:
            return "observer_alert_failed"
        return None

    def _read_failure_diagnostic(
        self,
        *,
        command_id: str,
    ) -> dict[str, str] | None:
        if self.evidence_sender is None:
            return None
        reader = getattr(self.evidence_sender, "read_failure_output", None)
        if not callable(reader):
            return None
        try:
            streams = reader(command_id=command_id)
        except Exception:
            return None
        if (
            not isinstance(streams, tuple)
            or len(streams) != 2
            or not all(value is None or isinstance(value, str) for value in streams)
        ):
            return None
        return _market_data_failure_diagnostic(streams)

    def _record_failure_diagnostic(
        self,
        *,
        occurrence_id: str,
        diagnostic: Mapping[str, str] | None,
    ) -> None:
        if diagnostic is None:
            return
        recorder = getattr(self.ledger, "record_failure_diagnostic", None)
        if not callable(recorder):
            return
        try:
            recorder(occurrence_id=occurrence_id, diagnostic=diagnostic)
        except Exception:
            # Terminal command state is already durable. Diagnostic persistence
            # is best-effort and must not turn a known failure into a retry.
            return

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
            expected_command_id = occurrence.get("evidence_command_id")
            if (
                isinstance(expected_command_id, str)
                and expected_command_id != command_id
            ):
                raise ObserverError("evidence command mismatch")
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
            reason: str | None = None
            if status in NON_TERMINAL_STATUSES:
                return ObservationResult(
                    occurrence_id=occurrence_id,
                    command_state=current_command,
                    runtime_state=current_runtime,
                    closure_state=closure,
                    evidence_requested=False,
                    reason="evidence_pending",
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
                    reason = self._notify_failure(
                        occurrence_id=occurrence_id,
                        command_id=command_id,
                        document=document,
                        status=status,
                    ) or "evidence_failed"
            else:
                self.ledger.advance(
                    occurrence_id=occurrence_id,
                    command_state=current_command,
                    runtime_state=current_runtime,
                    closure_state="ALERTED",
                )
                closure = "ALERTED"
                reason = self._notify_failure(
                    occurrence_id=occurrence_id,
                    command_id=command_id,
                    document=document,
                    status=status,
                ) or "evidence_failed"
            return ObservationResult(
                occurrence_id=occurrence_id,
                command_state=current_command,
                runtime_state=current_runtime,
                closure_state=closure,
                evidence_requested=False,
                reason=reason,
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
        failure_diagnostic: dict[str, str] | None = None
        if status in TERMINAL_STATUS_MAP and status != "Success":
            failure_diagnostic = self._read_failure_diagnostic(
                command_id=command_id,
            )
            self._record_failure_diagnostic(
                occurrence_id=occurrence_id,
                diagnostic=failure_diagnostic,
            )
        if status in TERMINAL_STATUS_MAP and self.sink is not None:
            # The terminal state is durable already; notification failure must
            # not turn a known SSM failure into a retryable event.
            reason = self._notify_failure(
                occurrence_id=occurrence_id,
                command_id=command_id,
                document=document,
                status=status,
                diagnostic=failure_diagnostic,
            )
        if status == "Success" and occurrence.get("phase") == "stop":
            if self.evidence_sender is not None:
                try:
                    self.request_evidence(occurrence=occurrence)
                    evidence_requested = True
                except Exception:
                    self.ledger.advance(
                        occurrence_id=occurrence_id,
                        command_state=command_state,
                        runtime_state=runtime_state,
                        closure_state="ALERTED",
                    )
                    closure_state = "ALERTED"
                    reason = self._notify_failure(
                        occurrence_id=occurrence_id,
                        command_id=command_id,
                        document=EVIDENCE_DOCUMENT_NAME,
                        status="evidence_request_failed",
                    ) or "evidence_request_failed"
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
            read_status = getattr(self.evidence_sender, "read_status", None)
            evidence_command_id = occurrence.get("evidence_command_id")
            if (
                closure_state == "EVIDENCE_PENDING"
                and isinstance(evidence_command_id, str)
                and callable(read_status)
            ):
                try:
                    status = read_status(command_id=evidence_command_id)
                    return_event = {
                        "detail-type": EVENT_TYPE,
                        "source": "aws.ssm",
                        "detail": {
                            "command-id": evidence_command_id,
                            "instance-id": INSTANCE_ID,
                            "status": status,
                            "document-name": EVIDENCE_DOCUMENT_NAME,
                            "comment": f"cstar-evidence:{occurrence['occurrence_id']}",
                        },
                    }
                    results.append(self.process_ssm_event(return_event))
                    continue
                except Exception:
                    results.append(
                        ObservationResult(
                            occurrence_id=str(occurrence["occurrence_id"]),
                            command_state=command_state,
                            runtime_state=runtime_state,
                            closure_state=closure_state,
                            evidence_requested=False,
                            reason="evidence_status_read_failed",
                        )
                    )
                    continue
            command_id = occurrence.get("command_id")
            if (
                command_state in {"PENDING", "IN_PROGRESS"}
                and isinstance(command_id, str)
                and callable(read_status)
            ):
                try:
                    status = read_status(command_id=command_id)
                    return_event = {
                        "detail-type": EVENT_TYPE,
                        "source": "aws.ssm",
                        "detail": {
                            "command-id": command_id,
                            "instance-id": INSTANCE_ID,
                            "status": status,
                            "document-name": "KiwoomStock-ShadowCStarActivation",
                            "comment": f"cstar:{occurrence['occurrence_id']}",
                        },
                    }
                    results.append(self.process_ssm_event(return_event))
                    continue
                except Exception:
                    results.append(
                        ObservationResult(
                            occurrence_id=str(occurrence["occurrence_id"]),
                            command_state=command_state,
                            runtime_state=runtime_state,
                            closure_state=closure_state,
                            evidence_requested=False,
                            reason="command_status_read_failed",
                        )
                    )
                    continue
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
        command_id = _bounded_text(command_id, 128)
        self.ledger.record_evidence_command(
            occurrence_id=str(occurrence["occurrence_id"]),
            command_id=command_id,
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

    def read_status(self, *, command_id: str) -> str:
        response = self.client.get_command_invocation(
            CommandId=command_id,
            InstanceId=self.instance_id,
        )
        status = response.get("Status") if isinstance(response, Mapping) else None
        if not isinstance(status, str) or status not in COMMAND_STATUSES:
            raise ObserverError("command status invalid")
        return status

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

    def read_failure_output(
        self,
        *,
        command_id: str,
    ) -> tuple[str | None, str | None]:
        response = self.client.get_command_invocation(
            CommandId=command_id,
            InstanceId=self.instance_id,
        )
        if not isinstance(response, Mapping):
            raise ObserverError("failure invocation invalid")
        status = response.get("Status")
        if not isinstance(status, str) or status not in TERMINAL_STATUS_MAP or status == "Success":
            raise ObserverError("failure invocation is not terminal")
        output = response.get("StandardOutputContent")
        error = response.get("StandardErrorContent")
        for value in (output, error):
            if value is not None and (
                not isinstance(value, str)
                or len(value.encode("utf-8")) > MAX_FAILURE_OUTPUT_BYTES
            ):
                raise ObserverError("failure invocation output invalid")
        return cast(str | None, output), cast(str | None, error)


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
            "observer_alert": "cstar_observer_alerted",
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

    def record_evidence_command(self, *, occurrence_id: str, command_id: str) -> None:
        command_id = _bounded_text(command_id, 128)
        item = self.occurrences[occurrence_id]
        if item.get("closure_state") != "EVIDENCE_PENDING":
            raise ObserverError("evidence occurrence not pending")
        existing = item.get("evidence_command_id")
        if existing is not None and existing != command_id:
            raise ObserverError("evidence command mismatch")
        item["evidence_command_id"] = command_id

    def record_failure_diagnostic(
        self,
        *,
        occurrence_id: str,
        diagnostic: Mapping[str, str],
    ) -> None:
        self.occurrences[occurrence_id]["failure_diagnostic"] = dict(diagnostic)

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
            Limit=100,
        )
        items = response.get("Items", []) if isinstance(response, Mapping) else []
        for item in items:
            if isinstance(item, Mapping) and item.get("SK") == "META":
                return dict(item)
        return None

    def record_evidence_command(self, *, occurrence_id: str, command_id: str) -> None:
        command_id = _bounded_text(command_id, 128)
        self.table.update_item(
            Key={"PK": f"OCC#{occurrence_id}", "SK": "META"},
            UpdateExpression="SET evidence_command_id = :command_id",
            ConditionExpression=(
                "closure_state = :pending AND "
                "(attribute_not_exists(evidence_command_id) "
                "OR evidence_command_id = :command_id)"
            ),
            ExpressionAttributeValues={
                ":command_id": command_id,
                ":pending": "EVIDENCE_PENDING",
            },
        )

    def record_failure_diagnostic(
        self,
        *,
        occurrence_id: str,
        diagnostic: Mapping[str, str],
    ) -> None:
        self.table.update_item(
            Key={"PK": f"OCC#{occurrence_id}", "SK": "META"},
            UpdateExpression="SET failure_diagnostic = :diagnostic",
            ConditionExpression="attribute_exists(PK) AND attribute_exists(SK)",
            ExpressionAttributeValues={":diagnostic": dict(diagnostic)},
        )

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
