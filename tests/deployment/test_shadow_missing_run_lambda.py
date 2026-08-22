from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from deploy.shadow_missing_run_detector import (
    ACTIVATION_PATH,
    ACTIVATION_WORKFLOW_ID,
    AUDIT_PATH,
    AUDIT_WORKFLOW_ID,
    RunQueryResult,
)
from deploy.shadow_missing_run_lambda import (
    DetectorAdapterError,
    MemoryClaimStore,
    evaluate,
    main,
    process,
    query_workflow_runs,
)


CHECKED = datetime(2026, 8, 24, 0, 5, tzinfo=timezone.utc)
SHA = "a" * 40


def _run(
    *, id: int, path: str, event: str, created: str,
    status: str = "completed", conclusion: str | None = "success",
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


def _activation() -> dict[str, object]:
    return _run(
        id=101,
        path=ACTIVATION_PATH,
        event="schedule",
        created="2026-08-23T23:52:00Z",
    )


def _audit() -> dict[str, object]:
    return _run(
        id=201,
        path=AUDIT_PATH,
        event="workflow_run",
        created="2026-08-24T00:10:00Z",
    )


def _query(*runs: dict[str, object]) -> RunQueryResult:
    return RunQueryResult.success(ACTIVATION_WORKFLOW_ID, list(runs))


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        del args


def _opener(response: _Response, captured: list[Any]):
    def open(request: Any, *, timeout: float) -> _Response:
        captured.append((request, timeout))
        return response

    return open


def test_query_projects_only_bounded_workflow_run_fields():
    captured: list[Any] = []
    body = json.dumps({
        "total_count": 1,
        "workflow_runs": [{
            **_activation(),
            "name": "ignored by projection",
        }],
    }).encode()
    result = query_workflow_runs(
        ACTIVATION_WORKFLOW_ID,
        "schedule",
        opener=_opener(_Response(200, body), captured),
    )
    assert result.workflow_id == ACTIVATION_WORKFLOW_ID
    assert len(result.runs) == 1
    assert set(result.runs[0]) == {
        "id", "event", "status", "conclusion", "head_branch", "head_sha",
        "path", "created_at", "run_started_at", "updated_at",
    }
    request, timeout = captured[0]
    assert request.get_method() == "GET"
    assert "branch=main" in request.full_url
    assert "event=schedule" in request.full_url
    assert "per_page=100" in request.full_url
    assert timeout == 5.0


@pytest.mark.parametrize(
    "response",
    [
        _Response(500, b"{}"),
        _Response(200, b"not-json"),
        _Response(200, json.dumps({"workflow_runs": []}).encode()),
    ],
)
def test_query_fails_closed_on_visibility_or_shape(response):
    with pytest.raises(DetectorAdapterError):
        query_workflow_runs(
            ACTIVATION_WORKFLOW_ID,
            "schedule",
            opener=_opener(response, []),
        )


def test_evaluate_success_and_api_failure_are_distinct():
    def query(workflow_id: int, event: str) -> RunQueryResult:
        if workflow_id == ACTIVATION_WORKFLOW_ID:
            assert event == "schedule"
            return _query(_activation())
        assert workflow_id == AUDIT_WORKFLOW_ID
        assert event == "workflow_run"
        return RunQueryResult.success(AUDIT_WORKFLOW_ID, [_audit()])

    record = evaluate(
        {"schedule": "start", "phase": "closure"},
        query=query,
        checked_at=CHECKED.replace(minute=35),
    )
    assert record["status"] == "CLOSED"
    assert record["schedule"] == "start"
    assert record["detector_schema_version"] == 1

    def unavailable(_workflow_id: int, _event: str) -> RunQueryResult:
        raise DetectorAdapterError("api_visibility_failure")

    failure = evaluate(
        {"schedule": "start", "phase": "presence"},
        query=unavailable,
        checked_at=CHECKED,
    )
    assert failure["status"] == "API_VISIBILITY_FAILURE"
    assert failure["activation_count"] == 0
    assert failure["schema_version"] == 1


class _Sink:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def send(self, record: dict[str, object]) -> None:
        self.records.append(record)


class _Metrics:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def emit(self, record: dict[str, object]) -> None:
        self.records.append(record)


def test_process_claims_each_alert_once_and_emits_heartbeat():
    def query(_workflow_id: int, _event: str) -> RunQueryResult:
        return _query()

    store = MemoryClaimStore(set(), {})
    sink = _Sink()
    metrics = _Metrics()
    event = {
        "schedule": "start",
        "phase": "presence",
        "checked_at_utc": "2026-08-24T00:05:00Z",
    }
    first = process(
        event,
        query=query,
        store=store,
        sink=sink,
        metrics=metrics,
        alert_enabled=True,
    )
    second = process(
        event,
        query=query,
        store=store,
        sink=sink,
        metrics=metrics,
        alert_enabled=True,
    )
    assert first["status"] == "DELAYED_OR_MISSING"
    assert first["alert_claimed"] is True
    assert first["alert_delivered"] is True
    assert second["alert_claimed"] is False
    assert second["alert_delivered"] is False
    assert len(sink.records) == 1
    assert len(metrics.records) == 2
    assert len(store.heartbeats) == 1


def test_metrics_only_does_not_claim_or_send_alert():
    store = MemoryClaimStore(set(), {})
    sink = _Sink()
    record = process(
        {"schedule": "start", "phase": "presence"},
        query=lambda _workflow_id, _event: _query(),
        store=store,
        sink=sink,
        alert_enabled=False,
        now=lambda: CHECKED,
    )
    assert record["status"] == "DELAYED_OR_MISSING"
    assert record["alert_claimed"] is False
    assert not sink.records
    assert not store.claims


def test_fixture_dry_run_never_needs_a_network_client(tmp_path: Path, capsys):
    fixture = tmp_path / "runs.json"
    fixture.write_text(
        json.dumps({"activation_runs": [_activation()]}),
        encoding="utf-8",
    )
    assert main([
        "--schedule", "start", "--phase", "presence",
        "--checked-at-utc", "2026-08-24T00:05:00Z",
        "--fixture", str(fixture),
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ACTIVATION_PRESENT"
    assert output["alert_delivered"] is False


def test_fixture_rejects_unexpected_top_level_keys(tmp_path: Path):
    fixture = tmp_path / "runs.json"
    fixture.write_text(
        json.dumps({"activation_runs": [], "unexpected": []}),
        encoding="utf-8",
    )
    assert main([
        "--schedule", "start", "--phase", "presence",
        "--checked-at-utc", "2026-08-24T00:05:00Z",
        "--fixture", str(fixture),
    ]) == 1
