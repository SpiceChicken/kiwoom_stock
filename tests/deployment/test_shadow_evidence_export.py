"""Bounded read-only evidence exporter tests."""

import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/ec2/shadow_evidence_export.py"


def test_invalid_identity_fails_before_external_export():
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--session-date-kst", "2026-08-24",
            "--occurrence-id", "bad",
            "--release-id", "b" * 64,
            "--image", "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
            "--source-sha", "d" * 40,
            "--expected-worker-sha256", "e" * 64,
            "--expected-validator-sha256", "f" * 64,
            "--expected-shadow-document-sha256", "0" * 64,
            "--offset", "0", "--length", "10",
            "--expected-instance-id", "i-0e42e09d6c087ba29",
            "--region", "ap-northeast-2",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""


def test_named_volume_export_delegates_to_canonical_worker(monkeypatch, capsys):
    import deploy.ec2.shadow_evidence_export as exporter

    completed = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"event":"telemetry_page","row_count":1}\n',
        stderr="",
    )
    with patch.object(exporter.subprocess, "run", return_value=completed) as run:
        result = exporter.main([
            "--session-date-kst", "2026-08-24",
            "--occurrence-id", "a" * 64,
            "--release-id", "b" * 64,
            "--image", "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
            "--source-sha", "d" * 40,
            "--expected-worker-sha256", "e" * 64,
            "--expected-validator-sha256", "f" * 64,
            "--expected-shadow-document-sha256", "0" * 64,
            "--offset", "0", "--length", "256",
            "--expected-instance-id", "i-0e42e09d6c087ba29",
            "--region", "ap-northeast-2",
        ])
    assert result == 0
    command = run.call_args.args[0]
    assert command[:5] == [
        "/usr/local/sbin/kiwoom-shadow-worker",
        "--inherited-lock-fd", "9", "--desired-state", "telemetry-export-page",
    ]
    assert "--database-path" not in command
    assert command[command.index("--compose-shadow-sha256") + 1] == "0" * 64
    assert run.call_args.kwargs["pass_fds"] == (9,)
    output = json.loads(capsys.readouterr().out)
    assert output["payload"]["row_count"] == 1
