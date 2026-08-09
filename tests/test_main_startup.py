"""Fail-fast startup contract tests with every external boundary disabled."""

import importlib
import logging
import os
from datetime import date, datetime
from pathlib import Path
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.application.session import SessionEndReason, TradingSessionResult
from kiwoom_stock.settings import LegacyMappings, SETTING_SPECS, Settings


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
REQUIRED_SETTINGS = (
    "KIWOOM_PROCESS_NAME",
)


def _valid_mapping(output_root: Path) -> dict[str, str]:
    return {
        "KIWOOM_API_MODE": "disabled",
        "KIWOOM_PROCESS_NAME": "paper-monitor",
        "KIWOOM_OUTPUT_DIR": str(output_root),
    }


def _replace_kiwoom_environment(monkeypatch, values: dict[str, str]) -> None:
    for spec in SETTING_SPECS:
        monkeypatch.delenv(spec.name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def _subprocess_environment(values: dict[str, str]) -> dict[str, str]:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("KIWOOM_")
    }
    environment.update(values)
    environment["PYTHONPATH"] = os.pathsep.join((str(REPOSITORY_ROOT), str(SOURCE_ROOT)))
    return environment


def _clear_kiwoom_logging_handlers() -> None:
    for current_logger in (logging.getLogger(), logging.getLogger("status")):
        for handler in list(current_logger.handlers):
            if (
                getattr(handler, "_kiwoom_preflight_console", False)
                or getattr(handler, "_kiwoom_structured_file", False)
            ):
                current_logger.removeHandler(handler)
                handler.close()


def _forbidden(message: str):
    def fail(*args, **kwargs):
        raise AssertionError(message)

    return fail


class _FalsyCallable:
    def __init__(self, result):
        self.mock = MagicMock(return_value=result)

    def __bool__(self):
        return False

    def __call__(self, *args, **kwargs):
        return self.mock(*args, **kwargs)


def test_missing_settings_fail_before_date_calendar_runtime_and_files(
    monkeypatch, tmp_path, capsys
):
    _clear_kiwoom_logging_handlers()
    main_module = importlib.import_module("main")
    output_root = tmp_path / "runtime"
    _replace_kiwoom_environment(monkeypatch, {"KIWOOM_OUTPUT_DIR": str(output_root)})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module.config, "load_legacy_json_mappings", lambda: LegacyMappings())
    monkeypatch.setattr(main_module, "setup_structured_logging", _forbidden("file logging"))
    monkeypatch.setattr(main_module, "create_trading_runtime", _forbidden("runtime created"))
    monkeypatch.setattr(main_module, "notify_monitor_crashed", _forbidden("crash notice"))
    monkeypatch.setattr(main_module, "run_post_market_tasks", _forbidden("post-market"))
    monkeypatch.setattr(main_module.config.Path, "mkdir", _forbidden("output created"))
    monkeypatch.setattr(os, "makedirs", _forbidden("directory created"))
    monkeypatch.setattr(threading.Thread, "start", _forbidden("thread started"))
    monkeypatch.setattr(socket, "create_connection", _forbidden("network opened"))
    today_provider = MagicMock(side_effect=AssertionError("date read"))
    market_calendar = MagicMock(side_effect=AssertionError("calendar read"))

    try:
        with pytest.raises(SystemExit) as caught:
            main_module.main(
                today_provider=today_provider,
                market_calendar=market_calendar,
            )
        rendered = capsys.readouterr().out
    finally:
        _clear_kiwoom_logging_handlers()

    assert caught.value.code == 1
    assert all(name in rendered for name in REQUIRED_SETTINGS)
    assert ".env.example and docs/configuration.md" in rendered
    today_provider.assert_not_called()
    market_calendar.assert_not_called()
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "trades.db").exists()


def test_invalid_settings_error_redacts_secret_markers_before_date(monkeypatch, tmp_path, capsys):
    _clear_kiwoom_logging_handlers()
    main_module = importlib.import_module("main")
    output_root = tmp_path / "runtime"
    mapping = _valid_mapping(output_root)
    secret_markers = ("do-not-print-app-key", "do-not-print-secret-key", "do-not-print-token")
    mapping.update(
        {
            "KIWOOM_APP_KEY": secret_markers[0],
            "KIWOOM_SECRET_KEY": secret_markers[1],
            "KIWOOM_SLACK_BOT_TOKEN": secret_markers[2],
        }
    )
    _replace_kiwoom_environment(monkeypatch, mapping)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module.config, "load_legacy_json_mappings", lambda: LegacyMappings())
    today_provider = MagicMock(side_effect=AssertionError("date read"))

    try:
        with pytest.raises(SystemExit) as caught:
            main_module.main(today_provider=today_provider, market_calendar=lambda _: False)
        rendered = capsys.readouterr().out
    finally:
        _clear_kiwoom_logging_handlers()

    assert caught.value.code == 1
    assert "KIWOOM_SLACK_CHANNEL_ID" in rendered
    assert all(marker not in rendered for marker in secret_markers)
    today_provider.assert_not_called()
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()


