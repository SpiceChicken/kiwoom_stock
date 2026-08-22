from datetime import datetime, timezone

import pytest

from deploy.shadow_missing_run_detector import (
    ACTIVATION_WORKFLOW_ID,
    ACTIVATION_PATH,
    AUDIT_WORKFLOW_ID,
    AUDIT_PATH,
    MissingRunError,
    RunQueryResult,
    classify_occurrence,
)


CHECKED = datetime(2026, 8, 24, 0, 5, tzinfo=timezone.utc)
SHA = "a" * 40


def _run(
    *, id: int, path: str, event: str, status: str = "completed",
    conclusion: str | None = "success", created: str = "2026-08-23T23:52:00Z",
) -> dict[str, object]:
    return {
        "id": id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "head_branch": "main",
        "head_sha": SHA,
        "path": path,
        "created_at": created,
        "run_started_at": created,
        "updated_at": created,
    }


def _activation(**updates: object) -> dict[str, object]:
    created = updates.pop("created", "2026-08-23T23:52:00Z")
    value = _run(
        id=101, path=ACTIVATION_PATH, event="schedule", created=created,
    )
    value.update(updates)
    return value


def _audit(**updates: object) -> dict[str, object]:
    created = updates.pop("created", "2026-08-24T00:10:00Z")
    value = _run(
        id=201, path=AUDIT_PATH, event="workflow_run",
        created=created,
    )
    value.update(updates)
    return value


def _activation_query(*runs: dict[str, object]) -> RunQueryResult:
    return RunQueryResult.success(ACTIVATION_WORKFLOW_ID, list(runs))


def _audit_query(*runs: dict[str, object]) -> RunQueryResult:
    return RunQueryResult.success(AUDIT_WORKFLOW_ID, list(runs))


def test_start_presence_success_is_present():
    result = classify_occurrence(
        schedule="start", phase="presence", checked_at=CHECKED,
        activation_query=_activation_query(_activation()),
    )
    assert result["status"] == "ACTIVATION_PRESENT"
    assert result["occurrence_id"] == "2026-08-24#start"
    assert result["activation_run_id"] == "101"


def test_start_presence_zero_run_is_delayed_or_missing():
    result = classify_occurrence(
        schedule="start", phase="presence", checked_at=CHECKED,
        activation_query=_activation_query(),
    )
    assert result["status"] == "DELAYED_OR_MISSING"
    assert result["activation_count"] == 0


@pytest.mark.parametrize(
    "checked_at",
    [
        datetime(2026, 8, 24, 0, 4, 59, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 0, 34, 59, tzinfo=timezone.utc),
    ],
)
def test_phase_grace_cutoff_is_enforced(checked_at):
    phase = "presence" if checked_at.minute == 4 else "closure"
    with pytest.raises(MissingRunError, match="check_window_invalid"):
        classify_occurrence(
            schedule="start", phase=phase, checked_at=checked_at,
            activation_query=_activation_query(),
            audit_query=_audit_query(),
        )


def test_start_closure_zero_run_is_missing_activation():
    result = classify_occurrence(
        schedule="start", phase="closure",
        checked_at=datetime(2026, 8, 24, 0, 35, tzinfo=timezone.utc),
        activation_query=_activation_query(),
        audit_query=_audit_query(),
    )
    assert result["status"] == "MISSING_ACTIVATION"


def test_stop_closure_requires_successful_audit():
    result = classify_occurrence(
        schedule="stop", phase="closure",
        checked_at=datetime(2026, 8, 24, 7, 20, tzinfo=timezone.utc),
        activation_query=_activation_query(
            _activation(created="2026-08-24T06:36:00Z"),
        ),
        audit_query=_audit_query(_audit(created="2026-08-24T06:45:00Z")),
    )
    assert result["status"] == "CLOSED"
    assert result["desired_state"] == "stop"
    assert result["audit_run_id"] == "201"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"status": "in_progress", "conclusion": None}, "STUCK_ACTIVATION"),
        ({"conclusion": "failure"}, "ACTIVATION_FAILED"),
    ],
)
def test_closure_reports_activation_state(updates, expected):
    result = classify_occurrence(
        schedule="start", phase="closure",
        checked_at=datetime(2026, 8, 24, 0, 35, tzinfo=timezone.utc),
        activation_query=_activation_query(_activation(**updates)),
        audit_query=_audit_query(),
    )
    assert result["status"] == expected


