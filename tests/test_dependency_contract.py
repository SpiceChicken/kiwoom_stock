from pathlib import Path
import re
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_gemini_dependency_and_adapter_are_modern_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert any(dependency.startswith("google-genai") for dependency in dependencies)
    assert not any(
        dependency.startswith("google-generativeai")
        for dependency in dependencies
    )

    legacy_module = "google" + ".generativeai"
    client_source = (
        ROOT / "src" / "kiwoom_stock" / "utils" / "gemini_client.py"
    ).read_text(encoding="utf-8")
    assert legacy_module not in client_source


def test_supported_python_lock_files_are_hash_pinned_and_complete():
    lock_root = ROOT / "requirements" / "locks"
    expected = {
        "runtime-py311.txt": "Python 3.11",
        "dev-py311.txt": "Python 3.11",
        "runtime-py314.txt": "Python 3.14",
        "dev-py314.txt": "Python 3.14",
    }

    for filename, python_label in expected.items():
        path = lock_root / filename
        content = path.read_text(encoding="utf-8")
        assert f"with {python_label}" in content.splitlines()[1]
        assert "google-generativeai" not in content

        package_starts = [
            match.start()
            for match in re.finditer(r"(?m)^[A-Za-z0-9_.-]+==", content)
        ]
        package_blocks = [
            content[start:end]
            for start, end in zip(package_starts, package_starts[1:] + [len(content)])
        ]
        assert package_blocks
        for block in package_blocks:
            assert "--hash=sha256:" in block

    for python_label in ("311", "314"):
        dev_content = (lock_root / f"dev-py{python_label}.txt").read_text(
            encoding="utf-8"
        )
        assert "setuptools==84.0.0" in dev_content
