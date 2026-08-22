"""Contract tests for the scheduled Shadow artifact bundle auditor."""

from __future__ import annotations

import gzip
import hashlib
import importlib
import io
import json
from pathlib import Path
import stat
import sys
import zipfile

import pytest

import deploy.audit_shadow_schedule_bundle as audit
import deploy.ec2.shadow_runtime_evidence as runtime_evidence
from deploy.shadow_schedule_observation import build_observation


sys.modules.setdefault("shadow_runtime_evidence", runtime_evidence)
invocation_diagnostic = importlib.import_module(
    "deploy.ec2.shadow_invocation_diagnostic"
)


SOURCE = "a" * 40
CONTROL = "c" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
COMMAND = "00000000-0000-0000-0000-000000000001"


def _expected(desired_state: str) -> audit.AuditExpectation:
    if desired_state == "continuous":
        return audit.AuditExpectation(
            run_id="123",
            cron="50 23 * * 0-4",
            desired_state=desired_state,
            control_plane_sha=CONTROL,
            source_sha=SOURCE,
            image_digest=IMAGE,
            activation_id="shadow-session-20260821",
        )
    return audit.AuditExpectation(
        run_id="456",
        cron="35 6 * * 1-5",
        desired_state=desired_state,
        control_plane_sha=CONTROL,
        source_sha=SOURCE,
        image_digest=IMAGE,
        activation_id="shadow-session-20260824",
    )


def _run(expected: audit.AuditExpectation) -> dict[str, object]:
    if expected.desired_state == "continuous":
        created_at = "2026-08-20T23:53:00Z"
        started_at = "2026-08-20T23:54:00Z"
    else:
        created_at = "2026-08-24T06:40:00Z"
        started_at = "2026-08-24T06:41:00Z"
    return {
        "id": int(expected.run_id),
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "head_sha": expected.control_plane_sha,
        "head_branch": "main",
        "path": ".github/workflows/cd-shadow-worker-activation.yml",
        "created_at": created_at,
        "run_started_at": started_at,
    }


def _observation(
    run: dict[str, object], expected: audit.AuditExpectation,
) -> dict[str, object]:
    return build_observation(
        {
            "event": run["event"],
            "id": run["id"],
            "created_at": run["created_at"],
            "run_started_at": run["run_started_at"],
            "head_branch": run["head_branch"],
        },
        run_id=expected.run_id,
        cron=expected.cron,
        desired_state=expected.desired_state,
    )


def _evidence(
    expected: audit.AuditExpectation, *, cycles: int,
) -> dict[str, object]:
    return {
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "build_run_id": "789",
        "activation_id": expected.activation_id,
        "desired_state": expected.desired_state,
        "command_id": COMMAND,
        "runtime_status": (
            "PASS" if expected.desired_state == "continuous" else "STOPPED"
        ),
        "cycles": cycles,
        "http_attempts": cycles * 6,
        "db_reopens": cycles - 1,
        "database": True,
        "decision_telemetry": (
            {"paper_action": "HOLD"}
            if expected.desired_state == "continuous"
            else None
        ),
        "side_effects": {
            "orders": False,
            "account": False,
            "revoke": False,
            "database": True,
            "notifications": False,
            "reports": False,
            "s3": False,
        },
        "ssm_status": "Success",
        "ssm_response_code": 0,
    }


def _diagnostic(expected: audit.AuditExpectation) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "activation_id": expected.activation_id,
        "desired_state": expected.desired_state,
        "command_id": COMMAND,
        "ssm_status": "Success",
        "ssm_response_code": 0,
        "stdout_bytes": 100,
        "stderr_bytes": 0,
        "failure_category": "runtime_terminal_nonoperational",
        "terminal": None,
    }


def _terminal_record(
    expected: audit.AuditExpectation, *, status: str, reason: str, cycles: int,
) -> dict[str, object]:
    multi_cycle = cycles > 1
    return {
        "schema_version": 4,
        "event": "terminal",
        "status": status,
        "mode": "shadow-continuous",
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "activation_id": expected.activation_id,
        "cycles": cycles,
        "elapsed_seconds": 120.0 if cycles else 1.0,
        "first_cycle_start_elapsed_seconds": 0.0 if cycles else None,
        "second_cycle_start_elapsed_seconds": 60.0 if multi_cycle else None,
        "second_cycle_interval_seconds": 60.0 if multi_cycle else None,
        "minimum_cycle_interval_seconds": 60.0 if multi_cycle else None,
        "db_reopens": max(cycles - 1, 0),
        "resources_closed": True,
        "side_effects": {
            "broker_orders": False,
            "account": False,
            "oauth_revoke": False,
            "slack": False,
            "gemini": False,
            "s3": False,
            "reports": False,
        },
        "reason": reason,
    }


def _terminal_evidence(
    expected: audit.AuditExpectation, *, status: str, reason: str, cycles: int,
) -> dict[str, object]:
    validated = runtime_evidence.validate(
        [_terminal_record(
            expected, status=status, reason=reason, cycles=cycles,
        )],
        mode="shadow-continuous",
        event="terminal",
        source_sha=expected.source_sha,
        image_digest=expected.image_digest,
        activation_id=expected.activation_id,
    )
    value = {
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "build_run_id": "789",
        "activation_id": expected.activation_id,
        "desired_state": expected.desired_state,
        "command_id": COMMAND,
        "ssm_status": "Success",
        "ssm_response_code": 0,
    }
    value.update(runtime_evidence.activation_summary(validated))
    return value


