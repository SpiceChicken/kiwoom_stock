"""Bounded read-only evidence exporter tests."""

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy/ec2/shadow_evidence_export.py"


def test_invalid_identity_fails_before_external_export(tmp_path):
    result = subprocess.run(
        [
            sys.executable, str(SCRIPT),
            "--session-date-kst", "2026-08-24",
            "--occurrence-id", "bad",
            "--release-id", "b" * 64,
            "--offset", "0", "--length", "10",
            "--expected-instance-id", "i-0e42e09d6c087ba29",
            "--region", "ap-northeast-2",
            "--database-path", str(tmp_path / "missing.db"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == ""
