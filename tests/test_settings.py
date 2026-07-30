import importlib
import logging
import os
import re
import socket
import sys
import threading
from dataclasses import FrozenInstanceError
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.settings import (
    SETTING_SPECS,
    KiwoomApiMode,
    LegacyMappings,
    KiwoomSettings,
    RuntimeSettings,
    Settings,
    SettingsValidationError,
    KiwoomEndpoint,
    load_settings_from_environment,
)


def valid_mapping():
    return {
        "KIWOOM_API_MODE": "disabled",
        "KIWOOM_PROCESS_NAME": "paper-monitor",
    }


def enabled_mapping(root: Path, mode: str = "mock"):
    credentials_dir = root / f"{mode}-credentials"
    credentials_dir.mkdir(mode=0o700)
    for name, value in (
        ("KIWOOM_APP_KEY", "synthetic-app-key"),
        ("KIWOOM_SECRET_KEY", "synthetic-secret-key"),
    ):
        target = credentials_dir / name
        target.write_text(value + "\n", encoding="utf-8")
        target.chmod(0o400)
    return {
        **valid_mapping(),
        "KIWOOM_API_MODE": mode,
        "KIWOOM_APP_ENV": "staging" if mode == "mock" else "prod",
        "KIWOOM_CREDENTIALS_DIR": str(credentials_dir),
    }


def isolated_config(monkeypatch):
    module = importlib.import_module("kiwoom_stock.core.config")
    for name in ("CONFIG", "STRATEGY_CONFIG", "SCORING_CONFIG", "OUTPUT_DIR_STR", "_CURRENT_SETTINGS"):
        monkeypatch.setattr(module, name, getattr(module, name))
    monkeypatch.setattr(module, "_CURRENT_SETTINGS", None)
    return module


def clear_kiwoom_logging_handlers():
    for logger in (logging.getLogger(), logging.getLogger("status")):
        for handler in list(logger.handlers):
            if (
                getattr(handler, "_kiwoom_preflight_console", False)
                or getattr(handler, "_kiwoom_structured_file", False)
            ):
                logger.removeHandler(handler)
                handler.close()


class _ReportDatabase:
    def __init__(
        self,
        rows=(),
        *,
        events=None,
        query_error=None,
        close_error=None,
        clear_rows_on_close=False,
    ):
        self.rows = list(rows)
        self.events = events if events is not None else []
        self.query_error = query_error
        self.close_error = close_error
        self.clear_rows_on_close = clear_rows_on_close
        self.close_calls = 0
        self.closed = False
        self.queried_dates = []

    def get_today_traded_targets(self, target_date):
        assert not self.closed
        self.events.append("database-query")
        self.queried_dates.append(target_date)
        if self.query_error is not None:
            raise self.query_error
        return self.rows

    def close(self):
        self.events.append("database-close")
        self.close_calls += 1
        self.closed = True
        if self.clear_rows_on_close:
            for row in self.rows:
                row.clear()
        if self.close_error is not None:
            raise self.close_error


def env_example_mapping():
    root = Path(__file__).resolve().parents[1]
    result = {}
    for line in (root / ".env.example").read_text(encoding="utf-8").splitlines():
        if re.fullmatch(r"KIWOOM_[A-Z0-9_]+=.*", line):
            name, value = line.split("=", 1)
            result[name] = value
    return result


def test_from_mapping_happy_path_and_defaults():
    settings = Settings.from_mapping(valid_mapping(), default_output_dir="/tmp/kiwoom-output")

    assert settings.runtime.app_env == "local"
    assert settings.kiwoom.api_mode == "disabled"
    assert settings.kiwoom.endpoint is None
    assert settings.monitoring.fast_interval_seconds == 10
    assert settings.monitoring.slow_interval_seconds == 60
    assert settings.monitoring.market_proxy_code == "069500"
    assert settings.strategy.entry_deadline == "15:00"
    assert settings.storage.output_dir == Path("/tmp/kiwoom-output")
    assert settings.database.path == Path("trades.db")


@pytest.mark.parametrize(
    ("mode", "endpoint"),
    [
        ("mock", KiwoomEndpoint.MOCK),
        ("prod", KiwoomEndpoint.PROD),
    ],
)
def test_enabled_mode_derives_only_the_official_endpoint(tmp_path, mode, endpoint):
    settings = Settings.from_mapping(enabled_mapping(tmp_path, mode))

    assert settings.kiwoom.endpoint is endpoint


def test_programmatic_kiwoom_settings_rejects_untyped_mode_and_bad_directory(
    tmp_path,
):
    with pytest.raises(TypeError, match="KiwoomApiMode"):
        KiwoomSettings("mock", tmp_path)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="absolute credential directory"):
        KiwoomSettings(KiwoomApiMode.MOCK, Path("relative"))

    disabled = KiwoomSettings(KiwoomApiMode.DISABLED, None)
    assert disabled.endpoint is None


