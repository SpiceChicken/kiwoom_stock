#!/usr/bin/env python3
"""Classify missing or incomplete Shadow schedule occurrences.

This module deliberately has no AWS, GitHub, Slack, or filesystem dependency.
An adapter may feed it a bounded projection of GitHub workflow-run responses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
ACTIVATION_WORKFLOW_ID = 325559548
AUDIT_WORKFLOW_ID = 339873147
ACTIVATION_PATH = ".github/workflows/cd-shadow-worker-activation.yml"
AUDIT_PATH = ".github/workflows/cd-shadow-schedule-audit.yml"
RUN_ID_RE = re.compile(r"[1-9][0-9]{0,19}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
UTC_TIMESTAMP_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:"
    r"[0-9]{2}:[0-9]{2}Z"
)
MAX_RUNS = 100
EARLY_TOLERANCE = timedelta(minutes=5)
MAX_CHECK_AGE = timedelta(hours=2)
PHASES = {"presence", "closure"}
PHASE_GRACE = {
    "presence": timedelta(minutes=15),
    "closure": timedelta(minutes=45),
}
RUN_STATUSES = {
    "queued", "in_progress", "completed", "requested", "waiting", "pending",
}
RUN_CONCLUSIONS = {
    None, "success", "failure", "neutral", "cancelled", "skipped",
    "timed_out", "action_required", "stale",
}


class MissingRunError(ValueError):
    """A value-free missing-run input or occurrence rejection."""


@dataclass(frozen=True)
class ScheduleContract:
    desired_state: str
    label: str
    hour: int
    minute: int
    activation_workflow_id: int = ACTIVATION_WORKFLOW_ID
    audit_workflow_id: int = AUDIT_WORKFLOW_ID


CONTRACTS = {
    "start": ScheduleContract("continuous", "start", 8, 50),
    "stop": ScheduleContract("stop", "stop", 15, 35),
}


RUN_KEYS = {
    "id", "event", "status", "conclusion", "head_branch", "head_sha",
    "path", "created_at", "run_started_at", "updated_at",
}


@dataclass(frozen=True)
class ValidRun:
    run_id: str
    event: str
    status: str
    conclusion: str | None
    created_at: datetime
    run_started_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class RunQueryResult:
    """One bounded, successful or failed workflow-runs API query."""

    workflow_id: int
    runs: tuple[Mapping[str, object], ...]
    api_error: bool = False

    @classmethod
    def success(
        cls, workflow_id: int, runs: Sequence[Mapping[str, object]],
    ) -> "RunQueryResult":
        if type(workflow_id) is not int or workflow_id <= 0:
            raise MissingRunError("query_invalid")
        if not isinstance(runs, (list, tuple)):
            raise MissingRunError("run_list_invalid")
        if len(runs) > MAX_RUNS:
            raise MissingRunError("run_list_oversized")
        if any(not isinstance(run, Mapping) for run in runs):
            raise MissingRunError("run_invalid")
        return cls(workflow_id, tuple(runs))

    @classmethod
    def failure(cls, workflow_id: int) -> "RunQueryResult":
        if type(workflow_id) is not int or workflow_id <= 0:
            raise MissingRunError("query_invalid")
        return cls(workflow_id, (), api_error=True)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or UTC_TIMESTAMP_RE.fullmatch(value) is None:
        raise MissingRunError("run_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise MissingRunError("run_invalid") from None
    return parsed.replace(tzinfo=UTC)


def _validate_run(
    value: Mapping[str, object], *, path: str, event: str,
) -> ValidRun:
    head_sha = value.get("head_sha")
    conclusion = value.get("conclusion")
    if (
        set(value) != RUN_KEYS
        or type(value.get("id")) is not int
        or not RUN_ID_RE.fullmatch(str(value.get("id")))
        or value.get("event") != event
        or not isinstance(value.get("status"), str)
        or value.get("status") not in RUN_STATUSES
        or (
            conclusion is not None
            and (
                not isinstance(conclusion, str)
                or conclusion not in RUN_CONCLUSIONS
            )
        )
        or value.get("head_branch") != "main"
        or not isinstance(head_sha, str)
        or SHA_RE.fullmatch(head_sha) is None
        or value.get("path") != path
    ):
        raise MissingRunError("run_invalid")
    created = _parse_timestamp(value.get("created_at"))
    started = _parse_timestamp(value.get("run_started_at"))
    updated = _parse_timestamp(value.get("updated_at"))
    if started < created or updated < created or updated < started:
        raise MissingRunError("run_timing_invalid")
    return ValidRun(
        run_id=str(value["id"]),
        event=event,
        status=str(value["status"]),
        conclusion=conclusion if isinstance(conclusion, str) else None,
        created_at=created,
        run_started_at=started,
        updated_at=updated,
    )


def _checked_at(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise MissingRunError("check_time_invalid")
    return value.astimezone(UTC)


def _occurrence_at(
    checked_at: datetime, contract: ScheduleContract, *, phase: str,
) -> datetime:
    checked_utc = _checked_at(checked_at)
    local = checked_utc.astimezone(KST)
    if local.weekday() >= 5:
        raise MissingRunError("occurrence_not_weekday")
    expected_local = local.replace(
        hour=contract.hour, minute=contract.minute, second=0, microsecond=0,
    )
    expected = expected_local.astimezone(UTC)
    age = checked_utc - expected
    if age < PHASE_GRACE[phase] or age > MAX_CHECK_AGE:
        raise MissingRunError("check_window_invalid")
    return expected


def _candidate_runs(
    values: Sequence[Mapping[str, object]], *, path: str, event: str,
    expected: datetime, checked_at: datetime,
) -> list[ValidRun]:
    if not isinstance(values, (list, tuple)):
        raise MissingRunError("run_list_invalid")
    if len(values) > MAX_RUNS:
        raise MissingRunError("run_list_oversized")
    checked_utc = _checked_at(checked_at)
    lower = expected - EARLY_TOLERANCE
    candidates: list[ValidRun] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise MissingRunError("run_invalid")
        run = _validate_run(value, path=path, event=event)
        if (
            run.created_at > checked_utc
            or run.run_started_at > checked_utc
            or run.updated_at > checked_utc
        ):
            raise MissingRunError("run_timing_future")
        if lower <= run.created_at <= checked_utc:
            candidates.append(run)
    return sorted(candidates, key=lambda item: (item.created_at, item.run_id))


def _result(
    *, contract: ScheduleContract, phase: str, expected: datetime,
    checked_at: datetime, status: str, activations: Sequence[ValidRun],
    audits: Sequence[ValidRun],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "occurrence_id": expected.astimezone(KST).strftime("%Y-%m-%d#")
        + contract.label,
        "desired_state": contract.desired_state,
        "phase": phase,
        "status": status,
        "expected_at_utc": expected.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checked_at_utc": _checked_at(checked_at).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "activation_count": len(activations),
        "audit_count": len(audits),
        "activation_run_id": (
            activations[0].run_id if len(activations) == 1 else None
        ),
        "audit_run_id": audits[0].run_id if len(audits) == 1 else None,
    }


def classify_occurrence(
    *,
    schedule: str,
    phase: str,
    checked_at: datetime,
    activation_query: RunQueryResult,
    audit_query: RunQueryResult | None = None,
) -> dict[str, object]:
    """Classify one bounded occurrence from projected API run objects.

    `checked_at` is the detector invocation time. The function never treats an
    API error as an empty list; adapters must raise `MissingRunError` instead.
    """
    contract = CONTRACTS.get(schedule)
    if contract is None or phase not in PHASES:
        raise MissingRunError("request_invalid")
    expected = _occurrence_at(checked_at, contract, phase=phase)
    if (
        activation_query.workflow_id != contract.activation_workflow_id
        or activation_query.api_error
    ):
        raise MissingRunError("api_visibility_failure")
    activations = _candidate_runs(
        activation_query.runs,
        path=ACTIVATION_PATH,
        event="schedule",
        expected=expected,
        checked_at=checked_at,
    )
    audits: list[ValidRun] = []
    if phase == "closure":
        if (
            audit_query is None
            or audit_query.workflow_id != contract.audit_workflow_id
            or audit_query.api_error
        ):
            raise MissingRunError("api_visibility_failure")
        audits = _candidate_runs(
            audit_query.runs,
            path=AUDIT_PATH,
            event="workflow_run",
            expected=expected,
            checked_at=checked_at,
        )
    if len(activations) > 1:
        return _result(
            contract=contract, phase=phase, expected=expected,
            checked_at=checked_at, status="DUPLICATE_ACTIVATION",
            activations=activations, audits=audits,
        )
    if not activations:
        status = (
            "DELAYED_OR_MISSING"
            if phase == "presence" else "MISSING_ACTIVATION"
        )
        return _result(
            contract=contract, phase=phase, expected=expected,
            checked_at=checked_at, status=status, activations=activations,
            audits=audits,
        )
    activation = activations[0]
    if activation.status != "completed":
        status = "IN_PROGRESS" if phase == "presence" else "STUCK_ACTIVATION"
        return _result(
            contract=contract, phase=phase, expected=expected,
            checked_at=checked_at, status=status, activations=activations,
            audits=audits,
        )
    if activation.conclusion != "success":
        return _result(
            contract=contract, phase=phase, expected=expected,
            checked_at=checked_at, status="ACTIVATION_FAILED",
            activations=activations, audits=audits,
        )
    if phase == "presence":
        return _result(
            contract=contract, phase=phase, expected=expected,
            checked_at=checked_at, status="ACTIVATION_PRESENT",
            activations=activations, audits=audits,
        )
    if len(audits) > 1:
        status = "DUPLICATE_AUDIT"
    elif not audits:
        status = "MISSING_AUDIT"
    elif audits[0].status != "completed":
        status = "AUDIT_IN_PROGRESS"
    elif audits[0].conclusion != "success":
        status = "AUDIT_FAILED"
    else:
        status = "CLOSED"
    return _result(
        contract=contract, phase=phase, expected=expected,
        checked_at=checked_at, status=status, activations=activations,
        audits=audits,
    )