@pytest.mark.parametrize(
    ("audit_runs", "expected"),
    [
        ([], "MISSING_AUDIT"),
        ([_audit(status="in_progress", conclusion=None)], "AUDIT_IN_PROGRESS"),
        ([_audit(conclusion="failure")], "AUDIT_FAILED"),
        ([_audit(), _audit(id=202)], "DUPLICATE_AUDIT"),
    ],
)
def test_closure_reports_audit_state(audit_runs, expected):
    result = classify_occurrence(
        schedule="start", phase="closure",
        checked_at=datetime(2026, 8, 24, 0, 35, tzinfo=timezone.utc),
        activation_query=_activation_query(_activation()),
        audit_query=_audit_query(*audit_runs),
    )
    assert result["status"] == expected


def test_duplicate_activation_is_fail_closed():
    result = classify_occurrence(
        schedule="start", phase="presence", checked_at=CHECKED,
        activation_query=_activation_query(_activation(), _activation(id=102)),
    )
    assert result["status"] == "DUPLICATE_ACTIVATION"
    assert result["activation_count"] == 2


@pytest.mark.parametrize(
    "updates",
    [
        {"updated_at": "2026-08-24T00:06:00Z"},
        {
            "created": "2026-08-24T00:06:00Z",
        },
    ],
)
def test_future_lifecycle_timestamp_is_rejected(updates):
    with pytest.raises(MissingRunError, match="run_timing_future"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=_activation_query(_activation(**updates)),
        )


def test_api_failure_is_not_a_missing_activation():
    with pytest.raises(MissingRunError, match="api_visibility_failure"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=RunQueryResult.failure(ACTIVATION_WORKFLOW_ID),
        )


@pytest.mark.parametrize("audit_query", [None, "failure"])
def test_closure_requires_successful_audit_query(audit_query):
    query = (
        None
        if audit_query is None
        else RunQueryResult.failure(AUDIT_WORKFLOW_ID)
    )
    with pytest.raises(MissingRunError, match="api_visibility_failure"):
        classify_occurrence(
            schedule="start", phase="closure",
            checked_at=datetime(2026, 8, 24, 0, 35, tzinfo=timezone.utc),
            activation_query=_activation_query(_activation()),
            audit_query=query,
        )


def test_wrong_workflow_query_id_is_rejected():
    with pytest.raises(MissingRunError, match="api_visibility_failure"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=RunQueryResult.success(999, []),
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"event": "workflow_dispatch"},
        {"head_branch": "feature"},
        {"path": "other.yml"},
        {"head_sha": "not-a-sha"},
        {"id": True},
        {"conclusion": {"unsafe": True}},
        {"created_at": "2026-08-24T00:52:00+00:00"},
        {"updated_at": "2026-08-23T23:51:00Z"},
        {
            "run_started_at": "2026-08-23T23:54:00Z",
            "updated_at": "2026-08-23T23:53:00Z",
        },
    ],
)
def test_run_projection_is_exact_and_fail_closed(updates):
    with pytest.raises(MissingRunError, match="run_"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=_activation_query(_activation(**updates)),
        )


@pytest.mark.parametrize(
    "checked_at",
    [
        datetime(2026, 8, 22, 9, 5, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 7, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 24, 12, 0),
    ],
)
def test_invalid_check_windows_fail_closed(checked_at):
    with pytest.raises(MissingRunError):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=checked_at,
            activation_query=_activation_query(),
        )


def test_late_run_is_considered_at_closure_when_it_exists_before_check():
    result = classify_occurrence(
        schedule="start", phase="closure",
        checked_at=datetime(2026, 8, 24, 0, 35, tzinfo=timezone.utc),
        activation_query=_activation_query(
            _activation(created="2026-08-24T00:20:00Z"),
        ),
        audit_query=_audit_query(),
    )
    assert result["status"] == "MISSING_AUDIT"
    assert result["activation_count"] == 1


def test_oversized_run_list_is_rejected():
    with pytest.raises(MissingRunError, match="run_list_oversized"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=RunQueryResult.success(
                ACTIVATION_WORKFLOW_ID,
                [_activation(id=index) for index in range(101)],
            ),
        )


@pytest.mark.parametrize("runs", [None, "unsafe"])
def test_run_list_shape_is_rejected(runs):
    with pytest.raises(MissingRunError, match="run_list_invalid"):
        RunQueryResult.success(ACTIVATION_WORKFLOW_ID, runs)


def test_non_mapping_run_is_rejected():
    with pytest.raises(MissingRunError, match="run_invalid"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at=CHECKED,
            activation_query=RunQueryResult.success(
                ACTIVATION_WORKFLOW_ID, ["unsafe"],
            ),
        )


def test_checked_at_must_be_aware_datetime():
    with pytest.raises(MissingRunError, match="check_time_invalid"):
        classify_occurrence(
            schedule="start", phase="presence", checked_at="unsafe",
            activation_query=_activation_query(),
        )