def test_programmatic_runtime_settings_rejects_invalid_environment_and_name():
    with pytest.raises(ValueError, match="supported environment"):
        RuntimeSettings("qa", "worker")
    with pytest.raises(ValueError, match="non-empty"):
        RuntimeSettings("staging", " ")


def test_disabled_mode_rejects_stale_credential_directory(tmp_path):
    mapping = valid_mapping()
    mapping["KIWOOM_CREDENTIALS_DIR"] = str(tmp_path)

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert "KIWOOM_CREDENTIALS_DIR" in {
        issue.name for issue in caught.value.issues
    }


def test_validation_aggregates_errors_with_help_and_canonical_names():
    invalid = {
        "KIWOOM_BASE_URL": "not-a-url",
        "KIWOOM_PROCESS_NAME": " ",
        "KIWOOM_APP_ENV": "unknown",
        "KIWOOM_OUTPUT_DIR": "/",
        "KIWOOM_DB_PATH": "../trades.db",
        "KIWOOM_FAST_INTERVAL_SECONDS": "-1",
        "KIWOOM_SLOW_INTERVAL_SECONDS": "0",
        "KIWOOM_MAX_WORKERS": "0",
        "KIWOOM_MAX_STOCKS": "0",
        "KIWOOM_ETF_KEYWORDS": "ETF,,ETN",
        "KIWOOM_DEBUG_MODE": "yes",
        "KIWOOM_DAY_TRADE_EXIT_TIME": "09:00",
        "KIWOOM_ENTRY_DEADLINE": "10:00",
        "KIWOOM_TOTAL_LOSS_LIMIT": "1",
        "KIWOOM_SLACK_BOT_TOKEN": "token-without-channel",
    }

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(invalid)

    names = {issue.name for issue in caught.value.issues}
    assert {
        "KIWOOM_BASE_URL",
        "KIWOOM_PROCESS_NAME",
        "KIWOOM_APP_ENV",
        "KIWOOM_OUTPUT_DIR",
        "KIWOOM_DB_PATH",
        "KIWOOM_FAST_INTERVAL_SECONDS",
        "KIWOOM_SLOW_INTERVAL_SECONDS",
        "KIWOOM_MAX_WORKERS",
        "KIWOOM_MAX_STOCKS",
        "KIWOOM_ETF_KEYWORDS",
        "KIWOOM_DEBUG_MODE",
        "KIWOOM_ENTRY_DEADLINE",
        "KIWOOM_TOTAL_LOSS_LIMIT",
        "KIWOOM_SLACK_CHANNEL_ID",
    } <= names
    assert ".env.example and docs/configuration.md" in str(caught.value)


def test_forbidden_ambient_credentials_fail_even_when_blank():
    mapping = valid_mapping()
    mapping["KIWOOM_APP_KEY"] = ""

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert "KIWOOM_APP_KEY" in {issue.name for issue in caught.value.issues}


def test_error_and_repr_redact_secret_values():
    mapping = valid_mapping()
    mapping["KIWOOM_APP_KEY"] = "do-not-print-app-key"
    mapping["KIWOOM_SECRET_KEY"] = "do-not-print-secret-key"
    mapping["KIWOOM_SLACK_WEBHOOK_URL"] = "do-not-print-webhook"

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    rendered = str(caught.value)
    assert "do-not-print-app-key" not in rendered
    assert "do-not-print-secret-key" not in rendered
    assert "do-not-print-webhook" not in rendered

    settings = Settings.from_mapping(valid_mapping())
    assert "KIWOOM_APP_KEY" not in repr(settings)


@pytest.mark.parametrize(
    "updates, expected_name",
    [
        ({"KIWOOM_SLACK_CHANNEL_ID": "C123"}, "KIWOOM_SLACK_BOT_TOKEN"),
        ({"KIWOOM_OUTPUT_DIR": "../outside"}, "KIWOOM_OUTPUT_DIR"),
        ({"KIWOOM_DB_PATH": "/"}, "KIWOOM_DB_PATH"),
        ({"KIWOOM_DEBUG_MODE": "1"}, "KIWOOM_DEBUG_MODE"),
        ({"KIWOOM_ETF_KEYWORDS": "ETF,ETF"}, "KIWOOM_ETF_KEYWORDS"),
    ],
)
def test_strict_pair_path_boolean_and_list_validation(updates, expected_name):
    mapping = valid_mapping()
    mapping.update(updates)

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert expected_name in {issue.name for issue in caught.value.issues}


def test_legacy_credentials_fail_closed_without_rendering_values():
    with pytest.raises(SettingsValidationError) as caught:
        LegacyMappings.from_mappings(
            {"nested": {"appkey": "legacy-app"}},
            {"secretkey": "legacy-secret"},
            {"deep": {"base_url": "https://legacy.example.invalid"}},
        )

    rendered = str(caught.value)
    assert "legacy-app" not in rendered
    assert "legacy-secret" not in rendered
    assert "legacy.example.invalid" not in rendered
    assert {issue.name for issue in caught.value.issues} == {
        "LEGACY.CONFIG.nested.appkey",
        "LEGACY.STRATEGY_CONFIG.secretkey",
        "LEGACY.SCORING_CONFIG.deep.base_url",
    }