def test_unexpected_settings_source_error_uses_fatal_exit_without_crash_notice(
    monkeypatch, caplog
):
    main_module = importlib.import_module("main")
    source_error = RuntimeError("legacy settings source failed")
    crash_notice = MagicMock(side_effect=AssertionError("crash notice"))
    today_provider = MagicMock(side_effect=AssertionError("date read"))
    market_calendar = MagicMock(side_effect=AssertionError("calendar read"))
    monkeypatch.setattr(main_module, "setup_preflight_logging", lambda: None)
    monkeypatch.setattr(
        main_module.config,
        "validate_environment_settings",
        MagicMock(side_effect=source_error),
    )
    monkeypatch.setattr(main_module, "notify_monitor_crashed", crash_notice)
    monkeypatch.setattr(main_module, "create_trading_runtime", _forbidden("runtime created"))

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as caught:
        main_module.main(today_provider=today_provider, market_calendar=market_calendar)

    assert caught.value.code == 1
    assert "legacy settings source failed" in caplog.text
    crash_notice.assert_not_called()
    today_provider.assert_not_called()
    market_calendar.assert_not_called()


@pytest.mark.parametrize("failure_boundary", ("today_provider", "market_calendar"))
def test_startup_callable_error_uses_fatal_exit_before_runtime(
    monkeypatch, tmp_path, caplog, failure_boundary
):
    main_module = importlib.import_module("main")
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(_valid_mapping(output_root))
    startup_date = date(2026, 7, 17)
    boundary_error = RuntimeError(f"{failure_boundary} failed")
    validate_settings = MagicMock(return_value=settings)
    today_provider = MagicMock(return_value=startup_date)
    market_calendar = MagicMock(return_value=False)
    crash_notice = MagicMock(side_effect=AssertionError("crash notice"))

    if failure_boundary == "today_provider":
        today_provider.side_effect = boundary_error
    else:
        market_calendar.side_effect = boundary_error

    monkeypatch.setattr(main_module, "setup_preflight_logging", lambda: None)
    monkeypatch.setattr(main_module.config, "validate_environment_settings", validate_settings)
    monkeypatch.setattr(
        main_module,
        "datetime",
        MagicMock(now=MagicMock(side_effect=AssertionError("default datetime used"))),
    )
    monkeypatch.setattr(main_module, "is_krx_open_on", _forbidden("default calendar used"))
    monkeypatch.setattr(main_module, "setup_structured_logging", _forbidden("file logging"))
    monkeypatch.setattr(main_module, "create_trading_runtime", _forbidden("runtime created"))
    monkeypatch.setattr(main_module, "notify_monitor_started", _forbidden("start notice"))
    monkeypatch.setattr(main_module, "notify_monitor_crashed", crash_notice)
    monkeypatch.setattr(main_module, "run_post_market_tasks", _forbidden("post-market"))

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as caught:
        main_module.main(
            today_provider=today_provider,
            market_calendar=market_calendar,
        )

    assert caught.value.code == 1
    assert "❌ 시스템 가동 중 치명적 오류 발생" in caplog.text
    assert str(boundary_error) in caplog.text
    validate_settings.assert_called_once_with()
    today_provider.assert_called_once_with()
    if failure_boundary == "today_provider":
        market_calendar.assert_not_called()
    else:
        market_calendar.assert_called_once_with(startup_date)
    crash_notice.assert_not_called()
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "trades.db").exists()