def _terminal_diagnostic(
    expected: audit.AuditExpectation, *, status: str, reason: str, cycles: int,
) -> dict[str, object]:
    value = _diagnostic(expected)
    value["terminal"] = {
        "status": status,
        "reason": reason,
        "cycles": cycles,
        "db_reopens": max(cycles - 1, 0),
        "resources_closed": True,
        "elapsed_seconds": 1.0,
        "error_type": None,
    }
    return value


def _successful_stop_invocation(
    expected: audit.AuditExpectation,
) -> dict[str, object]:
    terminal = _terminal_record(
        expected, status="STOPPED", reason="stop-requested", cycles=2,
    )
    return {
        "Status": "Success",
        "ResponseCode": 0,
        "StandardOutputContent": _encode(terminal).decode("utf-8"),
        "StandardErrorContent": "",
    }


def _successful_stop_diagnostic(
    expected: audit.AuditExpectation,
) -> dict[str, object]:
    return invocation_diagnostic.build_diagnostic(
        _successful_stop_invocation(expected),
        source_sha=expected.source_sha,
        image_digest=expected.image_digest,
        activation_id=expected.activation_id,
        desired_state="stop",
        command_id=COMMAND,
    )


def _receipt(expected: audit.AuditExpectation) -> dict[str, object]:
    return {
        "schema_version": 2,
        "event": "shadow-status-notification",
        "source_sha": expected.source_sha,
        "activation_id": expected.activation_id,
        "desired_state": expected.desired_state,
        "delivery_status": "DELIVERED",
        "category": "runtime_accepted",
        "schedule_observation": "accepted",
    }


def _telemetry_row(
    expected: audit.AuditExpectation, *, cycle: int,
) -> dict[str, object]:
    row: dict[str, object] = {
        "schema_version": 1,
        "activation_id": expected.activation_id,
        "session_date_kst": "2026-08-24",
        "cycle_index": cycle,
        "observed_at": f"2026-08-24T00:0{cycle}:00+00:00",
        "stock_code": "005930",
        "proxy_code": "069500",
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "config_sha256": "d" * 64,
        "strategy_slot": "baseline",
        "candidate_id": None,
        "current_price": 70000.0 + cycle,
        "vwap": 69900.0,
        "strength": 101.0,
        "trend_rsi": 50.0,
        "atr_percent": 1.0,
        "down_atr_percent": 0.5,
        "volume_ratio": 1.2,
        "forces": {
            "thrust": 1.1,
            "gravity": 0.5,
            "drag": 0.2,
            "magnetic": 0.1,
            "jerk": 0.0,
            "impulse": 0.3,
            "net_force": 1.0,
            "current_velocity": 0.8,
            "volume_drop_ratio": 0.0,
        },
        "decision": {
            "market_regime": "NEUTRAL",
            "strategy_reason_code": "JERK_NON_POSITIVE",
            "strategy_intent": "NO_ENTRY_SIGNAL",
            "paper_action": "HOLD",
            "position_before": "FLAT",
            "trading_window": "OPEN",
            "session_phase": "ENTRY",
            "net_force_band": "POSITIVE",
            "current_velocity_band": "POSITIVE",
            "thrust_band": "FROM_1_0_TO_1_5",
            "jerk_band": "NEUTRAL",
            "strength_band": "ABOVE_100",
            "trend_rsi_band": "NEUTRAL",
            "price_vwap_relation": "ABOVE",
        },
        "position_after": "FLAT",
        "paper_position_id": None,
        "continuity": {
            "schema_version": 1,
            "hydration_source": "initial",
            "previous_observed_at": None,
            "history_depth": 0,
            "baseline_source": "row_4_fixed_cadence",
            "baseline_sample_index": 4,
            "baseline_time_estimated": True,
        },
        "row_sha256": "",
        "committed_at": f"2026-08-24T00:0{cycle}:01+00:00",
    }
    payload = {key: row[key] for key in audit.ROW_HASH_KEYS}
    row["row_sha256"] = hashlib.sha256(
        audit._canonical_json(payload)
    ).hexdigest()
    return row


def _mutate_decision_semantics(
    decision: dict[str, object], mutation: str,
) -> None:
    if mutation == "reason_intent_semantic":
        decision["strategy_reason_code"] = "BREAKOUT_OVERRIDE"
    elif mutation == "entry_jerk_semantic":
        decision["strategy_reason_code"] = "BREAKOUT_OVERRIDE"
        decision["strategy_intent"] = "ENTRY_SIGNAL"
    elif mutation == "window_phase_semantic":
        decision["session_phase"] = "EXIT_ONLY"
    elif mutation == "sell_position_semantic":
        decision["paper_action"] = "SELL"
    elif mutation == "net_force_semantic":
        decision["strategy_reason_code"] = "NET_FORCE_NEGATIVE"
    elif mutation == "thrust_semantic":
        decision["strategy_reason_code"] = "THRUST_LOW"
    else:
        raise ValueError("unknown semantic mutation")


