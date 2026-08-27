#!/usr/bin/env python3
"""Cloud submitter for the C* exact-document activation protocol.

The submitter owns cloud-side leasing and SSM submission.  It does not execute
the worker and never mutates Scheduler configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import os
import re
from threading import RLock
from typing import Any, Mapping, Protocol, cast

try:
    from deploy.shadow_cstar_contract import (
        ContractError,
        RELEASE_INTENT_KEYS,
        make_release_intent,
        make_session_lease,
        occurrence_id_for,
        parse_utc_timestamp,
        session_date_kst,
        validate_scheduler_payload,
        validate_scheduled_slot,
    )
except ModuleNotFoundError:  # flat Lambda ZIP package
    from shadow_cstar_contract import (  # type: ignore[no-redef]
        ContractError,
        RELEASE_INTENT_KEYS,
        make_release_intent,
        make_session_lease,
        occurrence_id_for,
        parse_utc_timestamp,
        session_date_kst,
        validate_scheduler_payload,
        validate_scheduled_slot,
    )


ACTIVATION_DOCUMENT_NAME = "KiwoomStock-ShadowCStarActivation"
INSTANCE_ID = "i-0e42e09d6c087ba29"
REGION = "ap-northeast-2"
COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{7,127}$")


class SubmitterError(ValueError):
    """A fail-closed cloud submission rejection."""


@dataclass(frozen=True)
class SubmissionResult:
    occurrence_id: str
    session_date_kst: str
    release_id: str | None
    submission_state: str
    command_id: str | None
    reason: str | None = None


class CStarLedger(Protocol):
    def get_generation(self, generation: str) -> Mapping[str, object] | None: ...
    def get_active_release(self) -> Mapping[str, object] | None: ...
    def get_release_intent(self, release_id: str) -> Mapping[str, object] | None: ...
    def get_session(self, session_date: str) -> Mapping[str, object] | None: ...
    def prepare_occurrence(
        self,
        *,
        occurrence_id: str,
        payload: Mapping[str, object],
        lease: Mapping[str, object],
        release: Mapping[str, object],
    ) -> Mapping[str, object]: ...
    def record_command(
        self,
        *,
        occurrence_id: str,
        command_id: str,
        attempt_number: int,
    ) -> None: ...

    def record_rejection(
        self,
        *,
        occurrence_id: str,
        payload: Mapping[str, object],
        reason: str,
        release_id: str | None,
    ) -> None: ...


class SsmCommandSender(Protocol):
    def send(
        self,
        *,
        payload: Mapping[str, object],
        lease: Mapping[str, object],
        release: Mapping[str, object],
        occurrence_id: str,
    ) -> str: ...


def _reject(message: str) -> SubmitterError:
    return SubmitterError(message)


def _ledger_error_details(error: Exception) -> dict[str, object]:
    """Return bounded, non-secret details for a cloud ledger exception."""

    details: dict[str, object] = {
        "error_type": type(error).__name__,
    }
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return details
    error_value = response.get("Error")
    if isinstance(error_value, Mapping):
        code = error_value.get("Code")
        if isinstance(code, str):
            details["error_code"] = code[:128]
    cancellation_reasons = response.get("CancellationReasons")
    if isinstance(cancellation_reasons, list):
        reasons: list[dict[str, str]] = []
        for value in cancellation_reasons[:100]:
            if not isinstance(value, Mapping):
                continue
            reason: dict[str, str] = {}
            reason_code = value.get("Code")
            reason_message = value.get("Message")
            if isinstance(reason_code, str):
                reason["code"] = reason_code[:128]
            if isinstance(reason_message, str):
                reason["message"] = reason_message[:256]
            if reason:
                reasons.append(reason)
        details["cancellation_reasons"] = reasons
    return details


def _log_ledger_failure(operation: str, error: Exception) -> None:
    """Log only bounded service metadata; never log request values or secrets."""

    print(json.dumps({
        "event": "cstar-ledger-failure",
        "operation": operation,
        **_ledger_error_details(error),
    }, sort_keys=True, separators=(",", ":")))


def _ledger_request_shape(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Describe only serialized AttributeValue types for a failed request."""

    result: list[dict[str, object]] = []
    for transaction in items:
        operation = next(iter(transaction), "unknown")
        raw_body = transaction.get(operation)
        if not isinstance(raw_body, Mapping):
            result.append({"operation": operation, "body": type(raw_body).__name__})
            continue
        body = dict(raw_body)
        entry: dict[str, object] = {"operation": operation}
        for field in ("Item", "Key", "ExpressionAttributeValues"):
            value = body.get(field)
            if not isinstance(value, Mapping):
                continue
            entry[field] = {
                str(key): next(iter(attribute), "unknown")
                if isinstance(attribute, Mapping)
                else type(attribute).__name__
                for key, attribute in value.items()
            }
        result.append(entry)
    return result


