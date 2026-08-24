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
        self.ledger.record_rejection(
            occurrence_id=result.occurrence_id,
            payload=payload,
            reason=reason,
            release_id=release_id,
        )
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
        self.client = getattr(getattr(client, "meta", None), "client", client)

    @staticmethod
    def _key(pk: str, sk: str) -> dict[str, str]:
        return {"PK": pk, "SK": sk}

    def _get(self, pk: str, sk: str) -> dict[str, object] | None:
        if hasattr(self.table, "meta"):
            response = self.table.get_item(Key=self._key(pk, sk), ConsistentRead=True)
        else:
            response = self.client.get_item(
                TableName=self.table_name,
                Key=self._key(pk, sk),
                ConsistentRead=True,
            )
        item = response.get("Item") if isinstance(response, Mapping) else None
        return dict(item) if isinstance(item, Mapping) else None

    def _transact(self, items: list[dict[str, object]]) -> None:
        from boto3.dynamodb.types import TypeSerializer

        serializer = TypeSerializer()
        wire_items: list[dict[str, object]] = []
        for transaction in items:
            operation = next(iter(transaction))
            raw_body = transaction[operation]
            if not isinstance(raw_body, Mapping):
                raise SubmitterError("transaction shape invalid")
            body = dict(raw_body)
            if "Item" in body:
                body["Item"] = {
                    key: serializer.serialize(item)
                    for key, item in body["Item"].items()
                }
            if "Key" in body:
                body["Key"] = {
                    key: serializer.serialize(item)
                    for key, item in body["Key"].items()
                }
            if "ExpressionAttributeValues" in body:
                body["ExpressionAttributeValues"] = {
                    key: serializer.serialize(item)
                    for key, item in body["ExpressionAttributeValues"].items()
                }
            wire_items.append({operation: body})
        self.client.transact_write_items(TransactItems=wire_items)

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
        if hasattr(self.table, "meta"):
            self.table.update_item(**kwargs)
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
