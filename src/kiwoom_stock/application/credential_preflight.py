"""Shared no-network credential preflight for all application entry points."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from kiwoom_stock.application.credentials import (
    CredentialProvider,
    KiwoomClientCredentials,
)
from kiwoom_stock.settings import (
    KiwoomApiMode,
    Settings,
    SettingsIssue,
    SettingsValidationError,
)


class SettingsValidator(Protocol):
    def validate_environment_settings(self) -> Settings:
        """Validate canonical and legacy settings without activation."""


CredentialProviderFactory = Callable[[Path], CredentialProvider]


@dataclass(frozen=True)
class CredentialPreflight:
    settings: Settings
    credentials: KiwoomClientCredentials | None

    def __post_init__(self) -> None:
        enabled = self.settings.kiwoom.api_mode is not KiwoomApiMode.DISABLED
        if enabled != (self.credentials is not None):
            raise ValueError("credential preflight result does not match API mode")


def preflight_settings(
    settings: Settings,
    provider_factory: CredentialProviderFactory,
) -> CredentialPreflight:
    """Validate/read credentials once without creating clients or network I/O."""

    if settings.kiwoom.api_mode is KiwoomApiMode.DISABLED:
        return CredentialPreflight(settings=settings, credentials=None)
    credentials_dir = settings.kiwoom.credentials_dir
    if credentials_dir is None:
        raise SettingsValidationError(
            (
                SettingsIssue(
                    "KIWOOM_CREDENTIALS_DIR",
                    "is required for enabled API mode",
                ),
            )
        )
    credentials = provider_factory(credentials_dir).load()
    return CredentialPreflight(settings=settings, credentials=credentials)


def preflight_environment(
    validator: SettingsValidator,
    provider_factory: CredentialProviderFactory,
) -> CredentialPreflight:
    """Validate all sources, including legacy JSON, then preflight credentials."""

    return preflight_settings(
        validator.validate_environment_settings(),
        provider_factory,
    )
