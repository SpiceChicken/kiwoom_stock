"""Contract tests for the scheduled Shadow artifact bundle auditor."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

import pytest

import deploy.audit_shadow_schedule_bundle as audit
from deploy.shadow_schedule_observation import build_observation


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
        "forces": {"net_force": 1.0},
        "decision": {"paper_action": "HOLD"},
        "position_after": "FLAT",
        "paper_position_id": None,
        "continuity": {"schema_version": 1},
        "row_sha256": "",
        "committed_at": f"2026-08-24T00:0{cycle}:01+00:00",
    }
    payload = {key: row[key] for key in audit.ROW_HASH_KEYS}
    row["row_sha256"] = hashlib.sha256(
        audit._canonical_json(payload)
    ).hexdigest()
    return row


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
    desired_state: str,
) -> tuple[
    audit.AuditExpectation,
    dict[str, object],
    dict[str, object],
    bytes,
    dict[str, bytes],
]:
    expected = _expected(desired_state)
    run = _run(expected)
    cycles = 1 if desired_state == "continuous" else 2
    members = {
        "shadow-worker-evidence.json": _encode(
            _evidence(expected, cycles=cycles)
        ),
        "shadow-worker-diagnostic.json": _encode(_diagnostic(expected)),
        "shadow-status-notification.json": _encode(_receipt(expected)),
        f"shadow-schedule-observation-{expected.run_id}.json": _encode(
            _observation(run, expected)
        ),
    }
    if desired_state == "stop":
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
    "mutation",
    [
        "run_head",
        "run_conclusion",
        "artifact_run",
        "artifact_head",
        "artifact_digest",
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
        "manifest_hash",
        "manifest_identity",
        "row_hash",
        "row_count",
    ],
)
def test_stop_telemetry_mutations_fail_closed(mutation):
    expected, run, artifact, _archive_bytes, members = _fixture("stop")
    if mutation == "missing_telemetry":
        members.pop("shadow-telemetry.jsonl.gz")
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
