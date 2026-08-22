"""Strict contracts for post-completion Shadow audit preparation."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import deploy.prepare_shadow_schedule_audit as prepare


SOURCE = "a" * 40
CONTROL = "c" * 40
RUN_ID = 123


def _run() -> dict[str, object]:
    return {
        "id": RUN_ID,
        "event": "schedule",
        "status": "completed",
        "conclusion": "success",
        "head_sha": CONTROL,
        "head_branch": "main",
        "path": ".github/workflows/cd-shadow-worker-activation.yml",
        "created_at": "2026-08-20T23:53:00Z",
        "run_started_at": "2026-08-20T23:54:00Z",
    }


def _event(run: dict[str, object] | None = None) -> dict[str, object]:
    value = _run() if run is None else run
    return {
        "repository": {"full_name": "SpiceChicken/kiwoom_stock"},
        "workflow_run": {
            "id": value["id"],
            "run_attempt": 1,
            "event": value["event"],
            "status": value["status"],
            "conclusion": value["conclusion"],
            "name": "Shadow worker activation",
            "path": value["path"],
            "head_sha": value["head_sha"],
            "head_branch": value["head_branch"],
            "head_repository": {
                "full_name": "SpiceChicken/kiwoom_stock"
            },
            "created_at": value["created_at"],
            "run_started_at": value["run_started_at"],
        },
    }


def _artifact() -> dict[str, object]:
    return {
        "id": 999,
        "name": f"shadow-worker-{SOURCE}-shadow-session-20260821",
        "size_in_bytes": 1024,
        "expired": False,
        "digest": "sha256:" + "d" * 64,
        "workflow_run": {"id": RUN_ID, "head_sha": CONTROL},
    }


def _artifacts(*items: dict[str, object]) -> dict[str, object]:
    selected = list(items) if items else [_artifact()]
    return {"total_count": len(selected), "artifacts": selected}


def test_completed_schedule_selects_one_exact_artifact_projection():
    run = _run()
    artifact = _artifact()
    result = prepare.prepare_audit(
        event=_event(run),
        run=run,
        artifact_list=_artifacts(artifact),
        expected_source_sha=SOURCE,
        artifact_metadata=copy.deepcopy(artifact),
    )
    assert result == {"artifact_id": 999, "artifact": artifact}


@pytest.mark.parametrize(
    "mutation",
    [
        "manual_event",
        "upstream_status",
        "upstream_conclusion",
        "repository",
        "head_repository",
        "workflow_name",
        "workflow_path",
        "branch",
        "head_sha",
        "run_id",
        "run_id_bool",
        "run_attempt_type",
        "malformed_timestamp",
        "event_api_timing_mismatch",
        "extra_key",
    ],
)
def test_event_and_run_provenance_mutations_fail_closed(mutation):
    run = _run()
    event = _event(run)
    event_run = event["workflow_run"]
    if mutation == "manual_event":
        event_run["event"] = "workflow_dispatch"
    elif mutation == "upstream_status":
        event_run["status"] = "in_progress"
    elif mutation == "upstream_conclusion":
        event_run["conclusion"] = "failure"
    elif mutation == "repository":
        event["repository"]["full_name"] = "foreign/repository"
    elif mutation == "head_repository":
        event_run["head_repository"]["full_name"] = "foreign/repository"
    elif mutation == "workflow_name":
        event_run["name"] = "Foreign workflow"
    elif mutation == "workflow_path":
        event_run["path"] = ".github/workflows/foreign.yml"
    elif mutation == "branch":
        event_run["head_branch"] = "feature"
    elif mutation == "head_sha":
        event_run["head_sha"] = "e" * 40
    elif mutation == "run_id":
        event_run["id"] = 124
    elif mutation == "run_id_bool":
        run["id"] = True
        event_run["id"] = True
    elif mutation == "run_attempt_type":
        event_run["run_attempt"] = True
    elif mutation == "malformed_timestamp":
        event_run["created_at"] = "2026-08-20 23:53:00Z"
    elif mutation == "event_api_timing_mismatch":
        event_run["run_started_at"] = "2026-08-20T23:55:00Z"
    else:
        event_run["unexpected"] = "value"
    with pytest.raises(prepare.AuditPreparationError):
        prepare.prepare_audit(
            event=event,
            run=run,
            artifact_list=_artifacts(),
            expected_source_sha=SOURCE,
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "duplicate",
        "expired",
        "wrong_run",
        "wrong_head",
        "wrong_name",
        "wrong_source",
        "total_count_over_page",
        "size_type",
        "oversized",
        "digest_type",
        "artifact_id_bool",
        "artifact_run_id_bool",
        "total_count_bool",
        "size_bool",
        "expired_int",
        "metadata_digest_mismatch",
        "metadata_size_mismatch",
    ],
)
def test_artifact_list_and_metadata_mutations_fail_closed(mutation):
    artifact = _artifact()
    metadata = copy.deepcopy(artifact)
    artifact_list = _artifacts(artifact)
    if mutation == "none":
        artifact_list = {"total_count": 0, "artifacts": []}
    elif mutation == "duplicate":
        duplicate = copy.deepcopy(artifact)
        duplicate["id"] = 1000
        artifact_list = _artifacts(artifact, duplicate)
    elif mutation == "expired":
        artifact["expired"] = True
    elif mutation == "wrong_run":
        artifact["workflow_run"]["id"] = 124
    elif mutation == "wrong_head":
        artifact["workflow_run"]["head_sha"] = "e" * 40
    elif mutation == "wrong_name":
        artifact["name"] = "untrusted-artifact"
    elif mutation == "wrong_source":
        artifact["name"] = (
            f"shadow-worker-{'e' * 40}-shadow-session-20260821"
        )
    elif mutation == "total_count_over_page":
        artifact_list["total_count"] = 101
    elif mutation == "size_type":
        artifact["size_in_bytes"] = 1024.0
    elif mutation == "oversized":
        artifact["size_in_bytes"] = prepare.MAX_ARCHIVE_BYTES + 1
    elif mutation == "digest_type":
        artifact["digest"] = None
    elif mutation == "artifact_id_bool":
        artifact["id"] = True
    elif mutation == "artifact_run_id_bool":
        artifact["workflow_run"]["id"] = True
    elif mutation == "total_count_bool":
        artifact_list["total_count"] = True
    elif mutation == "size_bool":
        artifact["size_in_bytes"] = True
    elif mutation == "expired_int":
        artifact["expired"] = 0
    elif mutation == "metadata_digest_mismatch":
        metadata["digest"] = "sha256:" + "e" * 64
    else:
        metadata["size_in_bytes"] = 2048
    with pytest.raises(prepare.AuditPreparationError):
        prepare.prepare_audit(
            event=_event(),
            run=_run(),
            artifact_list=artifact_list,
            expected_source_sha=SOURCE,
            artifact_metadata=(
                metadata
                if mutation.startswith("metadata_")
                else None
            ),
        )


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("filename", "raw"),
    [
        ("event.json", b'{"repository":{},"repository":{}}'),
        ("run.json", b'{"unsafe":NaN}'),
        ("artifact-list.json", b'{"total_count":1e999,"artifacts":[]}'),
        ("event.json", b'[]'),
        ("run.json", b'\xff'),
    ],
)
def test_cli_rejects_duplicate_nonfinite_type_and_utf8_without_reflection(
    tmp_path, capsys, filename, raw,
):
    event_path = tmp_path / "event.json"
    run_path = tmp_path / "run.json"
    artifacts_path = tmp_path / "artifact-list.json"
    output_path = tmp_path / "selection.json"
    _write_json(event_path, _event())
    _write_json(run_path, _run())
    _write_json(artifacts_path, _artifacts())
    (tmp_path / filename).write_bytes(raw)
    result = prepare.main([
        "--event-json", str(event_path),
        "--run-json", str(run_path),
        "--artifact-list-json", str(artifacts_path),
        "--expected-source-sha", SOURCE,
        "--output", str(output_path),
    ])
    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert captured.err.startswith(
        "shadow schedule audit preparation failed: "
    )
    assert "unsafe" not in captured.err
    assert not output_path.exists()


def test_cli_rejects_oversized_input(tmp_path, capsys):
    event_path = tmp_path / "event.json"
    run_path = tmp_path / "run.json"
    artifacts_path = tmp_path / "artifact-list.json"
    output_path = tmp_path / "selection.json"
    event_path.write_bytes(b"{" + b"x" * prepare.MAX_EVENT_BYTES + b"}")
    _write_json(run_path, _run())
    _write_json(artifacts_path, _artifacts())
    result = prepare.main([
        "--event-json", str(event_path),
        "--run-json", str(run_path),
        "--artifact-list-json", str(artifacts_path),
        "--expected-source-sha", SOURCE,
        "--output", str(output_path),
    ])
    assert result == 1
    assert "x" not in capsys.readouterr().err


@pytest.mark.parametrize("input_name", ["event", "run", "artifact-list"])
def test_cli_rejects_empty_inputs(tmp_path, capsys, input_name):
    event_path = tmp_path / "event.json"
    run_path = tmp_path / "run.json"
    artifacts_path = tmp_path / "artifact-list.json"
    output_path = tmp_path / "selection.json"
    _write_json(event_path, _event())
    _write_json(run_path, _run())
    _write_json(artifacts_path, _artifacts())
    paths = {
        "event": event_path,
        "run": run_path,
        "artifact-list": artifacts_path,
    }
    paths[input_name].write_bytes(b"")
    result = prepare.main([
        "--event-json", str(event_path),
        "--run-json", str(run_path),
        "--artifact-list-json", str(artifacts_path),
        "--expected-source-sha", SOURCE,
        "--output", str(output_path),
    ])
    assert result == 1
    assert not output_path.exists()
    assert capsys.readouterr().err.startswith(
        "shadow schedule audit preparation failed: "
    )


@pytest.mark.parametrize("input_name", ["event", "run", "artifact-list"])
def test_cli_rejects_symlink_inputs(tmp_path, capsys, input_name):
    event_target = tmp_path / "event-target.json"
    run_target = tmp_path / "run-target.json"
    artifacts_target = tmp_path / "artifact-list-target.json"
    _write_json(event_target, _event())
    _write_json(run_target, _run())
    _write_json(artifacts_target, _artifacts())
    targets = {
        "event": event_target,
        "run": run_target,
        "artifact-list": artifacts_target,
    }
    paths = {
        name: tmp_path / f"{name}.json"
        for name in targets
    }
    for name, path in paths.items():
        if name == input_name:
            path.symlink_to(targets[name])
        else:
            path.write_bytes(targets[name].read_bytes())
    output_path = tmp_path / "selection.json"
    result = prepare.main([
        "--event-json", str(paths["event"]),
        "--run-json", str(paths["run"]),
        "--artifact-list-json", str(paths["artifact-list"]),
        "--expected-source-sha", SOURCE,
        "--output", str(output_path),
    ])
    assert result == 1
    assert not output_path.exists()
    assert capsys.readouterr().err.startswith(
        "shadow schedule audit preparation failed: "
    )


def test_cli_rejects_symlink_output_without_overwrite(tmp_path, capsys):
    event_path = tmp_path / "event.json"
    run_path = tmp_path / "run.json"
    artifacts_path = tmp_path / "artifact-list.json"
    target = tmp_path / "target.json"
    output_path = tmp_path / "selection.json"
    _write_json(event_path, _event())
    _write_json(run_path, _run())
    _write_json(artifacts_path, _artifacts())
    target.write_text("preserve", encoding="utf-8")
    output_path.symlink_to(target)
    result = prepare.main([
        "--event-json", str(event_path),
        "--run-json", str(run_path),
        "--artifact-list-json", str(artifacts_path),
        "--expected-source-sha", SOURCE,
        "--output", str(output_path),
    ])
    assert result == 1
    assert target.read_text(encoding="utf-8") == "preserve"
    assert capsys.readouterr().err == (
        "shadow schedule audit preparation failed: output_invalid\n"
    )
