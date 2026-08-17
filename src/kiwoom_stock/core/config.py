"""Read-only legacy configuration views populated by explicit startup wiring."""

from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from kiwoom_stock.settings import (
    Settings,
    load_legacy_json_mappings,
    load_settings_from_environment,
)


CONFIG: Mapping[str, Any] = MappingProxyType({})
STRATEGY_CONFIG: Mapping[str, Any] = MappingProxyType({})
SCORING_CONFIG: Mapping[str, Any] = MappingProxyType({})
OUTPUT_DIR_STR = ""
_CURRENT_SETTINGS: Optional[Settings] = None

_SettingsSnapshot = Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str]


def _settings_snapshot(settings: Settings, today: date) -> _SettingsSnapshot:
    system_config, strategy_config = settings.to_legacy_mappings()
    scoring_config = settings.legacy_scoring_config
    output_dir_str = str(settings.storage.output_dir / "output" / today.strftime("%Y%m%d"))
    return system_config, strategy_config, scoring_config, output_dir_str


def report_output_dir_for(
    settings: Any,
    today: date,
    compatibility_module: Any = None,
) -> Path:
    """Resolve report output from typed settings, with a legacy-test fallback."""

    legacy_output = getattr(compatibility_module, "OUTPUT_DIR_STR", "")
    if isinstance(legacy_output, (str, Path)) and str(legacy_output):
        return Path(legacy_output)
    storage = getattr(settings, "storage", None)
    output_root = getattr(storage, "output_dir", None)
    if output_root is not None:
        return Path(output_root) / "output" / today.strftime("%Y%m%d")
    return Path(str(legacy_output)) if legacy_output else Path.cwd()


def notification_credentials_for(
    settings: Optional[Settings],
    compatibility_module: Any = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve Slack credentials from explicit settings before the compatibility view."""

    selected = settings or _CURRENT_SETTINGS
    if selected is not None:
        return (
            selected.notification.slack_bot_token,
            selected.notification.slack_channel_id,
        )
    legacy_config = getattr(compatibility_module, "CONFIG", {})
    return (
        legacy_config.get("slack_token"),
        legacy_config.get("slack_channel"),
    )


def _publish_settings(
    settings: Settings,
    system_config: Mapping[str, Any],
    strategy_config: Mapping[str, Any],
    scoring_config: Mapping[str, Any],
    output_dir_str: str,
) -> Settings:
    global CONFIG, STRATEGY_CONFIG, SCORING_CONFIG, OUTPUT_DIR_STR, _CURRENT_SETTINGS

    CONFIG = system_config
    STRATEGY_CONFIG = strategy_config
    SCORING_CONFIG = scoring_config
    OUTPUT_DIR_STR = output_dir_str
    _CURRENT_SETTINGS = settings
    return settings


def configure(settings: Settings, today: date) -> Settings:
    """Populate compatibility views without creating directories or reading files."""

    return _publish_settings(settings, *_settings_snapshot(settings, today))


def validate_environment_settings() -> Settings:
    """Validate process and legacy settings without publishing or creating output."""

    legacy = load_legacy_json_mappings()
    return load_settings_from_environment(legacy=legacy)


def activate_runtime_settings(settings: Settings, today: date) -> Settings:
    """Create dated output before atomically publishing compatibility views."""

    snapshot = _settings_snapshot(settings, today)
    Path(snapshot[3]).mkdir(parents=True, exist_ok=True)
    return _publish_settings(settings, *snapshot)


def configure_from_environment(today: date) -> Settings:
    """Idempotently wire process/legacy sources for explicit executable entry points."""

    settings = _CURRENT_SETTINGS
    if settings is None:
        settings = validate_environment_settings()
    return activate_runtime_settings(settings, today)