def test_valid_holiday_reads_settings_then_date_and_calendar_without_activation(
    monkeypatch, tmp_path
):
    _clear_kiwoom_logging_handlers()
    main_module = importlib.import_module("main")
    output_root = tmp_path / "runtime"
    startup_date = date(2026, 7, 18)
    events = []
    _replace_kiwoom_environment(monkeypatch, _valid_mapping(output_root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module.config, "load_legacy_json_mappings", lambda: LegacyMappings())
    real_validate = main_module.config.validate_environment_settings

    def validate_settings():
        events.append("validate")
        return real_validate()

    def provide_date():
        events.append("date")
        return startup_date

    def check_calendar(candidate):
        assert candidate == startup_date
        events.append("calendar")
        return False

    monkeypatch.setattr(main_module.config, "validate_environment_settings", validate_settings)
    monkeypatch.setattr(main_module, "setup_structured_logging", _forbidden("file logging"))
    monkeypatch.setattr(main_module, "create_trading_runtime", _forbidden("runtime created"))
    monkeypatch.setattr(main_module, "notify_monitor_started", _forbidden("start notice"))
    monkeypatch.setattr(main_module, "run_post_market_tasks", _forbidden("post-market"))
    monkeypatch.setattr(main_module.config, "_publish_settings", _forbidden("settings published"))
    monkeypatch.setattr(main_module.config.Path, "mkdir", _forbidden("output created"))
    monkeypatch.setattr(os, "makedirs", _forbidden("directory created"))
    monkeypatch.setattr(threading.Thread, "start", _forbidden("thread started"))
    monkeypatch.setattr(socket, "create_connection", _forbidden("network opened"))

    try:
        with pytest.raises(SystemExit) as caught:
            main_module.main(today_provider=provide_date, market_calendar=check_calendar)
    finally:
        _clear_kiwoom_logging_handlers()

    assert caught.value.code == 0
    assert events == ["validate", "date", "calendar"]
    assert main_module.config._CURRENT_SETTINGS is None
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "trades.db").exists()


def test_falsy_callable_seams_do_not_fall_back_to_default_time_or_calendar(
    monkeypatch, tmp_path
):
    main_module = importlib.import_module("main")
    output_root = tmp_path / "runtime"
    settings = Settings.from_mapping(_valid_mapping(output_root))
    startup_date = date(2026, 7, 18)
    today_provider = _FalsyCallable(startup_date)
    market_calendar = _FalsyCallable(False)
    default_now = MagicMock(side_effect=AssertionError("default datetime used"))

    monkeypatch.setattr(main_module, "setup_preflight_logging", lambda: None)
    monkeypatch.setattr(
        main_module.config,
        "validate_environment_settings",
        MagicMock(return_value=settings),
    )
    monkeypatch.setattr(main_module, "datetime", MagicMock(now=default_now))
    monkeypatch.setattr(main_module, "is_krx_open_on", _forbidden("default calendar used"))
    monkeypatch.setattr(main_module, "setup_structured_logging", _forbidden("file logging"))
    monkeypatch.setattr(main_module, "create_trading_runtime", _forbidden("runtime created"))

    with pytest.raises(SystemExit) as caught:
        main_module.main(
            today_provider=today_provider,
            market_calendar=market_calendar,
        )

    assert caught.value.code == 0
    today_provider.mock.assert_called_once_with()
    market_calendar.mock.assert_called_once_with(startup_date)
    default_now.assert_not_called()
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "trades.db").exists()


def test_valid_open_forwards_same_settings_and_date_before_existing_session_flow(
    monkeypatch, tmp_path
):
    main_module = importlib.import_module("main")
    startup_date = date(2026, 7, 17)
    settings = Settings.from_mapping(_valid_mapping(tmp_path / "runtime"))
    events = []
    notifier = object()
    monitor = SimpleNamespace(
        notifier=notifier,
        run=MagicMock(return_value=TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED)),
        close=MagicMock(side_effect=lambda: events.append("close")),
    )
    runtime = SimpleNamespace(
        settings=settings,
        app_config={"process_name": "paper-monitor", "app_env": "local"},
        output_dir_str=str(tmp_path / "runtime" / "output" / "20260717"),
        monitor=monitor,
    )
    runtime.shutdown_engine = monitor.close
    runtime.close = MagicMock()

    def validate_settings():
        events.append("validate")
        return settings

    def provide_date():
        events.append("date")
        return startup_date

    def check_calendar(candidate):
        assert candidate == startup_date
        events.append("calendar")
        return True

    def create_runtime(**kwargs):
        assert kwargs["today"] == startup_date
        assert kwargs["prevalidated_settings"] is settings
        events.append("runtime")
        return runtime

    monkeypatch.setattr(main_module, "setup_preflight_logging", lambda: None)
    monkeypatch.setattr(main_module, "setup_structured_logging", lambda: events.append("logging"))
    monkeypatch.setattr(main_module.config, "validate_environment_settings", validate_settings)
    monkeypatch.setattr(main_module, "create_trading_runtime", create_runtime)
    monkeypatch.setattr(main_module, "notify_monitor_started", MagicMock())
    monkeypatch.setattr(main_module, "run_post_market_tasks", MagicMock())
    monkeypatch.setattr(main_module, "get_now_str", lambda: "2026-07-17 15:30:00")
    monkeypatch.setattr(main_module, "get_today_str", lambda: "2026-07-17")

    assert main_module.main(today_provider=provide_date, market_calendar=check_calendar) is None

    assert events == ["validate", "date", "calendar", "runtime", "logging", "close"]
    monitor.run.assert_called_once_with()
    monitor.close.assert_called_once_with()


