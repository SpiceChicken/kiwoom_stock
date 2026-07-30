"""Host-side secret path validation tests; values are never read by the validator."""

import os
from pathlib import Path

import pytest

from docker.validate_secret_paths import validate_secret_paths
from docker.validate_secret_paths import main
from docker.validate_secret_paths import SecretPathValidationError


pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX metadata contract")


def _pair(root: Path) -> tuple[Path, Path]:
    directory = root / "credentials"
    directory.mkdir(mode=0o700)
    app = directory / "app-key"
    secret = directory / "secret-key"
    app.write_text("synthetic-app-key\n", encoding="utf-8")
    secret.write_text("synthetic-secret-key\n", encoding="utf-8")
    app.chmod(0o400)
    secret.chmod(0o400)
    return app, secret


def _environment(mode: str, app: Path, secret: Path) -> dict[str, str]:
    prefix = "KIWOOM_MOCK" if mode == "mock" else "KIWOOM_PROD"
    return {
        f"{prefix}_APP_KEY_FILE": str(app),
        f"{prefix}_SECRET_KEY_FILE": str(secret),
    }


def test_validator_accepts_hardened_external_pair_without_reading_values(tmp_path):
    app, secret = _pair(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    result = validate_secret_paths(
        "mock",
        environ=_environment("mock", app, secret),
        repository_root=checkout,
    )
    assert result == (app, secret)


def test_validator_source_stays_metadata_only():
    source = Path("docker/validate_secret_paths.py").read_text(encoding="utf-8")
    assert ".read_text(" not in source
    assert ".read_bytes(" not in source
    assert "os.open(" not in source


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda app, secret, root: {"KIWOOM_MOCK_APP_KEY_FILE": "relative"}, "absolute"),
        (lambda app, secret, root: {"KIWOOM_MOCK_APP_KEY_FILE": str(root / "checkout" / "app")}, "outside"),
        (lambda app, secret, root: {"KIWOOM_MOCK_APP_KEY_FILE": str(app.parent / "missing")}, "existing regular file"),
    ],
)
def test_validator_rejects_bad_path_contract(tmp_path, mutation, expected):
    app, secret = _pair(tmp_path)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    environment = _environment("mock", app, secret)
    repo_file = checkout / "app"
    repo_file.write_text("placeholder", encoding="utf-8")
    repo_file.chmod(0o400)
    environment.update(mutation(app, secret, tmp_path))
    with pytest.raises(SecretPathValidationError, match=expected):
        validate_secret_paths("mock", environ=environment, repository_root=checkout)


def test_validator_rejects_symlink_and_hardlink(tmp_path):
    app, secret = _pair(tmp_path)
    symlink = tmp_path / "app-link"
    symlink.symlink_to(app)
    environment = _environment("prod", symlink, secret)
    with pytest.raises(SecretPathValidationError, match="symbolic links"):
        validate_secret_paths("prod", environ=environment, repository_root=tmp_path / "checkout")

    hardlink = tmp_path / "secret-hardlink"
    os.link(secret, hardlink)
    environment = _environment("prod", app, hardlink)
    with pytest.raises(SecretPathValidationError, match="hard link"):
        validate_secret_paths("prod", environ=environment, repository_root=tmp_path / "checkout")


def test_validator_rejects_writable_parent_directory(tmp_path):
    app, secret = _pair(tmp_path)
    app.parent.chmod(0o777)
    environment = _environment("mock", app, secret)
    with pytest.raises(SecretPathValidationError, match="parent must not be"):
        validate_secret_paths("mock", environ=environment, repository_root=tmp_path / "checkout")


def test_validator_rejects_shared_file_and_invalid_mode(tmp_path):
    app, secret = _pair(tmp_path)
    environment = _environment("mock", app, app)
    with pytest.raises(SecretPathValidationError, match="distinct"):
        validate_secret_paths("mock", environ=environment, repository_root=tmp_path / "checkout")
    with pytest.raises(SecretPathValidationError, match="mode must be"):
        validate_secret_paths("disabled", environ=_environment("mock", app, secret))


def test_cli_returns_nonzero_without_environment_values(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KIWOOM_MOCK_APP_KEY_FILE", raising=False)
    monkeypatch.delenv("KIWOOM_MOCK_SECRET_KEY_FILE", raising=False)
    assert main(["--mode", "mock", "--repository-root", str(tmp_path)]) == 1
    assert "KIWOOM_MOCK_APP_KEY_FILE" in capsys.readouterr().err