def _telemetry(
    expected: audit.AuditExpectation, *, cycles: int,
) -> tuple[dict[str, object], bytes]:
    rows = [_telemetry_row(expected, cycle=index) for index in range(1, cycles + 1)]
    jsonl = b"".join(
        audit._canonical_json(row) + b"\n" for row in rows
    )
    compressed = gzip.compress(jsonl, mtime=0)
    manifest = {
        "event": "telemetry_manifest",
        "schema_version": 1,
        "activation_id": expected.activation_id,
        "session_date_kst": "2026-08-24",
        "row_count": cycles,
        "first_cycle": 1,
        "last_cycle": cycles,
        "source_sha": expected.source_sha,
        "image_digest": expected.image_digest,
        "config_sha256": "d" * 64,
        "database_bytes": 16384,
        "database_page_size": 4096,
        "database_page_count": 4,
        "finalized_session_count": 1,
        "session_sha256": hashlib.sha256(jsonl).hexdigest(),
        "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
        "compressed_bytes": len(compressed),
    }
    return manifest, compressed


def _rewrite_first_telemetry_row(
    members: dict[str, bytes], mutation,
) -> None:
    rows = [
        json.loads(line)
        for line in gzip.decompress(
            members["shadow-telemetry.jsonl.gz"]
        ).splitlines()
    ]
    mutation(rows[0])
    payload = {key: rows[0][key] for key in audit.ROW_HASH_KEYS}
    rows[0]["row_sha256"] = hashlib.sha256(
        audit._canonical_json(payload)
    ).hexdigest()
    jsonl = b"".join(
        audit._canonical_json(row) + b"\n" for row in rows
    )
    compressed = gzip.compress(jsonl, mtime=0)
    members["shadow-telemetry.jsonl.gz"] = compressed
    manifest = json.loads(
        members["shadow-telemetry.manifest.json"].decode("utf-8")
    )
    manifest["compressed_bytes"] = len(compressed)
    manifest["compressed_sha256"] = hashlib.sha256(compressed).hexdigest()
    manifest["session_sha256"] = hashlib.sha256(jsonl).hexdigest()
    members["shadow-telemetry.manifest.json"] = _encode(manifest)