def _ledger_transport_shape(client: Any, wire_items: list[dict[str, object]]) -> object:
    """Inspect botocore's serialized JSON shape without exposing values."""

    try:
        serializer = client._serializer
        operation_model = client._service_model.operation_model("TransactWriteItems")
        request = serializer.serialize_to_request(
            {"TransactItems": wire_items}, operation_model
        )
        body = request.get("body")
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        decoded = json.loads(body) if isinstance(body, str) else body
        if not isinstance(decoded, Mapping):
            return {"body": type(decoded).__name__}
        transactions = decoded.get("TransactItems")
        if not isinstance(transactions, list):
            return {"transactions": type(transactions).__name__}
        return _ledger_request_shape(transactions)
    except Exception as error:
        return {"error_type": type(error).__name__}


def _ledger_body_shape(body: object) -> object:
    """Describe AttributeValue types from an already serialized HTTP body."""

    try:
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        decoded = json.loads(body) if isinstance(body, str) else body
        if not isinstance(decoded, Mapping):
            return {"body": type(decoded).__name__}
        transactions = decoded.get("TransactItems")
        if not isinstance(transactions, list):
            return {"transactions": type(transactions).__name__}
        return _ledger_request_shape(transactions)
    except Exception as error:
        return {"error_type": type(error).__name__}


def _botocore_version() -> str:
    try:
        import botocore
    except ImportError:
        return "unknown"
    value = getattr(botocore, "__version__", "unknown")
    return value if isinstance(value, str) else "unknown"


def _release_id(value: Mapping[str, object]) -> str:
    item = value.get("release_id")
    if not isinstance(item, str) or len(item) != 64 or any(c not in "0123456789abcdef" for c in item):
        raise _reject("release id invalid")
    return item