@pytest.mark.parametrize(
    "forbidden_key",
    (
        "AppKey",
        "APP_KEY",
        "app-key",
        "SecretKey",
        "secret_key",
        "secret-key",
        "BaseUrl",
        "base_url",
        "base-url",
        "KIWOOM_APP_KEY",
        "kiwoom-secret-key",
        "KiwoomBaseUrl",
    ),
)
def test_direct_legacy_constructor_scans_sequences_and_normalized_keys(
    forbidden_key,
):
    secret_value = "must-never-render"

    with pytest.raises(SettingsValidationError) as caught:
        LegacyMappings(config={"outer": [{"inner": ({forbidden_key: secret_value},)}]})

    assert secret_value not in str(caught.value)
    assert any(
        issue.name.endswith(forbidden_key) for issue in caught.value.issues
    )


def test_legacy_constructor_rejects_cycles_without_recursion_error():
    cyclic = {}
    cyclic["nested"] = [cyclic]

    with pytest.raises(SettingsValidationError, match="cyclic containers"):
        LegacyMappings(config=cyclic)


def test_legacy_credential_scan_allows_similar_noncredential_keys():
    legacy = LegacyMappings(
        config={
            "application_key": "ordinary-setting",
            "base_url_timeout": 3,
            "secretary": {"app_key_rotation_days": 30},
        }
    )

    assert legacy.config["application_key"] == "ordinary-setting"


@pytest.mark.parametrize(
    "forbidden_key",
    ("kiwoom-app-key", "KIWOOM_secret_key", "KiwoomBaseUrl"),
)
def test_canonical_mapping_rejects_case_and_separator_aliases(forbidden_key):
    mapping = valid_mapping()
    mapping[forbidden_key] = "must-never-render"

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert forbidden_key in {issue.name for issue in caught.value.issues}
    assert "must-never-render" not in str(caught.value)


@pytest.mark.parametrize(
    ("mode", "app_env"),
    (("mock", "local"), ("mock", "prod"), ("prod", "staging"), ("prod", "dev")),
)
def test_enabled_api_mode_rejects_wrong_application_environment(
    tmp_path,
    mode,
    app_env,
):
    mapping = enabled_mapping(tmp_path, mode)
    mapping["KIWOOM_APP_ENV"] = app_env

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert "KIWOOM_API_MODE" in {issue.name for issue in caught.value.issues}


def test_main_merge_keeps_canonical_values_over_legacy_strategy_top_level():
    legacy = LegacyMappings.from_mappings(
        {"strategy": {"unmapped_rule": "config-value"}},
        {
            "process_name": "legacy-process",
            "app_env": "prod",
            "aws_s3_bucket_name": "legacy-bucket",
            "aws_region": "ap-northeast-2",
            "webhook_url": "https://legacy.example.invalid/slack",
            "gemini_api_key": "legacy-gemini",
            "slack_token": "legacy-token",
            "slack_channel": "legacy-channel",
            "fast_interval": 999,
            "slow_interval": 999,
            "max_workers": 999,
            "market": {"proxy_code": "111111"},
            "filters": {"max_stocks": 1, "etf_keywords": ["LEGACY"]},
            "strategy": {"unmapped_rule": "strategy-value"},
        },
    )
    mapping = {
        **valid_mapping(),
        "KIWOOM_APP_ENV": "local",
        "KIWOOM_SLACK_WEBHOOK_URL": "https://canonical.example.invalid/slack",
        "KIWOOM_SLACK_BOT_TOKEN": "canonical-token",
        "KIWOOM_SLACK_CHANNEL_ID": "canonical-channel",
        "KIWOOM_GEMINI_API_KEY": "canonical-gemini",
        "KIWOOM_S3_BUCKET_NAME": "canonical-bucket",
        "KIWOOM_AWS_REGION": "us-east-1",
        "KIWOOM_FAST_INTERVAL_SECONDS": "11",
        "KIWOOM_SLOW_INTERVAL_SECONDS": "61",
        "KIWOOM_MAX_WORKERS": "9",
        "KIWOOM_MARKET_PROXY_CODE": "222222",
        "KIWOOM_MAX_STOCKS": "12",
        "KIWOOM_ETF_KEYWORDS": "ETF,ETN",
    }

    settings = Settings.from_mapping(mapping, legacy=legacy)
    system_config, strategy_config = settings.to_legacy_mappings()
    app_config = {**system_config, **strategy_config}

    assert "appkey" not in app_config
    assert "secretkey" not in app_config
    assert "base_url" not in app_config
    assert app_config["process_name"] == "paper-monitor"
    assert app_config["app_env"] == "local"
    assert app_config["aws_s3_bucket_name"] == "canonical-bucket"
    assert app_config["aws_region"] == "us-east-1"
    assert app_config["webhook_url"] == "https://canonical.example.invalid/slack"
    assert app_config["gemini_api_key"] == "canonical-gemini"
    assert app_config["slack_token"] == "canonical-token"
    assert app_config["slack_channel"] == "canonical-channel"
    assert app_config["fast_interval"] == 11
    assert app_config["slow_interval"] == 61
    assert app_config["max_workers"] == 9
    assert app_config["market"]["proxy_code"] == "222222"
    assert app_config["filters"]["max_stocks"] == 12
    assert app_config["filters"]["etf_keywords"] == ("ETF", "ETN")
    assert app_config["strategy"]["unmapped_rule"] == "strategy-value"
    assert "appkey" not in strategy_config
    assert "market" not in strategy_config
    assert any("CONFIG.strategy.unmapped_rule conflicts" in item for item in settings.diagnostics.warnings)


