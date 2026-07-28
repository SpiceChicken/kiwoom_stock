"""Security contract tests for the POSIX mounted-file credential adapter."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiwoom_stock import cli
from kiwoom_stock.application.credentials import (
    CredentialProviderError,
    SensitiveText,
)
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    APP_KEY_FILE,
    MAX_CREDENTIAL_BYTES,
    SECRET_KEY_FILE,
    StrictFileCredentialProvider,
)
from kiwoom_stock.settings import Settings, SettingsIssue, SettingsValidationError


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="strict descriptor-relative provider is POSIX-only",
)


def _credential_dir(root: Path) -> Path:
    target = root / "credentials"
    target.mkdir(mode=0o700)
    for name, value in (
        (APP_KEY_FILE, "synthetic-app-key"),
        (SECRET_KEY_FILE, "synthetic-secret-key"),
    ):
        path = target / name
        path.write_text(value + "\n", encoding="utf-8")
        path.chmod(0o400)
    return target


def test_sensitive_text_and_bundle_never_render_secret_values(tmp_path):
    secret = SensitiveText("sentinel-secret")

    assert str(secret) == "[REDACTED]"
    assert "sentinel-secret" not in repr(secret)


def test_strict_provider_reads_single_line_files_once_from_external_directory(
    tmp_path,
):
    credentials_dir = _credential_dir(tmp_path)
    provider = StrictFileCredentialProvider(
        credentials_dir,
        repository_root=Path(__file__).resolve().parents[1],
    )

    credentials = provider.load()

    assert credentials.app_key.reveal_for_auth() == "synthetic-app-key"
    assert credentials.secret_key.reveal_for_auth() == "synthetic-secret-key"
    assert "synthetic" not in repr(credentials)


@pytest.mark.parametrize(
    "mutation,restore_mode",
    [
        (lambda path: None, False),
        (lambda path: path.write_text("line-one\nline-two", encoding="utf-8"), True),
        (lambda path: path.write_text("contains\x00nul", encoding="utf-8"), True),
        (lambda path: path.write_bytes(b"\xff"), True),
        (lambda path: path.write_bytes(b"x" * (MAX_CREDENTIAL_BYTES + 1)), True),
        (lambda path: path.write_text("", encoding="utf-8"), True),
    ],
)
def test_strict_provider_rejects_unsafe_mode_or_content_without_value(
    tmp_path,
    mutation,
    restore_mode,
):
    credentials_dir = _credential_dir(tmp_path)
    target = credentials_dir / APP_KEY_FILE
    target.chmod(0o600)
    mutation(target)
    if restore_mode:
        target.chmod(0o400)
    provider = StrictFileCredentialProvider(
        credentials_dir,
        repository_root=Path(__file__).resolve().parents[1],
    )

    with pytest.raises(CredentialProviderError) as caught:
        provider.load()

    assert "synthetic-app-key" not in str(caught.value)


def test_strict_provider_rejects_symlink_and_hardlink(tmp_path):
    credentials_dir = _credential_dir(tmp_path)
    original = credentials_dir / APP_KEY_FILE
    symlink = credentials_dir / "symlink"
    symlink.symlink_to(original)
    original.unlink()
    symlink.rename(original)

    with pytest.raises(CredentialProviderError):
        StrictFileCredentialProvider(
            credentials_dir,
            repository_root=Path(__file__).resolve().parents[1],
        ).load()

    original.unlink()
    original.write_text("synthetic-app-key", encoding="utf-8")
    original.chmod(0o400)
    os.link(original, credentials_dir / "second-link")
    with pytest.raises(CredentialProviderError):
        StrictFileCredentialProvider(
            credentials_dir,
            repository_root=Path(__file__).resolve().parents[1],
        ).load()


def test_strict_provider_requires_absolute_directory_outside_repository(tmp_path):
    with pytest.raises(CredentialProviderError):
        StrictFileCredentialProvider(Path("relative"))

    repository_root = Path(__file__).resolve().parents[1]
    inside = repository_root / ".credential-provider-test-placeholder"
    with pytest.raises(CredentialProviderError):
        StrictFileCredentialProvider(inside, repository_root=repository_root)


def test_strict_provider_rejects_final_directory_symlink(tmp_path):
    credentials_dir = _credential_dir(tmp_path)
    alias = tmp_path / "credential-alias"
    alias.symlink_to(credentials_dir, target_is_directory=True)

    with pytest.raises(CredentialProviderError, match="symbolic link"):
        StrictFileCredentialProvider(
            alias,
            repository_root=Path(__file__).resolve().parents[1],
        )


def test_strict_provider_rejects_intermediate_directory_symlink(tmp_path):
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir(mode=0o700)
    credentials_dir = _credential_dir(real_parent)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CredentialProviderError, match="symbolic links"):
        StrictFileCredentialProvider(
            alias_parent / credentials_dir.name,
            repository_root=Path(__file__).resolve().parents[1],
        )


def test_strict_provider_rejects_directory_replacement_after_construction(
    tmp_path,
):
    credentials_dir = _credential_dir(tmp_path)
    provider = StrictFileCredentialProvider(
        credentials_dir,
        repository_root=Path(__file__).resolve().parents[1],
    )
    old_dir = tmp_path / "credentials-old"
    credentials_dir.rename(old_dir)
    replacement = _credential_dir(tmp_path)

    with pytest.raises(CredentialProviderError, match="identity changed"):
        provider.load()

    assert replacement.is_dir()


@pytest.mark.parametrize(
    ("mutation_trigger", "target_name"),
    (
        (APP_KEY_FILE, SECRET_KEY_FILE),
        (SECRET_KEY_FILE, APP_KEY_FILE),
    ),
)
def test_strict_provider_rejects_in_place_pair_generation_change_during_load(
    monkeypatch,
    tmp_path,
    mutation_trigger,
    target_name,
):
    credentials_dir = _credential_dir(tmp_path)
    provider = StrictFileCredentialProvider(
        credentials_dir,
        repository_root=Path(__file__).resolve().parents[1],
    )
    original_read = StrictFileCredentialProvider._read_bounded
    mutated = False

    def mutate_pair_after_bounded_read(file_fd, name):
        nonlocal mutated
        payload = original_read(file_fd, name)
        if name == mutation_trigger and not mutated:
            mutated = True
            target = credentials_dir / target_name
            target.chmod(0o600)
            target.write_text("in-place-generation-change\n", encoding="utf-8")
            target.chmod(0o400)
        return payload

    monkeypatch.setattr(
        StrictFileCredentialProvider,
        "_read_bounded",
        staticmethod(mutate_pair_after_bounded_read),
    )

    with pytest.raises(
        CredentialProviderError,
        match="changed before|changed during|changed after",
    ):
        provider.load()


def test_check_config_disabled_never_constructs_provider(monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("KIWOOM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KIWOOM_API_MODE", "disabled")
    monkeypatch.setenv("KIWOOM_PROCESS_NAME", "config-check")
    constructor = pytest.MonkeyPatch()
    provider = __import__(
        "kiwoom_stock.infrastructure.kiwoom_credentials",
        fromlist=["StrictFileCredentialProvider"],
    )
    called = False

    class UnexpectedProvider:
        def __init__(self, path):
            nonlocal called
            called = True

    constructor.setattr(provider, "StrictFileCredentialProvider", UnexpectedProvider)
    try:
        assert cli.main(["--check-config"]) == 0
    finally:
        constructor.undo()
    assert called is False


def test_check_config_enabled_preflights_files_without_client_or_network(
    monkeypatch,
    tmp_path,
):
    credentials_dir = _credential_dir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("KIWOOM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KIWOOM_API_MODE", "mock")
    monkeypatch.setenv("KIWOOM_APP_ENV", "staging")
    monkeypatch.setenv("KIWOOM_PROCESS_NAME", "config-check")
    monkeypatch.setenv("KIWOOM_CREDENTIALS_DIR", str(credentials_dir))

    assert cli.main(["--check-config"]) == 0


def test_check_config_uses_shared_validation_and_loads_enabled_provider_once(
    monkeypatch,
    tmp_path,
):
    from kiwoom_stock.core import config
    from kiwoom_stock.infrastructure import kiwoom_credentials

    credentials_dir = _credential_dir(tmp_path)
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "mock",
            "KIWOOM_APP_ENV": "staging",
            "KIWOOM_PROCESS_NAME": "config-check",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir),
        }
    )
    credentials = StrictFileCredentialProvider(
        credentials_dir,
        repository_root=Path(__file__).resolve().parents[1],
    ).load()
    provider_instance = MagicMock()
    provider_instance.load.return_value = credentials
    provider_constructor = MagicMock(return_value=provider_instance)
    validator = MagicMock(return_value=settings)
    monkeypatch.setattr(config, "validate_environment_settings", validator)
    monkeypatch.setattr(
        kiwoom_credentials,
        "StrictFileCredentialProvider",
        provider_constructor,
    )

    assert cli.main(["--check-config"]) == 0

    validator.assert_called_once_with()
    provider_constructor.assert_called_once()
    provider_instance.load.assert_called_once_with()


def test_check_config_legacy_validation_failure_never_constructs_provider(
    monkeypatch,
):
    from kiwoom_stock.core import config
    from kiwoom_stock.infrastructure import kiwoom_credentials
    validation_error = SettingsValidationError(
        (
            SettingsIssue(
                "LEGACY.CONFIG.outer.kiwoom-app-key",
                "credential keys are forbidden in legacy mappings",
            ),
        )
    )
    validate = MagicMock(side_effect=validation_error)
    provider_constructor = MagicMock(
        side_effect=AssertionError("provider must not be constructed")
    )
    monkeypatch.setattr(config, "validate_environment_settings", validate)
    monkeypatch.setattr(
        kiwoom_credentials,
        "StrictFileCredentialProvider",
        provider_constructor,
    )

    assert cli.main(["--check-config"]) == 1

    validate.assert_called_once_with()
    provider_constructor.assert_not_called()