class CStarSubmitter:
    def __init__(self, ledger: CStarLedger, sender: SsmCommandSender) -> None:
        self.ledger = ledger
        self.sender = sender

    def submit(self, value: Mapping[str, object]) -> SubmissionResult:
        try:
            payload = validate_scheduler_payload(value)
            validate_scheduled_slot(payload)
        except ContractError:
            raise _reject("payload invalid") from None
        generation = self.ledger.get_generation(str(payload["schedule_generation"]))
        expected_arn = generation.get("schedule_arn") if generation is not None else None
        schedule_arns = generation.get("schedule_arns") if generation is not None else None
        if isinstance(schedule_arns, Mapping):
            expected_arn = schedule_arns.get(str(payload["phase"]))
        if generation is None or expected_arn != payload["schedule_arn"]:
            return self._rejected(
                payload,
                "STALE_GENERATION",
            )

        session_date = session_date_kst(payload)
        if payload["phase"] == "start":
            active = self.ledger.get_active_release()
            if active is None:
                return self._rejected(payload, "NO_ACTIVE_RELEASE")
            release_id = _release_id(active)
            session = make_session_lease(payload, release_id=release_id)
        else:
            existing_session = self.ledger.get_session(session_date)
            if existing_session is None:
                return self._rejected(payload, "REJECTED_NO_SESSION")
            release_id = _release_id(existing_session)
            session = cast(dict[str, str | int], dict(existing_session))

        raw_release = self.ledger.get_release_intent(release_id)
        if raw_release is None:
            return self._rejected(payload, "RELEASE_NOT_FOUND", release_id)
        try:
            release = make_release_intent(raw_release)
        except ContractError:
            return self._rejected(payload, "RELEASE_INVALID", release_id)

        occurrence_id = occurrence_id_for(payload)
        try:
            prepared = self.ledger.prepare_occurrence(
                occurrence_id=occurrence_id,
                payload=payload,
                lease=session,
                release=release,
            )
        except Exception as error:
            _log_ledger_failure("prepare_occurrence", error)
            raise _reject("ledger failure") from error
        state = str(prepared.get("submission_state", ""))
        if state in {"SUBMITTED", "AMBIGUOUS", "REJECTED"}:
            return SubmissionResult(
                occurrence_id=occurrence_id,
                session_date_kst=session_date,
                release_id=release_id,
                submission_state=state,
                command_id=cast(str, prepared["command_id"]) if isinstance(prepared.get("command_id"), str) else None,
                reason=cast(str, prepared["reason"]) if isinstance(prepared.get("reason"), str) else None,
            )
        try:
            command_id = self.sender.send(
                payload=payload,
                lease=session,
                release=release,
                occurrence_id=occurrence_id,
            )
        except Exception as error:
            self._mark_ambiguous(occurrence_id, str(error))
            return SubmissionResult(
                occurrence_id=occurrence_id,
                session_date_kst=session_date,
                release_id=release_id,
                submission_state="AMBIGUOUS",
                command_id=None,
                reason="send_failed",
            )
        try:
            self.ledger.record_command(
                occurrence_id=occurrence_id,
                command_id=command_id,
                attempt_number=int(str(payload["attempt_number"])),
            )
        except Exception as error:
            _log_ledger_failure("record_command", error)
            self._mark_ambiguous(occurrence_id, "record_failed")
            return SubmissionResult(
                occurrence_id=occurrence_id,
                session_date_kst=session_date,
                release_id=release_id,
                submission_state="AMBIGUOUS",
                command_id=command_id,
                reason="record_failed",
            )
        return SubmissionResult(
            occurrence_id=occurrence_id,
            session_date_kst=session_date,
            release_id=release_id,
            submission_state="SUBMITTED",
            command_id=command_id,
        )

    def _mark_ambiguous(self, occurrence_id: str, reason: str) -> None:
        marker = getattr(self.ledger, "mark_ambiguous", None)
        if callable(marker):
            marker(occurrence_id=occurrence_id, reason=reason[:128])

    def _rejected(
        self,
        payload: Mapping[str, object],
        reason: str,
        release_id: str | None = None,
    ) -> SubmissionResult:
        result = SubmissionResult(
            occurrence_id=occurrence_id_for(payload),
            session_date_kst=session_date_kst(payload),
            release_id=release_id,
            submission_state="REJECTED",
            command_id=None,
            reason=reason,
        )
        try:
            self.ledger.record_rejection(
                occurrence_id=result.occurrence_id,
                payload=payload,
                reason=reason,
                release_id=release_id,
            )
        except Exception as error:
            _log_ledger_failure("record_rejection", error)
            raise
        return result


