#!/usr/bin/env python3
"""Pure contracts for the C* shadow-session scheduling protocol.

This module deliberately has no AWS, filesystem, network, or clock side effects.
Cloud adapters and the EC2 fence use these values as their shared protocol
boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Final, Mapping, cast
from zoneinfo import ZoneInfo


KST: Final = ZoneInfo("Asia/Seoul")
SCHEMA_VERSION: Final = 1
RETENTION_DAYS: Final = 400
MAX_STRING_BYTES: Final = 512
MAX_ATTEMPT_NUMBER: Final = 9

SCHEDULER_PAYLOAD_KEYS: Final = frozenset(
    {
        "schema_version",
        "phase",
        "schedule_generation",
        "schedule_arn",
        "scheduled_time",
        "execution_id",
        "attempt_number",
    }
)
RELEASE_INTENT_KEYS: Final = frozenset(
    {
        "image_digest",
        "source_sha",
        "compose_shadow_sha256",
        "worker_sha256",
        "validator_sha256",
        "shadow_document_sha256",
        "rollout_attempt_id",
    }
)
SESSION_LEASE_KEYS: Final = frozenset(
    {
        "schema_version",
        "session_date_kst",
        "activation_id",
        "release_id",
        "schedule_generation",
    }
)
OCCURRENCE_ID_KEYS: Final = (
    "schema_version",
    "schedule_generation",
    "schedule_arn",
    "scheduled_time",
    "phase",
    "session_date_kst",
)

SUBMISSION_STATES: Final = (
    "CLAIMED",
    "SUBMITTING",
    "SUBMITTED",
    "AMBIGUOUS",
    "REJECTED",
)
COMMAND_STATES: Final = (
    "UNKNOWN",
    "PENDING",
    "IN_PROGRESS",
    "SUCCESS",
    "FAILED",
    "TIMED_OUT",
    "CANCELLED",
    "UNDELIVERABLE",
    "TERMINATED",
)
RUNTIME_STATES: Final = (
    "UNKNOWN",
    "ACCEPTED",
    "CLOSED_HOLIDAY",
    "STOPPED",
    "DUPLICATE",
    "STALE_GENERATION",
    "FAILED",
    "AMBIGUOUS",
)
CLOSURE_STATES: Final = ("OPEN", "EVIDENCE_PENDING", "CLOSED", "ALERTED")

DIAGNOSTIC_CATEGORIES: Final = frozenset(
    {
        "ambiguous",
        "duplicate",
        "evidence_failure",
        "holiday",
        "late_trigger",
        "no_session",
        "ssm_failure",
        "stale_generation",
    }
)
METRIC_NAMES: Final = frozenset(
    {
        "cstar_activation_accepted",
        "cstar_activation_rejected",
        "cstar_duplicate_occurrence",
        "cstar_ambiguous_occurrence",
        "cstar_observer_alerted",
        "cstar_evidence_exported",
    }
)

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_GENERATION_RE = re.compile(r"cstar-g[0-9]{6,}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_UTC_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)


class ContractError(ValueError):
    """Raised when an untrusted C* value is not an exact protocol value."""


def _invalid() -> ContractError:
    return ContractError("invalid")


def _bounded_string(value: object, *, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid()
    if len(value.encode("utf-8")) > MAX_STRING_BYTES:
        raise _invalid()
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _invalid()
    return value


def _exact_int(value: object, *, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise _invalid()
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str]) -> None:
    if set(value) != expected:
        raise _invalid()


def canonical_json_bytes(value: object) -> bytes:
    """Return deterministic compact JSON bytes, rejecting non-JSON numbers."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError):
        raise _invalid() from None
    return encoded


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def parse_utc_timestamp(value: object) -> datetime:
    text = _bounded_string(value)
    if _UTC_RE.fullmatch(text) is None:
        raise _invalid()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise _invalid() from None
    return parsed.replace(tzinfo=timezone.utc)