def _encode(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _archive(
    members: dict[str, bytes],
    *, extra: tuple[str, bytes] | None = None,
    symlink: str | None = None,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, value in members.items():
            if name == symlink:
                info = zipfile.ZipInfo(name)
                info.external_attr = stat.S_IFLNK << 16
                bundle.writestr(info, value)
            else:
                bundle.writestr(name, value)
        if extra is not None:
            bundle.writestr(*extra)
    return output.getvalue()


def _fixture(
    desired_state: str, *, holiday_closed: bool = False,
) -> tuple[
    audit.AuditExpectation,
    dict[str, object],
    dict[str, object],
    bytes,
    dict[str, bytes],
]:
    expected = _expected(desired_state)
    run = _run(expected)
    return _fixture_from_expected(
        expected, run, holiday_closed=holiday_closed,
    )


def _fixture_from_expected(
    expected: audit.AuditExpectation,
    run: dict[str, object],
    *,
    holiday_closed: bool = False,
) -> tuple[
    audit.AuditExpectation,
    dict[str, object],
    dict[str, object],
    bytes,
    dict[str, bytes],
]:
    if holiday_closed and expected.desired_state != "continuous":
        raise ValueError("holiday closure is continuous-only")
    cycles = 1 if expected.desired_state == "continuous" else 2
    if holiday_closed:
        evidence = _terminal_evidence(
            expected,
            status="CLOSED",
            reason="calendar-closed",
            cycles=0,
        )
        diagnostic = _terminal_diagnostic(
            expected,
            status="CLOSED",
            reason="calendar-closed",
            cycles=0,
        )
    elif expected.desired_state == "stop":
        evidence = _terminal_evidence(
            expected,
            status="STOPPED",
            reason="stop-requested",
            cycles=cycles,
        )
        diagnostic = _successful_stop_diagnostic(expected)
    else:
        evidence = _evidence(expected, cycles=cycles)
        diagnostic = _diagnostic(expected)
    members = {
        "shadow-worker-evidence.json": _encode(evidence),
        "shadow-worker-diagnostic.json": _encode(diagnostic),
        "shadow-status-notification.json": _encode(_receipt(expected)),
        f"shadow-schedule-observation-{expected.run_id}.json": _encode(
            _observation(run, expected)
        ),
    }
    if expected.desired_state == "stop":
        manifest, compressed = _telemetry(expected, cycles=cycles)
        members["shadow-telemetry.manifest.json"] = _encode(manifest)
        members["shadow-telemetry.jsonl.gz"] = compressed
    archive = _archive(members)
    artifact = {
        "id": 999,
        "name": f"shadow-worker-{expected.source_sha}-{expected.activation_id}",
        "size_in_bytes": len(archive),
        "expired": False,
        "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        "workflow_run": {
            "id": int(expected.run_id),
            "head_sha": expected.control_plane_sha,
        },
    }
    return expected, run, artifact, archive, members


@pytest.mark.parametrize(
    ("desired_state", "runtime_status", "cycles", "telemetry_rows"),
    [
        ("continuous", "PASS", 1, None),
        ("stop", "STOPPED", 2, 2),
    ],
)
def test_valid_scheduled_bundle_is_bound_and_summarized(
    desired_state, runtime_status, cycles, telemetry_rows,
):
    expected, run, artifact, archive, _members = _fixture(desired_state)
    result = audit.validate_bundle(
        run=run,
        artifact=artifact,
        archive=archive,
        expected=expected,
    )
    assert result["status"] == "PASS"
    assert result["run_id"] == expected.run_id
    assert result["control_plane_sha"] == CONTROL
    assert result["source_sha"] == SOURCE
    assert result["runtime_status"] == runtime_status
    assert result["cycles"] == cycles
    assert result["http_attempts"] == cycles * 6
    assert result["notification_delivery_status"] == "DELIVERED"
    assert result["telemetry_rows"] == telemetry_rows


@pytest.mark.parametrize(
    ("desired_state", "status", "reason", "cycles", "holiday_closed"),
    [
        ("continuous", "CLOSED", "calendar-closed", 0, True),
        ("stop", "STOPPED", "stop-requested", 2, False),
    ],
)
def test_terminal_fixtures_match_actual_activation_summary(
    desired_state, status, reason, cycles, holiday_closed,
):
    expected, _run_value, _artifact, _archive_bytes, members = _fixture(
        desired_state, holiday_closed=holiday_closed,
    )
    validated = runtime_evidence.validate(
        [_terminal_record(
            expected, status=status, reason=reason, cycles=cycles,
        )],
        mode="shadow-continuous",
        event="terminal",
        source_sha=expected.source_sha,
        image_digest=expected.image_digest,
        activation_id=expected.activation_id,
    )
    summary = runtime_evidence.activation_summary(validated)
    evidence = json.loads(
        members["shadow-worker-evidence.json"].decode("utf-8")
    )
    assert {key: evidence[key] for key in summary} == summary
    assert summary["runtime_status"] == status
    assert summary["cycles"] == cycles
    assert summary["http_attempts"] is None
    assert summary["database"] is False
    assert summary["decision_telemetry"] is None
    assert summary["side_effects"]["database"] is False
    if desired_state == "stop":
        diagnostic = json.loads(
            members["shadow-worker-diagnostic.json"].decode("utf-8")
        )
        actual_diagnostic = invocation_diagnostic.build_diagnostic(
            _successful_stop_invocation(expected),
            source_sha=expected.source_sha,
            image_digest=expected.image_digest,
            activation_id=expected.activation_id,
            desired_state="stop",
            command_id=COMMAND,
        )
        assert diagnostic == actual_diagnostic
        assert diagnostic["ssm_status"] == "Success"
        assert diagnostic["ssm_response_code"] == 0
        assert diagnostic["failure_category"] == (
            "success_without_accepted_runtime_evidence"
        )
        assert diagnostic["terminal"] is None


def test_calendar_closed_continuous_is_valid_in_explicit_and_auto_modes():
    expected, run, artifact, archive, _members = _fixture(
        "continuous", holiday_closed=True,
    )
    explicit = audit.validate_bundle(
        run=run,
        artifact=artifact,
        archive=archive,
        expected=expected,
    )
    automatic = audit.validate_auto_schedule_bundle(
        run=run,
        artifact=artifact,
        archive=archive,
        source_sha=SOURCE,
        image_digest=IMAGE,
    )
    for result in (explicit, automatic):
        assert result["status"] == "PASS"
        assert result["runtime_status"] == "CLOSED"
        assert result["cycles"] == 0
        assert result["http_attempts"] == 0
        assert result["desired_state"] == "continuous"


@pytest.mark.parametrize(
    "mutation",
    [
        "status",
        "cycles_bool",
        "attempts",
        "db_reopens_bool",
        "database",
        "decision",
        "side_effect_database",
        "terminal_reason",
        "terminal_cycles_bool",
        "terminal_missing",
    ],
)
def test_calendar_closed_continuous_mutations_fail_explicit_and_auto(mutation):
    expected, run, artifact, _archive_bytes, members = _fixture(
        "continuous", holiday_closed=True,
    )
    evidence = json.loads(
        members["shadow-worker-evidence.json"].decode("utf-8")
    )
    diagnostic = json.loads(
        members["shadow-worker-diagnostic.json"].decode("utf-8")
    )
    if mutation == "status":
        evidence["runtime_status"] = "PASS"
    elif mutation == "cycles_bool":
        evidence["cycles"] = False
    elif mutation == "attempts":
        evidence["http_attempts"] = 1
    elif mutation == "db_reopens_bool":
        evidence["db_reopens"] = False
    elif mutation == "database":
        evidence["database"] = True
    elif mutation == "decision":
        evidence["decision_telemetry"] = {"paper_action": "HOLD"}
    elif mutation == "side_effect_database":
        evidence["side_effects"]["database"] = True
    elif mutation == "terminal_reason":
        diagnostic["terminal"]["reason"] = "run-deadline"
    elif mutation == "terminal_cycles_bool":
        diagnostic["terminal"]["cycles"] = False
    else:
        diagnostic["terminal"] = None
    members["shadow-worker-evidence.json"] = _encode(evidence)
    members["shadow-worker-diagnostic.json"] = _encode(diagnostic)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(audit.ScheduleAuditError):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )
    with pytest.raises(
        audit.ScheduleAuditError, match="^auto_schedule_unresolved$"
    ):
        audit.validate_auto_schedule_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            source_sha=SOURCE,
            image_digest=IMAGE,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "status",
        "cycles_bool",
        "attempts",
        "db_reopens_bool",
        "database",
        "decision",
        "side_effect_database",
        "command",
        "ssm_status",
        "diagnostic_category",
        "diagnostic_terminal",
        "diagnostic_stdout_bool",
        "diagnostic_stdout_zero",
        "diagnostic_stdout_oversized",
        "diagnostic_stderr_bool",
    ],
)
def test_stop_terminal_summary_mutations_fail_closed(mutation):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")
    evidence = json.loads(
        members["shadow-worker-evidence.json"].decode("utf-8")
    )
    diagnostic = json.loads(
        members["shadow-worker-diagnostic.json"].decode("utf-8")
    )
    if mutation == "status":
        evidence["runtime_status"] = "PASS"
    elif mutation == "cycles_bool":
        evidence["cycles"] = True
    elif mutation == "attempts":
        evidence["http_attempts"] = 12
    elif mutation == "db_reopens_bool":
        evidence["db_reopens"] = True
    elif mutation == "database":
        evidence["database"] = True
    elif mutation == "decision":
        evidence["decision_telemetry"] = {"paper_action": "HOLD"}
    elif mutation == "side_effect_database":
        evidence["side_effects"]["database"] = True
    elif mutation == "command":
        evidence["command_id"] = "00000000-0000-0000-0000-000000000002"
    elif mutation == "ssm_status":
        evidence["ssm_status"] = "Failed"
    elif mutation == "diagnostic_category":
        diagnostic["failure_category"] = "runtime_terminal_nonoperational"
    elif mutation == "diagnostic_terminal":
        diagnostic["terminal"] = {
            "status": "STOPPED",
            "reason": "stop-requested",
        }
    elif mutation == "diagnostic_stdout_bool":
        diagnostic["stdout_bytes"] = True
    elif mutation == "diagnostic_stdout_zero":
        diagnostic["stdout_bytes"] = 0
    elif mutation == "diagnostic_stdout_oversized":
        diagnostic["stdout_bytes"] = audit.MAX_RUNTIME_INPUT_BYTES + 1
    else:
        diagnostic["stderr_bytes"] = False
    members["shadow-worker-evidence.json"] = _encode(evidence)
    members["shadow-worker-diagnostic.json"] = _encode(diagnostic)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(audit.ScheduleAuditError):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_cycle_start_elapsed_seconds", 0),
        ("second_cycle_start_elapsed_seconds", 60),
        ("second_cycle_interval_seconds", 60),
        ("minimum_cycle_interval_seconds", 60),
        ("first_cycle_start_elapsed_seconds", 10 ** 400),
        ("second_cycle_start_elapsed_seconds", 10 ** 400),
    ],
)
def test_stop_terminal_timing_requires_exact_float_and_fails_value_free(
    field, value,
):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")
    evidence = json.loads(
        members["shadow-worker-evidence.json"].decode("utf-8")
    )
    evidence[field] = value
    members["shadow-worker-evidence.json"] = _encode(evidence)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(
        audit.ScheduleAuditError, match="^runtime_not_operational$",
    ) as rejected:
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )
    assert str(value) not in str(rejected.value)


