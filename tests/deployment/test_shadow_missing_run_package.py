from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from deploy.build_shadow_missing_run_package import (
    PACKAGE_MEMBERS,
    build_package,
)


def test_detector_package_has_exact_reproducible_members(tmp_path: Path):
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"

    first_metadata = build_package(first)
    second_metadata = build_package(second)

    assert first.read_bytes() == second.read_bytes()
    assert first_metadata == second_metadata
    assert first_metadata["members"] == list(PACKAGE_MEMBERS)
    assert first_metadata["bytes"] == first.stat().st_size
    assert first_metadata["sha256"] == hashlib.sha256(
        first.read_bytes()
    ).hexdigest()
    with zipfile.ZipFile(first) as archive:
        assert archive.namelist() == list(PACKAGE_MEMBERS)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0)
                   for info in archive.infolist())


def test_detector_package_imports_lambda_handler_without_repo_modules(
    tmp_path: Path,
):
    archive = tmp_path / "detector.zip"
    build_package(archive)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(archive)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import shadow_missing_run_lambda as module; "
                "assert callable(module.handler)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_detector_package_refuses_to_overwrite_source():
    with pytest.raises(ValueError, match="overwrite source"):
        build_package(Path("deploy/shadow_missing_run_lambda.py"))