def test_top_level_legacy_credential_values_fail_closed():
    with pytest.raises(SettingsValidationError) as caught:
        LegacyMappings.from_mappings(
            {
                "appkey": "config-app",
                "secretkey": "secret",
                "base_url": "https://api.example.invalid",
            },
            {"appkey": "strategy-app"},
        )

    assert all(
        issue.rule == "credential keys are forbidden in legacy mappings"
        for issue in caught.value.issues
    )


def test_unknown_canonical_variable_is_not_ignored():
    mapping = valid_mapping()
    mapping["KIWOOM_UNDOCUMENTED_SWITCH"] = "true"

    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(mapping)

    assert "KIWOOM_UNDOCUMENTED_SWITCH" in {issue.name for issue in caught.value.issues}


def test_optional_blank_values_disable_without_legacy_fallback():
    legacy = LegacyMappings.from_mappings(
        {
            "webhook_url": "https://legacy.example.invalid/slack",
            "slack_token": "legacy-token",
            "slack_channel": "legacy-channel",
            "gemini_api_key": "legacy-gemini",
            "aws_s3_bucket_name": "legacy-bucket",
            "aws_region": "ap-northeast-2",
        }
    )
    mapping = {
        **valid_mapping(),
        "KIWOOM_CREDENTIALS_DIR": "",
        "KIWOOM_SLACK_WEBHOOK_URL": "",
        "KIWOOM_SLACK_BOT_TOKEN": "",
        "KIWOOM_SLACK_CHANNEL_ID": "",
        "KIWOOM_GEMINI_API_KEY": "",
        "KIWOOM_S3_BUCKET_NAME": "",
        "KIWOOM_AWS_REGION": "",
        "KIWOOM_ETF_KEYWORDS": "",
    }

    settings = Settings.from_mapping(mapping, legacy=legacy)

    assert settings.kiwoom.credentials_dir is None
    assert settings.notification.slack_webhook_url is None
    assert settings.notification.slack_bot_token is None
    assert settings.notification.slack_channel_id is None
    assert settings.notification.gemini_api_key is None
    assert settings.storage.s3_bucket_name is None
    assert settings.storage.aws_region is None
    assert settings.monitoring.etf_keywords == ()


def test_env_example_loads_after_required_values_are_filled():
    mapping = env_example_mapping()
    mapping.update(valid_mapping())

    settings = Settings.from_mapping(mapping)

    assert settings.kiwoom.credentials_dir is None
    assert settings.notification.slack_webhook_url is None
    assert settings.notification.slack_bot_token is None
    assert settings.notification.slack_channel_id is None
    assert settings.notification.gemini_api_key is None
    assert settings.storage.s3_bucket_name is None
    assert settings.storage.aws_region is None


def test_settings_and_compatibility_views_are_immutable():
    settings = Settings.from_mapping(
        {**valid_mapping(), "KIWOOM_ETF_KEYWORDS": "ETF,ETN"},
        legacy_config={"market": {"existing": "value"}},
    )
    system, strategy = settings.to_legacy_mappings()

    with pytest.raises(FrozenInstanceError):
        settings.runtime.app_env = "prod"
    with pytest.raises(TypeError):
        system["process_name"] = "replacement"
    with pytest.raises(TypeError):
        system["market"]["existing"] = "replacement"
    assert settings.monitoring.etf_keywords == ("ETF", "ETN")
    assert strategy["strategy"]["entry_deadline"] == "15:00"


def test_process_environment_loader_is_the_explicit_source(monkeypatch, tmp_path):
    for spec in SETTING_SPECS:
        monkeypatch.delenv(spec.name, raising=False)
    for name, value in valid_mapping().items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)

    settings = load_settings_from_environment()

    assert settings.storage.output_dir == tmp_path
    assert settings.diagnostics.sources["KIWOOM_API_MODE"] == "process environment or secret file"