@pytest.mark.parametrize("value", [1, 10 ** 400])
def test_holiday_diagnostic_elapsed_requires_exact_float_and_is_value_free(
    value,
):
    expected, run, artifact, _archive_bytes, members = _fixture(
        "continuous", holiday_closed=True,
    )
    diagnostic = json.loads(
        members["shadow-worker-diagnostic.json"].decode("utf-8")
    )
    diagnostic["terminal"]["elapsed_seconds"] = value
    members["shadow-worker-diagnostic.json"] = _encode(diagnostic)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(
        audit.ScheduleAuditError, match="^runtime_not_operational$",
    ) as rejected:
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )
    assert str(value) not in str(rejected.value)


@pytest.mark.parametrize(
    ("value", "minimum"),
    [
        (0.0, 0.0),
        (60.0, 60.0),
        (0, 0.0),
        (60, 60.0),
        (10 ** 400, 0.0),
        (float("inf"), 0.0),
    ],
)
def test_terminal_float_acceptance_matches_actual_runtime_validator(
    value, minimum,
):
    assert audit._is_finite_float(value, minimum=minimum) == (
        runtime_evidence._finite_float(value, minimum=minimum)
    )


@pytest.mark.parametrize("desired_state", ["continuous", "stop"])
def test_auto_schedule_fully_validates_and_selects_one_candidate(
    desired_state,
):
    expected, run, artifact, archive, _members = _fixture(desired_state)
    result = audit.validate_auto_schedule_bundle(
        run=run,
        artifact=artifact,
        archive=archive,
        source_sha=SOURCE,
        image_digest=IMAGE,
    )
    assert result["status"] == "PASS"
    assert result["run_id"] == expected.run_id
    assert result["desired_state"] == desired_state
    assert result["activation_id"] == expected.activation_id
    if desired_state == "stop":
        assert result["http_attempts"] == 12


def test_auto_schedule_does_not_misclassify_late_start_after_stop_cron():
    expected = audit.AuditExpectation(
        run_id="789",
        cron="50 23 * * 0-4",
        desired_state="continuous",
        control_plane_sha=CONTROL,
        source_sha=SOURCE,
        image_digest=IMAGE,
        activation_id="shadow-session-20260824",
    )
    run = {
        "id": 789,
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "head_sha": CONTROL,
        "head_branch": "main",
        "path": ".github/workflows/cd-shadow-worker-activation.yml",
        "created_at": "2026-08-24T06:40:00Z",
        "run_started_at": "2026-08-24T06:41:00Z",
    }
    _, run, artifact, archive, _members = _fixture_from_expected(expected, run)
    result = audit.validate_auto_schedule_bundle(
        run=run,
        artifact=artifact,
        archive=archive,
        source_sha=SOURCE,
        image_digest=IMAGE,
    )
    assert result["desired_state"] == "continuous"
    assert result["activation_id"] == "shadow-session-20260824"