def test_explicit_calendar_uses_only_supplied_date(monkeypatch):
    market_cal = importlib.import_module("kiwoom_stock.utils.market_cal")
    is_session = MagicMock(return_value=True)
    monkeypatch.setattr(
        market_cal.xcals,
        "get_calendar",
        MagicMock(return_value=SimpleNamespace(is_session=is_session)),
    )
    monkeypatch.setattr(
        market_cal,
        "datetime",
        MagicMock(now=MagicMock(side_effect=AssertionError("system datetime read"))),
    )

    assert market_cal.is_krx_open_on(date(2026, 7, 17)) is True
    is_session.assert_called_once_with("2026-07-17")


def test_today_calendar_wrapper_forwards_system_local_date_once(monkeypatch):
    market_cal = importlib.import_module("kiwoom_stock.utils.market_cal")
    explicit = MagicMock(return_value=False)
    now = MagicMock(return_value=datetime(2026, 7, 18, 23, 59, 59))
    monkeypatch.setattr(market_cal, "is_krx_open_on", explicit)
    monkeypatch.setattr(market_cal, "datetime", MagicMock(now=now))

    assert market_cal.is_krx_open_today() is False
    now.assert_called_once_with()
    explicit.assert_called_once_with(date(2026, 7, 18))


def test_calendar_adapter_exception_remains_conservative_closed(monkeypatch, caplog):
    market_cal = importlib.import_module("kiwoom_stock.utils.market_cal")
    monkeypatch.setattr(
        market_cal.xcals,
        "get_calendar",
        MagicMock(side_effect=RuntimeError("calendar unavailable")),
    )

    with caplog.at_level(logging.ERROR):
        assert market_cal.is_krx_open_on(date(2026, 7, 17)) is False

    assert "calendar unavailable" in caplog.text


def test_real_local_calendar_handles_known_session_and_saturday():
    market_cal = importlib.import_module("kiwoom_stock.utils.market_cal")

    assert market_cal.is_krx_open_on(date(2026, 7, 17)) is True
    assert market_cal.is_krx_open_on(date(2026, 7, 18)) is False


def test_source_entrypoint_missing_settings_exits_one_without_artifacts(tmp_path):
    result = subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "main.py")],
        cwd=tmp_path,
        env=_subprocess_environment({}),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    rendered = result.stdout + result.stderr
    assert result.returncode == 1
    assert all(name in rendered for name in REQUIRED_SETTINGS)
    assert ".env.example and docs/configuration.md" in rendered
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "trades.db").exists()


def test_source_fixed_holiday_subprocess_exits_zero_without_runtime_or_artifacts(tmp_path):
    output_root = tmp_path / "runtime"
    script = """
import os
from datetime import date
from pathlib import Path
import socket
import threading

import main

def forbidden(*args, **kwargs):
    raise AssertionError("forbidden side effect")

main.create_trading_runtime = forbidden
main.setup_structured_logging = forbidden
Path.mkdir = forbidden
os.makedirs = forbidden
threading.Thread.start = forbidden
socket.create_connection = forbidden

try:
    main.main(
        today_provider=lambda: date(2026, 7, 18),
        market_calendar=lambda candidate: candidate != date(2026, 7, 18),
    )
except SystemExit as error:
    assert error.code == 0
else:
    raise AssertionError("holiday startup did not exit")

assert main.config._CURRENT_SETTINGS is None
raise SystemExit(0)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_subprocess_environment(_valid_mapping(output_root)),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not output_root.exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "trades.db").exists()


def test_source_import_smoke_has_no_startup_side_effects(tmp_path):
    script = """
import os
from pathlib import Path
import socket
import threading

def forbidden(*args, **kwargs):
    raise AssertionError("forbidden import side effect")

Path.mkdir = forbidden
os.makedirs = forbidden
threading.Thread.start = forbidden
socket.create_connection = forbidden

import main
import kiwoom_stock.application.runtime
import kiwoom_stock.utils.market_cal

assert main.config._CURRENT_SETTINGS is None
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_subprocess_environment({}),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()
    assert not (tmp_path / "trades.db").exists()