def test_core_config_import_has_no_environment_clock_fs_thread_or_network_side_effect(monkeypatch):
    sys.modules.pop("kiwoom_stock.core.config", None)
    getenv = MagicMock(side_effect=AssertionError("environment read"))
    makedirs = MagicMock(side_effect=AssertionError("directory creation"))
    thread_start = MagicMock(side_effect=AssertionError("thread start"))
    network = MagicMock(side_effect=AssertionError("network access"))
    monkeypatch.setattr(os, "getenv", getenv)
    monkeypatch.setattr(os, "makedirs", makedirs)
    monkeypatch.setattr(threading.Thread, "start", thread_start)
    monkeypatch.setattr(socket, "create_connection", network)

    module = importlib.import_module("kiwoom_stock.core.config")

    assert dict(module.CONFIG) == {}
    assert dict(module.STRATEGY_CONFIG) == {}
    assert module.OUTPUT_DIR_STR == ""
    getenv.assert_not_called()
    makedirs.assert_not_called()
    thread_start.assert_not_called()
    network.assert_not_called()


def test_core_config_explicit_wiring_calculates_path_without_creating_it(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=tmp_path)
    config.configure(settings, today=date(2026, 7, 17))

    assert config.OUTPUT_DIR_STR == str(tmp_path / "output" / "20260717")
    assert not (tmp_path / "output").exists()
    with pytest.raises(TypeError):
        config.CONFIG["process_name"] = "replacement"


def test_validate_environment_settings_reads_once_without_publishing_or_creating_output(
    monkeypatch, tmp_path
):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    legacy = LegacyMappings()
    legacy_loader = MagicMock(return_value=legacy)
    settings_loader = MagicMock(return_value=settings)
    original_views = (
        config.CONFIG,
        config.STRATEGY_CONFIG,
        config.SCORING_CONFIG,
        config.OUTPUT_DIR_STR,
        config._CURRENT_SETTINGS,
    )
    monkeypatch.setattr(config, "load_legacy_json_mappings", legacy_loader)
    monkeypatch.setattr(config, "load_settings_from_environment", settings_loader)

    assert config.validate_environment_settings() is settings

    legacy_loader.assert_called_once_with()
    settings_loader.assert_called_once_with(legacy=legacy)
    assert (
        config.CONFIG,
        config.STRATEGY_CONFIG,
        config.SCORING_CONFIG,
        config.OUTPUT_DIR_STR,
        config._CURRENT_SETTINGS,
    ) == original_views
    assert not output_root.exists()