@pytest.mark.parametrize("trust_anchor", ["source", "image"])
def test_auto_schedule_current_tuple_drift_is_unresolved(trust_anchor):
    _expected_value, run, artifact, archive, _members = _fixture("continuous")
    source_sha = "e" * 40 if trust_anchor == "source" else SOURCE
    image_digest = (
        "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "e" * 64
        if trust_anchor == "image"
        else IMAGE
    )
    with pytest.raises(
        audit.ScheduleAuditError, match="^auto_schedule_unresolved$"
    ):
        audit.validate_auto_schedule_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            source_sha=source_sha,
            image_digest=image_digest,
        )


def test_auto_schedule_rejects_multiple_full_validation_passes(monkeypatch):
    _expected_value, run, artifact, archive, _members = _fixture("stop")

    def pass_every_candidate(**kwargs):
        return {
            "status": "PASS",
            "desired_state": kwargs["expected"].desired_state,
        }

    monkeypatch.setattr(audit, "validate_bundle", pass_every_candidate)
    with pytest.raises(
        audit.ScheduleAuditError, match="^auto_schedule_ambiguous$"
    ):
        audit.validate_auto_schedule_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            source_sha=SOURCE,
            image_digest=IMAGE,
        )


@pytest.mark.parametrize(
    ("desired_state", "mutation"),
    [
        ("continuous", "observation"),
        ("continuous", "receipt"),
        ("continuous", "evidence"),
        ("stop", "telemetry"),
    ],
)
def test_auto_schedule_full_bundle_mutations_are_unresolved(
    desired_state, mutation,
):
    expected, run, artifact, _archive_bytes, members = _fixture(desired_state)
    if mutation == "observation":
        value = _observation(run, expected)
        value["run_id"] = "124"
        members[f"shadow-schedule-observation-{expected.run_id}.json"] = (
            _encode(value)
        )
    elif mutation == "receipt":
        value = _receipt(expected)
        value["delivery_status"] = "FAILED"
        members["shadow-status-notification.json"] = _encode(value)
    elif mutation == "evidence":
        value = _evidence(expected, cycles=1)
        value["source_sha"] = "e" * 40
        members["shadow-worker-evidence.json"] = _encode(value)
    else:
        value = json.loads(
            members["shadow-telemetry.manifest.json"].decode("utf-8")
        )
        value["session_sha256"] = "e" * 64
        members["shadow-telemetry.manifest.json"] = _encode(value)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(
        audit.ScheduleAuditError, match="^auto_schedule_unresolved$"
    ):
        audit.validate_auto_schedule_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            source_sha=SOURCE,
            image_digest=IMAGE,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "run_head",
        "run_conclusion",
        "artifact_run",
        "artifact_head",
        "artifact_digest",
        "artifact_size",
        "oversized_archive",
        "receipt_delivery",
        "receipt_schedule",
        "observation_run",
        "runtime_status",
        "runtime_attempts",
        "runtime_database",
        "unsafe_side_effect",
        "diagnostic_invalid",
        "nonfinite_evidence",
        "extra_member",
        "symlink_member",
    ],
)
def test_bundle_provenance_and_operational_mutations_fail_closed(mutation):
    expected, run, artifact, archive, members = _fixture("continuous")
    if mutation == "run_head":
        run["head_sha"] = "e" * 40
    elif mutation == "run_conclusion":
        run["conclusion"] = "failure"
    elif mutation == "artifact_run":
        artifact["workflow_run"]["id"] = 124
    elif mutation == "artifact_head":
        artifact["workflow_run"]["head_sha"] = "e" * 40
    elif mutation == "artifact_digest":
        artifact["digest"] = "sha256:" + "e" * 64
    elif mutation == "artifact_size":
        artifact["size_in_bytes"] = len(archive) + 1
    elif mutation == "oversized_archive":
        archive = b"x" * (audit.MAX_ARCHIVE_BYTES + 1)
        artifact["size_in_bytes"] = len(archive)
        artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    else:
        if mutation == "receipt_delivery":
            value = _receipt(expected)
            value["delivery_status"] = "FAILED"
            members["shadow-status-notification.json"] = _encode(value)
        elif mutation == "receipt_schedule":
            value = _receipt(expected)
            value["schedule_observation"] = "invalid"
            members["shadow-status-notification.json"] = _encode(value)
        elif mutation == "observation_run":
            value = _observation(run, expected)
            value["run_id"] = "124"
            members[
                f"shadow-schedule-observation-{expected.run_id}.json"
            ] = _encode(value)
        elif mutation in {
            "runtime_status", "runtime_attempts", "runtime_database",
            "unsafe_side_effect",
        }:
            value = _evidence(expected, cycles=1)
            if mutation == "runtime_status":
                value["runtime_status"] = "CLOSED"
            elif mutation == "runtime_attempts":
                value["http_attempts"] = 0
            elif mutation == "runtime_database":
                value["database"] = False
            else:
                value["side_effects"]["orders"] = True
            members["shadow-worker-evidence.json"] = _encode(value)
        elif mutation == "diagnostic_invalid":
            value = _diagnostic(expected)
            value["command_id"] = "invalid"
            members["shadow-worker-diagnostic.json"] = _encode(value)
        elif mutation == "nonfinite_evidence":
            members["shadow-worker-evidence.json"] = members[
                "shadow-worker-evidence.json"
            ].replace(b'"build_run_id":"789"', b'"unsafe":1e999')
        if mutation == "extra_member":
            archive = _archive(members, extra=("unexpected.txt", b"x"))
        elif mutation == "symlink_member":
            archive = _archive(
                members, symlink="shadow-worker-evidence.json"
            )
        else:
            archive = _archive(members)
        artifact["size_in_bytes"] = len(archive)
        artifact["digest"] = (
            "sha256:" + hashlib.sha256(archive).hexdigest()
        )
    with pytest.raises(audit.ScheduleAuditError):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_telemetry",
        "missing_manifest",
        "manifest_hash",
        "manifest_identity",
        "row_hash",
        "row_count",
    ],
)
def test_stop_telemetry_mutations_fail_closed(mutation):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")
    evidence = json.loads(
        members["shadow-worker-evidence.json"].decode("utf-8")
    )
    diagnostic = json.loads(
        members["shadow-worker-diagnostic.json"].decode("utf-8")
    )
    assert evidence["http_attempts"] is None
    assert audit._validate_runtime(
        evidence, diagnostic, expected,
    )[2] is None
    if mutation == "missing_telemetry":
        members.pop("shadow-telemetry.jsonl.gz")
    elif mutation == "missing_manifest":
        members.pop("shadow-telemetry.manifest.json")
    elif mutation in {"manifest_hash", "manifest_identity", "row_count"}:
        manifest = json.loads(
            members["shadow-telemetry.manifest.json"].decode("utf-8")
        )
        if mutation == "manifest_hash":
            manifest["compressed_sha256"] = "e" * 64
        elif mutation == "manifest_identity":
            manifest["activation_id"] = "wrong"
        else:
            manifest["row_count"] = 1
        members["shadow-telemetry.manifest.json"] = _encode(manifest)
    else:
        compressed = members["shadow-telemetry.jsonl.gz"]
        rows = [
            json.loads(line)
            for line in gzip.decompress(compressed).splitlines()
        ]
        rows[0]["row_sha256"] = "e" * 64
        jsonl = b"".join(audit._canonical_json(row) + b"\n" for row in rows)
        members["shadow-telemetry.jsonl.gz"] = gzip.compress(jsonl, mtime=0)
        manifest = json.loads(
            members["shadow-telemetry.manifest.json"].decode("utf-8")
        )
        manifest["compressed_bytes"] = len(
            members["shadow-telemetry.jsonl.gz"]
        )
        manifest["compressed_sha256"] = hashlib.sha256(
            members["shadow-telemetry.jsonl.gz"]
        ).hexdigest()
        manifest["session_sha256"] = hashlib.sha256(jsonl).hexdigest()
        members["shadow-telemetry.manifest.json"] = _encode(manifest)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(audit.ScheduleAuditError):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("first_cycle", True),
        ("last_cycle", 2.0),
        ("row_count", 2.0),
        ("compressed_bytes", 1.0),
    ],
)
def test_stop_manifest_wrong_scalar_types_fail_before_hash(field, value):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")
    manifest = json.loads(
        members["shadow-telemetry.manifest.json"].decode("utf-8")
    )
    if field == "compressed_bytes":
        value = float(manifest["compressed_bytes"])
    manifest[field] = value
    members["shadow-telemetry.manifest.json"] = _encode(manifest)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(
        audit.ScheduleAuditError, match="^telemetry_manifest_invalid$"
    ):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "cycle_bool",
        "numeric_bool",
        "numeric_huge_int",
        "timestamp_scalar",
        "stock_code_scalar",
        "nullable_scalar",
        "forces_missing",
        "forces_bool",
        "decision_enum",
        "reason_intent_semantic",
        "entry_jerk_semantic",
        "window_phase_semantic",
        "sell_position_semantic",
        "net_force_semantic",
        "thrust_semantic",
        "continuity_history_bool",
        "continuity_source",
        "position_enum",
    ],
)
def test_self_rehashed_wrong_scalar_and_nested_rows_fail_schema(mutation):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")

    def mutate(row):
        if mutation == "cycle_bool":
            row["cycle_index"] = True
        elif mutation == "numeric_bool":
            row["current_price"] = True
        elif mutation == "numeric_huge_int":
            row["current_price"] = 10 ** 400
        elif mutation == "timestamp_scalar":
            row["observed_at"] = 123
        elif mutation == "stock_code_scalar":
            row["stock_code"] = 5930
        elif mutation == "nullable_scalar":
            row["candidate_id"] = 123
        elif mutation == "forces_missing":
            row["forces"].pop("jerk")
        elif mutation == "forces_bool":
            row["forces"]["jerk"] = False
        elif mutation == "decision_enum":
            row["decision"]["paper_action"] = "EXECUTE"
        elif mutation.endswith("_semantic"):
            _mutate_decision_semantics(row["decision"], mutation)
        elif mutation == "continuity_history_bool":
            row["continuity"]["history_depth"] = False
        elif mutation == "continuity_source":
            row["continuity"]["hydration_source"] = "unknown"
        else:
            row["position_after"] = "UNKNOWN"

    _rewrite_first_telemetry_row(members, mutate)
    archive = _archive(members)
    artifact["size_in_bytes"] = len(archive)
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()
    with pytest.raises(
        audit.ScheduleAuditError, match="^telemetry_row_invalid$"
    ):
        audit.validate_bundle(
            run=run,
            artifact=artifact,
            archive=archive,
            expected=expected,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "reason_intent_semantic",
        "entry_jerk_semantic",
        "window_phase_semantic",
        "sell_position_semantic",
        "net_force_semantic",
        "thrust_semantic",
    ],
)
def test_decision_semantics_match_actual_runtime_validator(mutation):
    decision = _telemetry_row(_expected("stop"), cycle=1)["decision"]
    assert isinstance(decision, dict)
    assert audit._valid_decision(decision)
    assert runtime_evidence._valid_decision_telemetry(decision)
    _mutate_decision_semantics(decision, mutation)
    assert all(
        value in audit.DECISION_ALLOWED[key]
        for key, value in decision.items()
    )
    assert not runtime_evidence._valid_decision_telemetry(decision)
    assert not audit._valid_decision(decision)