class Boto3SsmCommandSender:
    """Exact-document SSM adapter; no caller-controlled document name."""

    def __init__(self, client: Any, *, instance_id: str = INSTANCE_ID) -> None:
        self.client = client
        self.instance_id = instance_id

    def send(
        self,
        *,
        payload: Mapping[str, object],
        lease: Mapping[str, object],
        release: Mapping[str, object],
        occurrence_id: str,
    ) -> str:
        parameters = {
            "Phase": [str(payload["phase"])],
            "ScheduleGeneration": [str(payload["schedule_generation"])],
            "ScheduleArn": [str(payload["schedule_arn"])],
            "ScheduledTime": [str(payload["scheduled_time"])],
            "OccurrenceId": [occurrence_id],
            "SessionDateKst": [str(lease["session_date_kst"])],
            "ReleaseId": [_release_id(lease)],
            "DesiredState": ["continuous" if payload["phase"] == "start" else "stop"],
            "ImageDigest": [str(release["image_digest"])],
            "SourceSha": [str(release["source_sha"])],
            "ActivationId": [str(lease["activation_id"])],
            "ComposeShadowSha256": [str(release["compose_shadow_sha256"])],
            "ExpectedWorkerSha256": [str(release["worker_sha256"])],
            "ExpectedValidatorSha256": [str(release["validator_sha256"])],
            "ExpectedShadowDocumentSha256": [str(release["shadow_document_sha256"])],
            "ExpectedInstanceId": [self.instance_id],
            "Region": [REGION],
        }
        response = self.client.send_command(
            DocumentName=ACTIVATION_DOCUMENT_NAME,
            InstanceIds=[self.instance_id],
            Parameters=parameters,
            Comment=f"cstar:{occurrence_id}",
            TimeoutSeconds=1020,
        )
        command = response.get("Command") if isinstance(response, Mapping) else None
        command_id = command.get("CommandId") if isinstance(command, Mapping) else None
        if not isinstance(command_id, str) or COMMAND_ID_RE.fullmatch(command_id) is None:
            raise _reject("command id missing")
        return command_id


class InMemoryCStarLedger:
    """Thread-safe deterministic ledger used by unit tests and local rehearsal."""

    def __init__(self) -> None:
        self.generations: dict[str, dict[str, object]] = {}
        self.active_release: dict[str, object] | None = None
        self.releases: dict[str, dict[str, object]] = {}
        self.sessions: dict[str, dict[str, object]] = {}
        self.occurrences: dict[str, dict[str, object]] = {}
        self.commands: dict[str, str] = {}
        self.rejections: dict[str, dict[str, object]] = {}
        self._lock = RLock()

    def get_generation(self, generation: str) -> Mapping[str, object] | None:
        with self._lock:
            return self.generations.get(generation)

    def get_active_release(self) -> Mapping[str, object] | None:
        with self._lock:
            return self.active_release

    def get_release_intent(self, release_id: str) -> Mapping[str, object] | None:
        with self._lock:
            return self.releases.get(release_id)

    def get_session(self, session_date: str) -> Mapping[str, object] | None:
        with self._lock:
            return self.sessions.get(session_date)

    def prepare_occurrence(self, *, occurrence_id, payload, lease, release):
        with self._lock:
            if payload["phase"] == "start":
                existing = self.sessions.get(str(lease["session_date_kst"]))
                if existing is None:
                    self.sessions[str(lease["session_date_kst"])] = dict(lease)
                elif existing != lease:
                    raise SubmitterError("session race mismatch")
            existing_occurrence = self.occurrences.get(occurrence_id)
            if existing_occurrence is not None:
                return dict(existing_occurrence)
            item = {
                "submission_state": "SUBMITTING",
                "session_date_kst": lease["session_date_kst"],
                "release_id": release_id_for_lease(lease),
            }
            self.occurrences[occurrence_id] = item
            return dict(item)

    def record_command(self, *, occurrence_id, command_id, attempt_number):
        with self._lock:
            if occurrence_id not in self.occurrences:
                raise SubmitterError("unknown occurrence")
            self.commands[occurrence_id] = command_id
            item = self.occurrences[occurrence_id]
            item.update({"submission_state": "SUBMITTED", "command_id": command_id, "attempt_number": attempt_number})

    def record_rejection(
        self,
        *,
        occurrence_id: str,
        payload: Mapping[str, object],
        reason: str,
        release_id: str | None,
    ) -> None:
        with self._lock:
            item = {
                "occurrence_id": occurrence_id,
                "phase": payload["phase"],
                "schedule_generation": payload["schedule_generation"],
                "schedule_arn": payload["schedule_arn"],
                "scheduled_time": payload["scheduled_time"],
                "session_date_kst": session_date_kst(payload),
                "submission_state": "REJECTED",
                "reason": reason,
                "ssm_sent": False,
            }
            if release_id is not None:
                item["release_id"] = release_id
            existing = self.rejections.get(occurrence_id)
            if existing is not None and existing != item:
                raise SubmitterError("rejection audit mismatch")
            self.rejections.setdefault(occurrence_id, item)

    def mark_ambiguous(self, *, occurrence_id: str, reason: str) -> None:
        with self._lock:
            item = self.occurrences.get(occurrence_id)
            if item is not None and item.get("submission_state") not in {"SUBMITTED", "REJECTED"}:
                item.update({"submission_state": "AMBIGUOUS", "reason": reason})