def format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise _invalid()
    normalized = value.astimezone(timezone.utc).replace(microsecond=0)
    return normalized.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_kst_date(value: object) -> date:
    text = _bounded_string(value)
    if _DATE_RE.fullmatch(text) is None:
        raise _invalid()
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _invalid() from None


def validate_scheduler_payload(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and copy the exact payload sent by EventBridge Scheduler."""

    if not isinstance(value, Mapping):
        raise _invalid()
    _exact_keys(value, SCHEDULER_PAYLOAD_KEYS)
    if _exact_int(value.get("schema_version"), minimum=1, maximum=1) != SCHEMA_VERSION:
        raise _invalid()
    phase = _bounded_string(value.get("phase"))
    if phase not in {"start", "stop"}:
        raise _invalid()
    generation = _bounded_string(value.get("schedule_generation"), pattern=_GENERATION_RE)
    arn = _bounded_string(value.get("schedule_arn"), pattern=_ID_RE)
    scheduled = format_utc_timestamp(parse_utc_timestamp(value.get("scheduled_time")))
    execution_id = _bounded_string(value.get("execution_id"), pattern=_ID_RE)
    attempt_text = _bounded_string(
        value.get("attempt_number"),
        pattern=re.compile(r"[0-9]"),
    )
    _exact_int(int(attempt_text), minimum=0, maximum=MAX_ATTEMPT_NUMBER)
    return {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "schedule_generation": generation,
        "schedule_arn": arn,
        "scheduled_time": scheduled,
        "execution_id": execution_id,
        "attempt_number": attempt_text,
    }


def project_scheduler_context(
    context: Mapping[str, object],
    *,
    phase: str,
    schedule_generation: str,
) -> dict[str, object]:
    """Project AWS Scheduler context attributes into the exact payload.

    Extra context attributes are intentionally ignored; the returned payload is
    still strict and contains no delivery metadata in the occurrence identity.
    """

    if not isinstance(context, Mapping):
        raise _invalid()
    aliases: dict[str, tuple[str, ...]] = {
        "schedule_arn": ("aws.scheduler.schedule-arn", "schedule_arn"),
        "scheduled_time": ("aws.scheduler.scheduled-time", "scheduled_time"),
        "execution_id": ("aws.scheduler.execution-id", "execution_id"),
        "attempt_number": ("aws.scheduler.attempt-number", "attempt_number"),
    }
    projected: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "phase": phase,
        "schedule_generation": schedule_generation,
    }
    for field, names in aliases.items():
        found = [context[name] for name in names if name in context]
        if len(found) != 1:
            raise _invalid()
        projected[field] = found[0]
    return validate_scheduler_payload(projected)


def session_date_kst(value: Mapping[str, object]) -> str:
    payload = validate_scheduler_payload(value)
    return parse_utc_timestamp(payload["scheduled_time"]).astimezone(KST).date().isoformat()


def activation_id_for_session(session_date: str) -> str:
    parsed = _parse_kst_date(session_date)
    return f"shadow-session-{parsed:%Y%m%d}"


def _expected_kst_time(phase: str) -> tuple[int, int]:
    if phase == "start":
        return 8, 50
    if phase == "stop":
        return 15, 35
    raise _invalid()


def validate_scheduled_slot(value: Mapping[str, object]) -> datetime:
    """Require the exact weekday KST slot for the payload phase."""

    payload = validate_scheduler_payload(value)
    scheduled = parse_utc_timestamp(payload["scheduled_time"]).astimezone(KST)
    hour, minute = _expected_kst_time(cast(str, payload["phase"]))
    if scheduled.weekday() > 4 or (scheduled.hour, scheduled.minute, scheduled.second) != (
        hour,
        minute,
        0,
    ):
        raise _invalid()
    return scheduled


def occurrence_identity_payload(value: Mapping[str, object]) -> dict[str, object]:
    payload = validate_scheduler_payload(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "schedule_generation": cast(str, payload["schedule_generation"]),
        "schedule_arn": payload["schedule_arn"],
        "scheduled_time": payload["scheduled_time"],
        "phase": payload["phase"],
        "session_date_kst": session_date_kst(payload),
    }


def occurrence_id_for(value: Mapping[str, object]) -> str:
    return canonical_sha256(occurrence_identity_payload(value))


def make_release_intent(value: Mapping[str, object]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _invalid()
    _exact_keys(value, RELEASE_INTENT_KEYS)
    result: dict[str, str] = {}
    for key in RELEASE_INTENT_KEYS:
        if key == "source_sha":
            pattern = re.compile(r"[0-9a-f]{40}")
        elif key == "image_digest":
            pattern = re.compile(r"ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}")
        elif key == "rollout_attempt_id":
            pattern = _ID_RE
        else:
            pattern = _HEX64_RE
        item = _bounded_string(value.get(key), pattern=pattern)
        result[key] = item
    return {key: result[key] for key in sorted(result)}


def release_id_for(value: Mapping[str, object]) -> str:
    return canonical_sha256(make_release_intent(value))


def make_session_lease(
    scheduler_payload: Mapping[str, object],
    *,
    release_id: str,
) -> dict[str, str | int]:
    payload = validate_scheduler_payload(scheduler_payload)
    _bounded_string(release_id, pattern=_HEX64_RE)
    session = session_date_kst(payload)
    lease: dict[str, str | int] = {
        "schema_version": SCHEMA_VERSION,
        "session_date_kst": session,
        "activation_id": activation_id_for_session(session),
        "release_id": release_id,
        "schedule_generation": cast(str, payload["schedule_generation"]),
    }
    return validate_session_lease(lease)


def validate_session_lease(value: Mapping[str, object]) -> dict[str, str | int]:
    if not isinstance(value, Mapping):
        raise _invalid()
    _exact_keys(value, SESSION_LEASE_KEYS)
    if _exact_int(value.get("schema_version"), minimum=1, maximum=1) != SCHEMA_VERSION:
        raise _invalid()
    session = _parse_kst_date(value.get("session_date_kst"))
    activation = _bounded_string(value.get("activation_id"), pattern=re.compile(r"shadow-session-[0-9]{8}"))
    if activation != activation_id_for_session(session.isoformat()):
        raise _invalid()
    release_id = _bounded_string(value.get("release_id"), pattern=_HEX64_RE)
    generation = _bounded_string(value.get("schedule_generation"), pattern=_GENERATION_RE)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_date_kst": session.isoformat(),
        "activation_id": activation,
        "release_id": release_id,
        "schedule_generation": generation,
    }


_TRANSITIONS: Final = {
    "submission": {
        "CLAIMED": frozenset({"CLAIMED", "SUBMITTING", "REJECTED"}),
        "SUBMITTING": frozenset({"SUBMITTING", "SUBMITTED", "AMBIGUOUS", "REJECTED"}),
        "SUBMITTED": frozenset({"SUBMITTED", "AMBIGUOUS"}),
        "AMBIGUOUS": frozenset({"AMBIGUOUS"}),
        "REJECTED": frozenset({"REJECTED"}),
    },
    "command": {
        "UNKNOWN": frozenset({"UNKNOWN", "PENDING"}),
        "PENDING": frozenset({"PENDING", "IN_PROGRESS", "SUCCESS", "FAILED", "TIMED_OUT", "CANCELLED", "UNDELIVERABLE", "TERMINATED"}),
        "IN_PROGRESS": frozenset({"IN_PROGRESS", "SUCCESS", "FAILED", "TIMED_OUT", "CANCELLED", "UNDELIVERABLE", "TERMINATED"}),
        "SUCCESS": frozenset({"SUCCESS"}),
        "FAILED": frozenset({"FAILED"}),
        "TIMED_OUT": frozenset({"TIMED_OUT"}),
        "CANCELLED": frozenset({"CANCELLED"}),
        "UNDELIVERABLE": frozenset({"UNDELIVERABLE"}),
        "TERMINATED": frozenset({"TERMINATED"}),
    },
    "runtime": {
        "UNKNOWN": frozenset({"UNKNOWN", "ACCEPTED", "CLOSED_HOLIDAY", "STOPPED", "DUPLICATE", "STALE_GENERATION", "FAILED", "AMBIGUOUS"}),
        "ACCEPTED": frozenset({"ACCEPTED", "STOPPED", "FAILED", "AMBIGUOUS"}),
        "CLOSED_HOLIDAY": frozenset({"CLOSED_HOLIDAY"}),
        "STOPPED": frozenset({"STOPPED"}),
        "DUPLICATE": frozenset({"DUPLICATE"}),
        "STALE_GENERATION": frozenset({"STALE_GENERATION"}),
        "FAILED": frozenset({"FAILED"}),
        "AMBIGUOUS": frozenset({"AMBIGUOUS"}),
    },
    "closure": {
        "OPEN": frozenset({"OPEN", "EVIDENCE_PENDING", "ALERTED"}),
        "EVIDENCE_PENDING": frozenset({"EVIDENCE_PENDING", "CLOSED", "ALERTED"}),
        "CLOSED": frozenset({"CLOSED"}),
        # An evidence-only recovery may reopen an alerted closure. The
        # observer guards this transition with SUCCESS/STOPPED and a bounded
        # retry counter; activation state is never reopened.
        "ALERTED": frozenset({"ALERTED", "EVIDENCE_PENDING"}),
    },
}


def validate_state_transition(dimension: str, current: str, target: str) -> str:
    allowed = _TRANSITIONS.get(dimension)
    if allowed is None or current not in allowed or target not in allowed:
        raise _invalid()
    if target not in allowed[current]:
        raise ContractError("invalid state transition")
    return target


def allowed_state_transitions(dimension: str, current: str) -> tuple[str, ...]:
    allowed = _TRANSITIONS.get(dimension)
    if allowed is None or current not in allowed:
        raise _invalid()
    return tuple(sorted(allowed[current]))


def retention_ttl_epoch(
    closure_state: str,
    *,
    terminal_at_epoch: int | None,
    retention_days: int = RETENTION_DAYS,
) -> int | None:
    """Return a positive TTL only after terminal closure; never return zero."""

    if closure_state not in CLOSURE_STATES:
        raise _invalid()
    if type(retention_days) is not int or retention_days <= 0 or retention_days > 3_650:
        raise _invalid()
    if closure_state not in {"CLOSED", "ALERTED"}:
        if terminal_at_epoch is not None:
            raise _invalid()
        return None
    if type(terminal_at_epoch) is not int or terminal_at_epoch <= 0:
        raise _invalid()
    return terminal_at_epoch + timedelta(days=retention_days).days * 86_400


def diagnostic_category(value: object) -> str:
    category = _bounded_string(value, pattern=re.compile(r"[a-z][a-z0-9_]{0,31}"))
    if category not in DIAGNOSTIC_CATEGORIES:
        raise _invalid()
    return category


def metric_name(value: object) -> str:
    name = _bounded_string(value, pattern=re.compile(r"[a-z][a-z0-9_]{0,63}"))
    if name not in METRIC_NAMES:
        raise _invalid()
    return name


@dataclass(frozen=True)
class OccurrenceIdentity:
    """Validated identity used as the idempotency key for one phase occurrence."""

    occurrence_id: str
    session_date_kst: str
    activation_id: str
    phase: str


def occurrence_identity(value: Mapping[str, object]) -> OccurrenceIdentity:
    payload = validate_scheduler_payload(value)
    identity = occurrence_identity_payload(payload)
    return OccurrenceIdentity(
        occurrence_id=canonical_sha256(identity),
        session_date_kst=str(identity["session_date_kst"]),
        activation_id=activation_id_for_session(str(identity["session_date_kst"])),
        phase=str(identity["phase"]),
    )