def test_activate_runtime_settings_creates_output_before_publishing(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    output_dir = output_root / "output" / "20260717"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    publish = MagicMock(wraps=config._publish_settings)
    monkeypatch.setattr(config, "_publish_settings", publish)

    assert config.activate_runtime_settings(settings, today=date(2026, 7, 17)) is settings

    assert output_dir.is_dir()
    assert publish.call_count == 1
    assert config._CURRENT_SETTINGS is settings
    assert config.OUTPUT_DIR_STR == str(output_dir)


def test_activate_runtime_settings_does_not_publish_when_output_creation_fails(
    monkeypatch, tmp_path
):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    publish = MagicMock(side_effect=AssertionError("settings published"))
    monkeypatch.setattr(config, "_publish_settings", publish)
    monkeypatch.setattr(config.Path, "mkdir", MagicMock(side_effect=OSError("cannot create output")))

    with pytest.raises(OSError, match="cannot create output"):
        config.activate_runtime_settings(settings, today=date(2026, 7, 17))

    publish.assert_not_called()
    assert config.OUTPUT_DIR_STR == ""
    assert dict(config.CONFIG) == {}
    assert config._CURRENT_SETTINGS is None


def test_configure_from_environment_creates_date_output_after_validation(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "missing" / "runtime"
    output_dir = output_root / "output" / "20260717"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    events = []

    def validated(legacy):
        assert not output_root.exists()
        events.append("validated")
        return settings

    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(config, "load_settings_from_environment", validated)

    assert config.configure_from_environment(today=date(2026, 7, 17)) is settings
    assert output_dir.is_dir()
    assert config.OUTPUT_DIR_STR == str(output_dir)
    assert config.configure_from_environment(today=date(2026, 7, 17)) is settings
    assert events == ["validated"]


def test_configure_from_environment_updates_output_on_date_rollover(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    events = []

    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(
        config,
        "load_settings_from_environment",
        MagicMock(side_effect=lambda legacy: events.append("settings") or settings),
    )

    assert config.configure_from_environment(today=date(2026, 7, 17)) is settings
    assert config.OUTPUT_DIR_STR == str(output_root / "output" / "20260717")
    assert config.configure_from_environment(today=date(2026, 7, 18)) is settings
    assert config.OUTPUT_DIR_STR == str(output_root / "output" / "20260718")
    assert (output_root / "output" / "20260718").is_dir()
    assert events == ["settings"]


def test_configure_from_environment_does_not_publish_when_output_creation_fails(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(valid_mapping(), default_output_dir=output_root)
    events = []
    loader = MagicMock(side_effect=lambda legacy: settings)
    real_mkdir = config.Path.mkdir

    def failing_once(self, *args, **kwargs):
        events.append(str(self))
        if len(events) == 1:
            raise OSError("cannot create output")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(config, "load_settings_from_environment", loader)
    monkeypatch.setattr(config.Path, "mkdir", failing_once)

    with pytest.raises(OSError):
        config.configure_from_environment(today=date(2026, 7, 17))

    assert config.OUTPUT_DIR_STR == ""
    assert dict(config.CONFIG) == {}
    assert config._CURRENT_SETTINGS is None
    assert config.configure_from_environment(today=date(2026, 7, 17)) is settings
    assert config.OUTPUT_DIR_STR == str(output_root / "output" / "20260717")
    assert events[0] == str(output_root / "output" / "20260717")
    assert loader.call_count == 2


def test_invalid_environment_settings_create_no_output(monkeypatch, tmp_path):
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    for name in tuple(os.environ):
        if name.startswith("KIWOOM_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KIWOOM_OUTPUT_DIR", str(output_root))
    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())

    with pytest.raises(SettingsValidationError):
        config.configure_from_environment(today=date(2026, 7, 17))

    assert not output_root.exists()


def test_main_creates_output_before_client_and_engine(monkeypatch, tmp_path):
    clear_kiwoom_logging_handlers()
    main_module = importlib.import_module("main")
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    output_dir = output_root / "output" / "20260717"
    database_path = tmp_path / "configured" / "paper.db"
    database_path.parent.mkdir()
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=output_root,
    )
    events = []
    monitor = MagicMock()
    monitor.run.side_effect = KeyboardInterrupt

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "config", config)
    monkeypatch.setattr(main_module, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))))
    monkeypatch.setattr(main_module, "is_krx_open_on", lambda target_date: target_date == date(2026, 7, 17))
    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(
        config,
        "load_settings_from_environment",
        MagicMock(side_effect=lambda legacy: events.append("settings") or settings),
    )

    def client_factory(**kwargs):
        assert output_dir.is_dir()
        events.append("client")
        return MagicMock()

    def engine_factory(
        client,
        app_config,
        *,
        ledger,
        physical_state_repository,
    ):
        assert output_dir.is_dir()
        assert Path(ledger.db_path) == database_path
        events.append("engine")

        def close_resources():
            physical_state_repository.close()
            ledger.close()

        monitor.close.side_effect = close_resources
        return monitor

    monkeypatch.setattr(main_module, "KiwoomClient", client_factory)
    monkeypatch.setattr(main_module, "TradingEngine", engine_factory)

    main_module.main()

    assert events == ["settings", "client", "engine"]
    assert (tmp_path / "logs" / "trading.log").is_file()
    assert (tmp_path / "logs" / "error.log").is_file()
    assert (tmp_path / "logs" / "status.log").is_file()
    assert database_path.is_file()
    assert not (tmp_path / "trades.db").exists()
    clear_kiwoom_logging_handlers()


def test_standalone_chart_tool_creates_output_before_client_database_and_csv(monkeypatch, tmp_path):
    chart_tool = importlib.import_module("tools.extract_1min_chart")
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    output_dir = output_root / "output" / "20260717"
    database_path = tmp_path / "configured-chart.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=output_root,
    )
    events = []
    fake_db = _ReportDatabase(
        [{"stock_code": "005930", "stock_name": "Sample"}],
        events=events,
        clear_rows_on_close=True,
    )
    collector = MagicMock()

    def fetch_minute_chart(code, tic):
        assert fake_db.closed
        assert (code, tic) == ("005930", "1")
        events.append("minute-fetch")
        return [{"체결시간": "20260717100000", "현재가": "100"}]

    collector.fetch_minute_chart.side_effect = fetch_minute_chart
    monkeypatch.setattr(chart_tool, "config", config)
    monkeypatch.setattr(chart_tool, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))))
    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(
        config,
        "load_settings_from_environment",
        MagicMock(side_effect=lambda legacy: events.append("settings") or settings),
    )

    def client_factory(**kwargs):
        assert output_dir.is_dir()
        events.append("client")
        return MagicMock()

    def database_factory(path):
        assert output_dir.is_dir()
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(chart_tool, "KiwoomClient", client_factory)
    monkeypatch.setattr(chart_tool, "MarketDataCollector", MagicMock(return_value=collector))
    monkeypatch.setattr(chart_tool, "TradeLogger", database_factory)

    expected = output_dir / "Sample_005930_1min_2026-07-17.csv"
    assert chart_tool.extract_and_save_1min_chart("2026-07-17") == [str(expected)]
    assert expected.is_file()
    assert fake_db.close_calls == 1
    assert fake_db.queried_dates == ["2026-07-17"]
    assert events == [
        "settings",
        "client",
        "database",
        "database-query",
        "database-close",
        "minute-fetch",
    ]
    assert not (tmp_path / "trades.db").exists()