class DynamoCStarLedger:
    """DynamoDB adapter using conditional writes for C* leases and attempts."""

    def __init__(self, table_name: str, client: Any) -> None:
        if not table_name or not isinstance(table_name, str):
            raise SubmitterError("table name invalid")
        self.table_name = table_name
        self.table = client
        self._resource_table = (
            client
            if type(client).__module__.startswith("boto3.resources.")
            and hasattr(client, "meta")
            else None
        )
        self.client = (
            self._resource_table.meta.client
            if self._resource_table is not None
            else client
        )

    @staticmethod
    def _key(pk: str, sk: str) -> dict[str, str]:
        return {"PK": pk, "SK": sk}

    def _get(self, pk: str, sk: str) -> dict[str, object] | None:
        if self._resource_table is not None:
            response = self._resource_table.get_item(
                Key=self._key(pk, sk), ConsistentRead=True
            )
        else:
            response = self.client.get_item(
                TableName=self.table_name,
                Key=self._key(pk, sk),
                ConsistentRead=True,
            )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return dict(item) if isinstance(item, Mapping) else None

    def _transact(self, items: list[dict[str, object]]) -> None:
        # A Table resource installs a DynamoDB transformation handler on its
        # backing client.  It expects native Python values and serializes them
        # once.  A standalone low-level client has no such handler and expects
        # AttributeValue maps, so it needs explicit serialization here.
        serializer = None
        if self._resource_table is None:
            from boto3.dynamodb.types import TypeSerializer

            serializer = TypeSerializer()
        wire_items: list[dict[str, object]] = []
        for transaction in items:
            operation = next(iter(transaction))
            raw_body = transaction[operation]
            if not isinstance(raw_body, Mapping):
                raise SubmitterError("transaction shape invalid")
            body = dict(raw_body)
            if serializer is not None and "Item" in body:
                body["Item"] = {
                    key: serializer.serialize(item)
                    for key, item in body["Item"].items()
                }
            if serializer is not None and "Key" in body:
                body["Key"] = {
                    key: serializer.serialize(item)
                    for key, item in body["Key"].items()
                }
            if serializer is not None and "ExpressionAttributeValues" in body:
                body["ExpressionAttributeValues"] = {
                    key: serializer.serialize(item)
                    for key, item in body["ExpressionAttributeValues"].items()
                }
            wire_items.append({operation: body})
        transport_body_shape: dict[str, object] = {}
        events = getattr(getattr(self.client, "meta", None), "events", None)
        event_id = f"kiwoom-cstar-ledger-shape-{id(wire_items)}"

        def capture_transport_body(*args: object, **kwargs: object) -> None:
            params = kwargs.get("params")
            if params is None and len(args) >= 2:
                params = args[1]
            if isinstance(params, Mapping):
                transport_body_shape["value"] = _ledger_body_shape(params.get("body"))

        if events is not None:
            events.register(
                "before-call.dynamodb.TransactWriteItems",
                capture_transport_body,
                unique_id=event_id,
            )
        try:
            self.client.transact_write_items(TransactItems=wire_items)
        except Exception:
            print(json.dumps({
                "event": "cstar-ledger-request-shape",
                "botocore_version": _botocore_version(),
                "client_type": type(self.client).__name__,
                "table_type": type(self.table).__name__,
                "transactions": _ledger_request_shape(wire_items),
                "transport_transactions": _ledger_transport_shape(self.client, wire_items),
                "http_transactions": transport_body_shape.get("value", "unavailable"),
            }, sort_keys=True, separators=(",", ":")))
            raise
        finally:
            if events is not None:
                events.unregister(
                    "before-call.dynamodb.TransactWriteItems",
                    unique_id=event_id,
                )

    def get_generation(self, generation: str) -> Mapping[str, object] | None:
        return self._get(f"GEN#{generation}", "META")

    def get_active_release(self) -> Mapping[str, object] | None:
        return self._get("CONTROL#CSTAR", "RELEASE")

    def get_release_intent(self, release_id: str) -> Mapping[str, object] | None:
        item = self._get(f"RELEASE#{release_id}", "META")
        if item is None:
            return None
        return {key: item[key] for key in RELEASE_INTENT_KEYS if key in item}

    def get_session(self, session_date: str) -> Mapping[str, object] | None:
        return self._get(f"SESSION#{session_date}", "LEASE")

    def prepare_occurrence(
        self,
        *,
        occurrence_id: str,
        payload: Mapping[str, object],
        lease: Mapping[str, object],
        release: Mapping[str, object],
    ) -> Mapping[str, object]:
        session_date = str(lease["session_date_kst"])
        occurrence_key = self._key(f"OCC#{occurrence_id}", "META")
        occurrence = {
            **occurrence_key,
            "schema_version": 1,
            "occurrence_id": occurrence_id,
            "session_date_kst": session_date,
            "activation_id": lease["activation_id"],
            "release_id": lease["release_id"],
            "phase": payload["phase"],
            "schedule_generation": payload["schedule_generation"],
            "schedule_arn": payload["schedule_arn"],
            "scheduled_time": payload["scheduled_time"],
            "submission_state": "SUBMITTING",
            "command_state": "UNKNOWN",
            "runtime_state": "UNKNOWN",
            "closure_state": "OPEN",
            "next_action_at": int(
                parse_utc_timestamp(str(payload["scheduled_time"])).timestamp()
            ) + 300,
        }
        transact_items: list[dict[str, object]] = []
        if payload["phase"] == "start":
            transact_items.append(
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": {
                            **self._key(f"SESSION#{session_date}", "LEASE"),
                            **dict(lease),
                        },
                        "ConditionExpression": (
                            "attribute_not_exists(PK) OR release_id = :release_id"
                        ),
                        "ExpressionAttributeValues": {
                            ":release_id": lease["release_id"],
                        },
                    }
                }
            )
        transact_items.append(
            {
                "Put": {
                    "TableName": self.table_name,
                    "Item": occurrence,
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            }
        )
        try:
            self._transact(transact_items)
        except Exception:
            existing = self._get(*occurrence_key.values())
            if existing is not None:
                return existing
            raise
        return occurrence

    def record_command(
        self,
        *,
        occurrence_id: str,
        command_id: str,
        attempt_number: int,
    ) -> None:
        command_key = self._key(f"OCC#{occurrence_id}", f"CMD#{command_id}")
        self._transact([
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": {
                            **command_key,
                            "command_id": command_id,
                            "occurrence_id": occurrence_id,
                            "attempt_number": attempt_number,
                            "command_state": "PENDING",
                        },
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                },
                {
                    "Update": {
                        "TableName": self.table_name,
                        "Key": self._key(f"OCC#{occurrence_id}", "META"),
                        "UpdateExpression": "SET submission_state = :submitted, command_id = :command_id, command_state = :pending",
                        "ConditionExpression": "submission_state = :submitting",
                        "ExpressionAttributeValues": {
                            ":submitted": "SUBMITTED",
                            ":command_id": command_id,
                            ":pending": "PENDING",
                            ":submitting": "SUBMITTING",
                        },
                    }
                },
            ])

    def mark_ambiguous(self, *, occurrence_id: str, reason: str) -> None:
        kwargs: dict[str, Any] = {
            "Key": self._key(f"OCC#{occurrence_id}", "META"),
            "UpdateExpression": "SET submission_state = :ambiguous, reason = :reason",
            "ConditionExpression": "submission_state = :submitting",
            "ExpressionAttributeValues": {
                ":ambiguous": "AMBIGUOUS",
                ":reason": reason[:128],
                ":submitting": "SUBMITTING",
            },
        }
        if self._resource_table is not None:
            self._resource_table.update_item(**kwargs)
            return
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        kwargs["TableName"] = self.table_name
        kwargs["Key"] = {
            key: serializer.serialize(item)
            for key, item in kwargs["Key"].items()
        }
        kwargs["ExpressionAttributeValues"] = {
            key: serializer.serialize(item)
            for key, item in kwargs["ExpressionAttributeValues"].items()
        }
        self.client.update_item(**kwargs)

    def record_rejection(
        self,
        *,
        occurrence_id: str,
        payload: Mapping[str, object],
        reason: str,
        release_id: str | None,
    ) -> None:
        item: dict[str, object] = {
            **self._key(f"REJ#{occurrence_id}", "META"),
            "schema_version": 1,
            "occurrence_id": occurrence_id,
            "phase": payload["phase"],
            "schedule_generation": payload["schedule_generation"],
            "schedule_arn": payload["schedule_arn"],
            "scheduled_time": payload["scheduled_time"],
            "session_date_kst": session_date_kst(payload),
            "submission_state": "REJECTED",
            "reason": reason,
            "ssm_sent": False,
        }
        if release_id is not None:
            item["release_id"] = release_id
        try:
            self._transact([
                {
                    "Put": {
                        "TableName": self.table_name,
                        "Item": item,
                        "ConditionExpression": "attribute_not_exists(PK)",
                    }
                }
            ])
        except Exception:
            existing = self._get(f"REJ#{occurrence_id}", "META")
            if existing is None:
                raise
            expected = {
                key: value for key, value in item.items()
                if key not in {"PK", "SK", "schema_version"}
            }
            actual = {
                key: value for key, value in existing.items()
                if key not in {"PK", "SK", "schema_version"}
            }
            if actual != expected:
                raise SubmitterError("rejection audit mismatch")


