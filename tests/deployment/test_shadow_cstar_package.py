"""Deterministic C* package build tests."""

from pathlib import Path
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[2]
BUILDER = ROOT / "deploy/build_shadow_cstar_package.py"


def build(tmp_path: Path, role: str, name: str):
    output = tmp_path / name
    result = subprocess.run(
        [
            sys.executable, str(BUILDER), "--root", str(ROOT),
            "--role", role, "--output", str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    return output, result.stdout


def test_submitter_and_observer_packages_have_disjoint_entrypoints(tmp_path):
    submitter, output = build(tmp_path, "submitter", "submitter.zip")
    observer, _ = build(tmp_path, "observer", "observer.zip")
    assert "submitter sha256=" in output
    with zipfile.ZipFile(submitter) as archive:
        assert archive.namelist() == [
            "shadow_cstar_contract.py",
            "shadow_cstar_submitter.py",
        ]
    with zipfile.ZipFile(observer) as archive:
        assert archive.namelist() == [
            "shadow_cstar_contract.py",
            "shadow_cstar_observer.py",
            "shadow_cstar_submitter.py",
        ]


def test_package_bytes_are_deterministic(tmp_path):
    first, first_output = build(tmp_path, "submitter", "one.zip")
    second, second_output = build(tmp_path, "submitter", "two.zip")
    assert first.read_bytes() == second.read_bytes()
    assert first_output == second_output


def test_flat_lambda_zip_imports_without_repository_package_layout(tmp_path):
    package, _ = build(tmp_path, "observer", "observer.zip")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import shadow_cstar_observer, shadow_cstar_submitter, shadow_cstar_contract",
        ],
        env={"PYTHONPATH": str(package)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