def test_standalone_trade_validator_creates_output_before_database(monkeypatch, tmp_path):
    validator_tool = importlib.import_module("tools.trade_validator")
    config = isolated_config(monkeypatch)
    output_root = tmp_path / "runtime"
    output_dir = output_root / "output" / "20260717"
    database_path = tmp_path / "configured-analysis.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=output_root,
    )
    events = []
    fake_db = _ReportDatabase(events=events)
    monkeypatch.setattr(validator_tool, "config", config)
    monkeypatch.setattr(
        validator_tool, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17)))
    )
    monkeypatch.setattr(config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(
        config,
        "load_settings_from_environment",
        MagicMock(side_effect=lambda legacy: events.append("settings") or settings),
    )

    def database_factory(path):
        assert output_dir.is_dir()
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(validator_tool, "TradeLogger", database_factory)

    assert validator_tool.analyze_trade_efficiency("2026-07-17") is None
    assert fake_db.close_calls == 1
    assert fake_db.queried_dates == ["2026-07-17"]
    assert events == ["settings", "database", "database-query", "database-close"]
    assert not (tmp_path / "trades.db").exists()


def test_standalone_chart_tool_closes_configured_database_when_targets_are_empty(
    monkeypatch,
    tmp_path,
):
    chart_tool = importlib.import_module("tools.extract_1min_chart")
    database_path = tmp_path / "empty-chart.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=tmp_path,
    )
    config_module = MagicMock()
    config_module.CONFIG = dict(settings.to_legacy_mappings()[0])
    config_module.OUTPUT_DIR_STR = str(tmp_path)
    config_module.validate_environment_settings.return_value = settings
    config_module.activate_runtime_settings.return_value = settings
    events = []
    fake_db = _ReportDatabase(events=events)
    collector = MagicMock()

    def database_factory(path):
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(chart_tool, "config", config_module)
    monkeypatch.setattr(chart_tool, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))))
    monkeypatch.setattr(chart_tool, "KiwoomClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(chart_tool, "MarketDataCollector", MagicMock(return_value=collector))
    monkeypatch.setattr(chart_tool, "TradeLogger", database_factory)

    assert chart_tool.extract_and_save_1min_chart("2026-07-17") == []
    assert fake_db.close_calls == 1
    assert events == ["database", "database-query", "database-close"]
    collector.fetch_minute_chart.assert_not_called()
    assert not (tmp_path / "trades.db").exists()


def test_standalone_trade_validator_materializes_rows_before_database_close(
    monkeypatch,
    tmp_path,
):
    validator_tool = importlib.import_module("tools.trade_validator")
    database_path = tmp_path / "analysis-success.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=tmp_path,
    )
    config_module = MagicMock()
    config_module.OUTPUT_DIR_STR = str(tmp_path)
    config_module.configure_from_environment.return_value = settings
    events = []
    row = {
        "id": 1,
        "stock_code": "005930",
        "stock_name": "Sample",
        "buy_price": 100.0,
        "thrust": 1.5,
        "gravity": -0.2,
        "drag": -0.1,
        "magnetic": 0.3,
        "jerk": 0.2,
        "impulse": 0.1,
        "net_force": 1.8,
        "buy_time": "2026-07-17 09:00:00",
        "buy_regime": "TREND",
        "sell_price": 103.0,
        "profit_rate": 3.0,
        "sell_time": "2026-07-17 10:00:00",
        "sell_reason": "target",
        "status": "CLOSED",
    }
    fake_db = _ReportDatabase(
        [row],
        events=events,
        clear_rows_on_close=True,
    )

    def database_factory(path):
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(validator_tool, "config", config_module)
    monkeypatch.setattr(
        validator_tool,
        "datetime",
        MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))),
    )
    monkeypatch.setattr(validator_tool, "TradeLogger", database_factory)

    expected = tmp_path / "physics_trade_analysis_2026-07-17.csv"
    assert validator_tool.analyze_trade_efficiency("2026-07-17") == str(expected)
    assert fake_db.close_calls == 1
    assert events == ["database", "database-query", "database-close"]
    contents = expected.read_bytes()
    assert contents.startswith(b"\xef\xbb\xbf")
    assert contents.decode("utf-8-sig").splitlines()[0] == (
        "id,stock_code,stock_name,buy_price,thrust,gravity,drag,magnetic,jerk,"
        "impulse,net_force,buy_time,buy_regime,sell_price,profit_rate,sell_time,"
        "sell_reason,status,primary_driver,judgement"
    )
    assert "Thrust,🎯 정밀타격" in contents.decode("utf-8-sig")
    assert not (tmp_path / "trades.db").exists()