def release_id_for_lease(lease: Mapping[str, object]) -> str:
    value = lease.get("release_id")
    if not isinstance(value, str) or len(value) != 64:
        raise SubmitterError("lease release id invalid")
    return value


def lambda_handler(event: Mapping[str, object], _context: object) -> dict[str, object]:
    """AWS Lambda entrypoint; construction is kept here to keep the ZIP pure."""

    try:
        import boto3
    except ImportError as error:
        raise SubmitterError("boto3 runtime dependency missing") from error
    table_name = os.environ.get("CSTAR_TABLE_NAME", "")
    if not table_name:
        raise SubmitterError("CSTAR_TABLE_NAME is not configured")
    ledger = DynamoCStarLedger(table_name, boto3.resource("dynamodb").Table(table_name))
    sender = Boto3SsmCommandSender(boto3.client("ssm", region_name=REGION))
    result = CStarSubmitter(ledger, sender).submit(event)
    response = {
        "schema_version": 1,
        "occurrence_id": result.occurrence_id,
        "submission_state": result.submission_state,
        "command_id": result.command_id,
        "reason": result.reason,
    }
    print(json.dumps({
        "event": "cstar-submit",
        "phase": event.get("phase", "unknown"),
        "session_date_kst": result.session_date_kst,
        "occurrence_id": result.occurrence_id,
        "submission_state": result.submission_state,
        "reason": result.reason,
        "ssm_sent": result.command_id is not None,
    }, sort_keys=True, separators=(",", ":")))
    if result.submission_state == "REJECTED":
        try:
            boto3.client("cloudwatch", region_name=REGION).put_metric_data(
                Namespace="Kiwoom/ShadowCStar",
                MetricData=[{
                    "MetricName": "cstar_activation_rejected",
                    "Unit": "Count",
                    "Value": 1.0,
                }],
            )
        except Exception:
            print(json.dumps({
                "event": "cstar-submit-metric-failed",
                "metric": "cstar_activation_rejected",
            }, sort_keys=True, separators=(",", ":")))
    return response