def test_cli_emits_only_bounded_pass_summary(tmp_path, capsys):
    expected, run, artifact, archive, _members = _fixture("continuous")
    run_path = tmp_path / "run.json"
    artifact_path = tmp_path / "artifact.json"
    archive_path = tmp_path / "artifact.zip"
    run_path.write_bytes(_encode(run))
    artifact_path.write_bytes(_encode(artifact))
    archive_path.write_bytes(archive)
    result = audit.main([
        "--run-json", str(run_path),
        "--artifact-json", str(artifact_path),
        "--artifact-zip", str(archive_path),
        "--run-id", expected.run_id,
        "--cron", expected.cron,
        "--desired-state", expected.desired_state,
        "--control-plane-sha", expected.control_plane_sha,
        "--source-sha", expected.source_sha,
        "--image-digest", expected.image_digest,
        "--activation-id", expected.activation_id,
    ])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"
    assert set(output) == {
        "schema_version", "status", "run_id", "artifact_id",
        "control_plane_sha", "source_sha", "image_digest",
        "activation_id", "desired_state", "runtime_status", "cycles",
        "http_attempts", "command_id", "schedule_delay_seconds",
        "notification_delivery_status", "session_date_kst",
        "telemetry_rows",
    }


@pytest.mark.parametrize("auto_schedule", [False, True])
def test_calendar_closed_cli_passes_explicit_and_auto_modes(
    tmp_path, capsys, auto_schedule,
):
    expected, run, artifact, archive, _members = _fixture(
        "continuous", holiday_closed=True,
    )
    run_path = tmp_path / "run.json"
    artifact_path = tmp_path / "artifact.json"
    archive_path = tmp_path / "artifact.zip"
    run_path.write_bytes(_encode(run))
    artifact_path.write_bytes(_encode(artifact))
    archive_path.write_bytes(archive)
    args = [
        "--run-json", str(run_path),
        "--artifact-json", str(artifact_path),
        "--artifact-zip", str(archive_path),
        "--source-sha", SOURCE,
        "--image-digest", IMAGE,
    ]
    if auto_schedule:
        args.insert(0, "--auto-schedule")
    else:
        args.extend([
            "--run-id", expected.run_id,
            "--cron", expected.cron,
            "--desired-state", expected.desired_state,
            "--control-plane-sha", expected.control_plane_sha,
            "--activation-id", expected.activation_id,
        ])
    assert audit.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"
    assert output["runtime_status"] == "CLOSED"
    assert output["cycles"] == 0
    assert output["http_attempts"] == 0