def test_chart_query_error_remains_primary_when_database_close_is_interrupted(
    monkeypatch,
    tmp_path,
):
    chart_tool = importlib.import_module("tools.extract_1min_chart")
    database_path = tmp_path / "chart-query-error.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=tmp_path,
    )
    config_module = MagicMock()
    config_module.CONFIG = dict(settings.to_legacy_mappings()[0])
    config_module.OUTPUT_DIR_STR = str(tmp_path)
    config_module.validate_environment_settings.return_value = settings
    config_module.activate_runtime_settings.return_value = settings
    query_error = RuntimeError("query failed")
    close_error = KeyboardInterrupt("close interrupted")
    events = []
    fake_db = _ReportDatabase(
        events=events,
        query_error=query_error,
        close_error=close_error,
    )
    collector = MagicMock()
    target_logger = MagicMock()

    def database_factory(path):
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(chart_tool, "config", config_module)
    monkeypatch.setattr(chart_tool, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))))
    monkeypatch.setattr(chart_tool, "KiwoomClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(chart_tool, "MarketDataCollector", MagicMock(return_value=collector))
    monkeypatch.setattr(chart_tool, "TradeLogger", database_factory)
    monkeypatch.setattr(chart_tool, "logger", target_logger)

    with pytest.raises(RuntimeError) as caught:
        chart_tool.extract_and_save_1min_chart("2026-07-17")

    assert caught.value is query_error
    assert fake_db.close_calls == 1
    assert events == ["database", "database-query", "database-close"]
    assert query_error.__notes__ == [
        "report DB close also failed: close interrupted"
    ]
    target_logger.critical.assert_called_once()
    collector.fetch_minute_chart.assert_not_called()


def test_chart_database_close_failure_prevents_minute_fetch(monkeypatch, tmp_path):
    chart_tool = importlib.import_module("tools.extract_1min_chart")
    database_path = tmp_path / "chart-close-error.db"
    settings = Settings.from_mapping(
        {**enabled_mapping(tmp_path), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=tmp_path,
    )
    config_module = MagicMock()
    config_module.CONFIG = dict(settings.to_legacy_mappings()[0])
    config_module.OUTPUT_DIR_STR = str(tmp_path)
    config_module.validate_environment_settings.return_value = settings
    config_module.activate_runtime_settings.return_value = settings
    close_error = RuntimeError("close failed")
    fake_db = _ReportDatabase(
        [{"stock_code": "005930", "stock_name": "Sample"}],
        close_error=close_error,
    )
    collector = MagicMock()

    monkeypatch.setattr(chart_tool, "config", config_module)
    monkeypatch.setattr(chart_tool, "datetime", MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))))
    monkeypatch.setattr(chart_tool, "KiwoomClient", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(chart_tool, "MarketDataCollector", MagicMock(return_value=collector))
    monkeypatch.setattr(chart_tool, "TradeLogger", lambda path: fake_db)

    with pytest.raises(RuntimeError) as caught:
        chart_tool.extract_and_save_1min_chart("2026-07-17")

    assert caught.value is close_error
    assert fake_db.close_calls == 1
    collector.fetch_minute_chart.assert_not_called()


def test_trade_query_error_closes_configured_database_once(monkeypatch, tmp_path):
    validator_tool = importlib.import_module("tools.trade_validator")
    database_path = tmp_path / "analysis-query-error.db"
    settings = Settings.from_mapping(
        {**valid_mapping(), "KIWOOM_DB_PATH": str(database_path)},
        default_output_dir=tmp_path,
    )
    config_module = MagicMock()
    config_module.OUTPUT_DIR_STR = str(tmp_path)
    config_module.configure_from_environment.return_value = settings
    query_error = RuntimeError("analysis query failed")
    events = []
    fake_db = _ReportDatabase(events=events, query_error=query_error)

    def database_factory(path):
        assert path == database_path
        events.append("database")
        return fake_db

    monkeypatch.setattr(validator_tool, "config", config_module)
    monkeypatch.setattr(
        validator_tool,
        "datetime",
        MagicMock(now=MagicMock(return_value=datetime(2026, 7, 17))),
    )
    monkeypatch.setattr(validator_tool, "TradeLogger", database_factory)

    with pytest.raises(RuntimeError) as caught:
        validator_tool.analyze_trade_efficiency("2026-07-17")

    assert caught.value is query_error
    assert fake_db.close_calls == 1
    assert events == ["database", "database-query", "database-close"]
    assert not list(tmp_path.glob("physics_trade_analysis_*.csv"))


def test_setting_registry_matches_example_and_documentation():
    root = Path(__file__).resolve().parents[1]
    expected = {spec.name for spec in SETTING_SPECS}
    assert len(expected) == len(SETTING_SPECS)
    example_names = {
        line.split("=", 1)[0]
        for line in (root / ".env.example").read_text(encoding="utf-8").splitlines()
        if re.fullmatch(r"KIWOOM_[A-Z0-9_]+=.*", line)
    }
    documentation = (root / "docs" / "configuration.md").read_text(encoding="utf-8")
    matrix = documentation.split("<!-- settings-matrix:start -->", 1)[1].split(
        "<!-- settings-matrix:end -->", 1
    )[0]
    documented_names = set(re.findall(r"\| `?(KIWOOM_[A-Z0-9_]+)`? \|", matrix))

    assert example_names == expected
    assert documented_names == expected
    assert next(
        spec.consumer for spec in SETTING_SPECS if spec.name == "KIWOOM_DB_PATH"
    ) == "runtime and post-market SQLite"