def test_auto_cli_uses_only_external_tuple_and_emits_same_bounded_summary(
    tmp_path, capsys,
):
    expected, run, artifact, archive, _members = _fixture("stop")
    run_path = tmp_path / "run.json"
    artifact_path = tmp_path / "artifact.json"
    archive_path = tmp_path / "artifact.zip"
    run_path.write_bytes(_encode(run))
    artifact_path.write_bytes(_encode(artifact))
    archive_path.write_bytes(archive)
    result = audit.main([
        "--auto-schedule",
        "--run-json", str(run_path),
        "--artifact-json", str(artifact_path),
        "--artifact-zip", str(archive_path),
        "--source-sha", SOURCE,
        "--image-digest", IMAGE,
    ])
    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"
    assert output["run_id"] == expected.run_id
    assert output["desired_state"] == "stop"
    assert output["http_attempts"] == 12


def test_auto_cli_rejects_explicit_action_hint(tmp_path, capsys):
    _expected_value, run, artifact, archive, _members = _fixture("continuous")
    run_path = tmp_path / "run.json"
    artifact_path = tmp_path / "artifact.json"
    archive_path = tmp_path / "artifact.zip"
    run_path.write_bytes(_encode(run))
    artifact_path.write_bytes(_encode(artifact))
    archive_path.write_bytes(archive)
    result = audit.main([
        "--auto-schedule",
        "--run-json", str(run_path),
        "--artifact-json", str(artifact_path),
        "--artifact-zip", str(archive_path),
        "--source-sha", SOURCE,
        "--image-digest", IMAGE,
        "--desired-state", "continuous",
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err == "shadow schedule audit failed: audit_mode_invalid\n"


def test_cli_failure_is_value_free(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"secret":"MUST_NOT_REFLECT"}', encoding="utf-8")
    missing = tmp_path / "missing"
    result = audit.main([
        "--run-json", str(bad),
        "--artifact-json", str(bad),
        "--artifact-zip", str(missing),
        "--run-id", "123",
        "--cron", "50 23 * * 0-4",
        "--desired-state", "continuous",
        "--control-plane-sha", CONTROL,
        "--source-sha", SOURCE,
        "--image-digest", IMAGE,
        "--activation-id", "shadow-session-20260821",
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith("shadow schedule audit failed: ")
    assert "MUST_NOT_REFLECT" not in captured.err
