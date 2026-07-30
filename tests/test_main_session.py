"""Process-level routing tests for typed trading-session outcomes."""

import importlib
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.application.session import (
    CriticalNotificationOutcome,
    SessionEndReason,
    TradingSessionResult,
)
from kiwoom_stock.settings import Settings


def _configured_main(
    monkeypatch,
    session_result,
    *,
    run_error=None,
    close_error=None,
):
    main_module = importlib.import_module("main")
    events = []
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "disabled",
            "KIWOOM_PROCESS_NAME": "test-monitor",
        }
    )
    notifier = object()

    def run_monitor():
        events.append("run")
        if run_error is not None:
            raise run_error
        return session_result

    def close_monitor():
        events.append("close")
        if close_error is not None:
            raise close_error

    monitor = SimpleNamespace(
        notifier=notifier,
        run=MagicMock(side_effect=run_monitor),
        close=MagicMock(side_effect=close_monitor),
    )
    client = MagicMock()
    runtime = SimpleNamespace(
        settings=settings,
        app_config={
            "process_name": "test-monitor",
            "app_env": "production-like",
            "aws_s3_bucket_name": "archive-bucket",
        },
        monitor=monitor,
        client=client,
        output_dir_str="/isolated/output/20260718",
    )
    lifecycle_started = MagicMock(side_effect=lambda *args, **kwargs: events.append("started"))
    lifecycle_crashed = MagicMock(side_effect=lambda *args, **kwargs: events.append("crashed"))
    post_market = MagicMock(side_effect=lambda **kwargs: events.append("post_market") or object())
    reporter_factory = object()
    s3_factory = object()
    cleanup_files = object()
    scoped_cleanup = object()

    monkeypatch.setattr(main_module, "setup_preflight_logging", lambda: None)
    monkeypatch.setattr(main_module, "setup_structured_logging", lambda: None)
    monkeypatch.setattr(
        main_module.config,
        "validate_environment_settings",
        MagicMock(return_value=settings),
    )
    monkeypatch.setattr(main_module, "is_krx_open_on", MagicMock(return_value=True))
    monkeypatch.setattr(main_module, "create_trading_runtime", MagicMock(return_value=runtime))
    monkeypatch.setattr(main_module, "notify_monitor_started", lifecycle_started)
    monkeypatch.setattr(main_module, "notify_monitor_crashed", lifecycle_crashed)
    monkeypatch.setattr(main_module, "run_post_market_tasks", post_market)
    monkeypatch.setattr(main_module, "get_now_str", lambda: "2026-07-18 15:30:00")
    monkeypatch.setattr(main_module, "get_today_str", lambda: "2026-07-18")
    monkeypatch.setattr(main_module, "DailyReporter", reporter_factory)
    monkeypatch.setattr(main_module, "S3Manager", s3_factory)
    monkeypatch.setattr(main_module, "clean_old_csv_files", cleanup_files)
    monkeypatch.setattr(main_module, "clean_archived_csv_files", scoped_cleanup)

    return SimpleNamespace(
        module=main_module,
        events=events,
        monitor=monitor,
        client=client,
        notifier=notifier,
        started=lifecycle_started,
        crashed=lifecycle_crashed,
        post_market=post_market,
        reporter_factory=reporter_factory,
        s3_factory=s3_factory,
        cleanup_files=cleanup_files,
        scoped_cleanup=scoped_cleanup,
    )


@pytest.mark.parametrize("unresolved_codes", [(), ("005930", "035420")])
def test_main_kill_switch_skips_post_market_and_exits_one(
    monkeypatch, caplog, unresolved_codes
):
    result = TradingSessionResult(
        reason=SessionEndReason.KILL_SWITCH,
        total_pnl=-5.0,
        loss_limit=-5.0,
        unresolved_position_codes=unresolved_codes,
        critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
    )
    context = _configured_main(monkeypatch, result)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.run.assert_called_once_with()
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.started.assert_called_once_with(
        context.notifier,
        process_name="test-monitor",
        now_text="2026-07-18 15:30:00",
    )
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]
    assert "Trading session stopped by kill_switch" in caplog.text
    assert f"unresolved positions={len(unresolved_codes)}" in caplog.text


@pytest.mark.parametrize(
    "reason",
    [SessionEndReason.MARKET_CLOSED, SessionEndReason.USER_INTERRUPT],
)
def test_main_normal_terminal_results_preserve_b2_post_market_wiring(monkeypatch, reason):
    context = _configured_main(monkeypatch, TradingSessionResult(reason=reason))
    context.client.close.side_effect = lambda: context.events.append("client.close")

    assert context.module.main() is None

    context.monitor.run.assert_called_once_with()
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.crashed.assert_not_called()
    context.post_market.assert_called_once_with(
        notifier=context.notifier,
        process_name="test-monitor",
        now_text="2026-07-18 15:30:00",
        today_text="2026-07-18",
        app_env="production-like",
        output_dir_str="/isolated/output/20260718",
        s3_bucket="archive-bucket",
        reporter_factory=context.reporter_factory,
        s3_factory=context.s3_factory,
        cleanup_files=context.cleanup_files,
        scoped_cleanup=context.scoped_cleanup,
    )
    assert context.events == [
        "started",
        "run",
        "close",
        "post_market",
        "client.close",
    ]


def test_main_invalid_engine_result_uses_existing_crash_path(monkeypatch):
    context = _configured_main(monkeypatch, object())

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_called_once()
    kwargs = context.crashed.call_args.kwargs
    assert kwargs["process_name"] == "test-monitor"
    assert kwargs["now_text"] == "2026-07-18 15:30:00"
    assert isinstance(kwargs["error"], TypeError)
    assert "must return TradingSessionResult" in str(kwargs["error"])
    assert context.events == ["started", "run", "close", "crashed"]


@pytest.mark.parametrize(
    "close_error",
    [
        RuntimeError("close failed"),
        KeyboardInterrupt("close interrupt"),
        SystemExit(9),
    ],
)
def test_main_invalid_engine_result_remains_primary_when_close_fails(
    monkeypatch,
    caplog,
    close_error,
):
    context = _configured_main(monkeypatch, object(), close_error=close_error)

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_called_once()
    primary_error = context.crashed.call_args.kwargs["error"]
    assert isinstance(primary_error, TypeError)
    assert "must return TradingSessionResult" in str(primary_error)
    assert any("monitor close also failed" in note for note in primary_error.__notes__)
    assert context.events == ["started", "run", "close", "crashed"]
    assert "close also failed while preserving the primary error" in caplog.text


@pytest.mark.parametrize(
    "close_error",
    [
        RuntimeError("close failed"),
        KeyboardInterrupt("close interrupt"),
        SystemExit(9),
    ],
)
def test_main_run_error_remains_primary_for_every_close_failure(
    monkeypatch,
    caplog,
    close_error,
):
    run_error = RuntimeError("run failed")
    context = _configured_main(
        monkeypatch,
        None,
        run_error=run_error,
        close_error=close_error,
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.run.assert_called_once_with()
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_called_once()
    assert context.crashed.call_args.kwargs["error"] is run_error
    assert context.events == ["started", "run", "close", "crashed"]
    assert "close also failed while preserving the primary error" in caplog.text
    assert any("monitor close also failed" in note for note in run_error.__notes__)


def test_main_normal_result_close_failure_skips_post_market_and_uses_crash_path(
    monkeypatch,
):
    close_error = RuntimeError("close failed")
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
        close_error=close_error,
    )

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_called_once()
    assert context.crashed.call_args.kwargs["error"] is close_error
    assert context.events == ["started", "run", "close", "crashed"]


@pytest.mark.parametrize(
    "close_error",
    [KeyboardInterrupt("close interrupt"), SystemExit(9)],
)
def test_main_normal_result_close_process_control_is_reraised(
    monkeypatch,
    close_error,
):
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
        close_error=close_error,
    )

    with pytest.raises(type(close_error)) as caught:
        context.module.main()

    assert caught.value is close_error
    context.monitor.close.assert_called_once_with()
    context.client.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]


def test_main_kill_close_failure_exits_one_without_duplicate_crash_notice(
    monkeypatch,
    caplog,
):
    close_error = RuntimeError("close failed")
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            total_pnl=-5.0,
            loss_limit=-5.0,
            critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
        ),
        close_error=close_error,
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]
    assert "kill-switch shutdown cleanup failed" in caplog.text


def test_main_raw_run_keyboard_interrupt_preserves_local_termination(monkeypatch):
    run_interrupt = KeyboardInterrupt("raw interrupt")
    context = _configured_main(monkeypatch, None, run_error=run_interrupt)

    assert context.module.main() is None

    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]


@pytest.mark.parametrize(
    "close_error",
    [
        RuntimeError("close failed"),
        KeyboardInterrupt("close interrupt"),
        SystemExit(9),
    ],
)
def test_main_raw_run_keyboard_interrupt_remains_primary_when_close_fails(
    monkeypatch,
    caplog,
    close_error,
):
    run_interrupt = KeyboardInterrupt("raw interrupt")
    context = _configured_main(
        monkeypatch,
        None,
        run_error=run_interrupt,
        close_error=close_error,
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(KeyboardInterrupt) as caught:
        context.module.main()

    assert caught.value is run_interrupt
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]
    assert "close also failed while preserving the primary error" in caplog.text
    assert any("monitor close also failed" in note for note in run_interrupt.__notes__)


def test_main_raw_run_system_exit_is_reraised_after_successful_close(monkeypatch):
    run_exit = SystemExit(7)
    context = _configured_main(monkeypatch, None, run_error=run_exit)

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value is run_exit
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]


@pytest.mark.parametrize(
    "close_error",
    [
        RuntimeError("close failed"),
        KeyboardInterrupt("close interrupt"),
        SystemExit(9),
    ],
)
def test_main_raw_run_system_exit_remains_primary_when_close_fails(
    monkeypatch,
    caplog,
    close_error,
):
    run_exit = SystemExit(7)
    context = _configured_main(
        monkeypatch,
        None,
        run_error=run_exit,
        close_error=close_error,
    )

    with caplog.at_level(logging.CRITICAL), pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value is run_exit
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]
    assert "close also failed while preserving the primary error" in caplog.text
    assert any("monitor close also failed" in note for note in run_exit.__notes__)


@pytest.mark.parametrize(
    "close_error",
    [KeyboardInterrupt("close interrupt"), SystemExit(9)],
)
def test_main_kill_close_process_control_is_not_converted_to_exit_one(
    monkeypatch,
    close_error,
):
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            total_pnl=-5.0,
            loss_limit=-5.0,
            critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
        ),
        close_error=close_error,
    )

    with pytest.raises(type(close_error)) as caught:
        context.module.main()

    assert caught.value is close_error
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
    assert context.events == ["started", "run", "close"]


@pytest.mark.parametrize("failure_boundary", ["structured_logging", "started_notice"])
def test_main_failure_after_monitor_creation_closes_before_crash_notice(
    monkeypatch,
    failure_boundary,
):
    primary_error = RuntimeError(f"{failure_boundary} failed")
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
    )
    if failure_boundary == "structured_logging":
        context.module.setup_structured_logging = MagicMock(side_effect=primary_error)
    else:
        context.started.side_effect = primary_error
    context.client.close.side_effect = lambda: context.events.append("client.close")

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.run.assert_not_called()
    context.monitor.close.assert_called_once_with()
    context.post_market.assert_not_called()
    context.crashed.assert_called_once()
    assert context.crashed.call_args.kwargs["error"] is primary_error
    assert context.events[-3:] == ["close", "crashed", "client.close"]


def test_main_client_close_failure_preserves_active_system_exit(monkeypatch):
    run_exit = SystemExit(7)
    close_error = RuntimeError("local client close failed")
    context = _configured_main(monkeypatch, None, run_error=run_exit)
    context.client.close.side_effect = close_error

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value is run_exit
    assert any(
        "Kiwoom client close also failed with RuntimeError" in note
        for note in run_exit.__notes__
    )
    context.client.close.assert_called_once_with()


def test_main_normal_success_client_close_failure_is_terminal(monkeypatch):
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
    )
    close_error = RuntimeError("local client close failed")
    context.client.close.side_effect = close_error

    with pytest.raises(RuntimeError) as caught:
        context.module.main()

    assert caught.value is close_error
    context.post_market.assert_called_once()
    context.client.close.assert_called_once_with()


def test_main_runtime_construction_failure_does_not_close_uncreated_monitor(monkeypatch):
    primary_error = RuntimeError("runtime construction failed")
    context = _configured_main(
        monkeypatch,
        TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
    )
    context.module.create_trading_runtime = MagicMock(side_effect=primary_error)

    with pytest.raises(SystemExit) as caught:
        context.module.main()

    assert caught.value.code == 1
    context.monitor.run.assert_not_called()
    context.monitor.close.assert_not_called()
    context.post_market.assert_not_called()
    context.crashed.assert_not_called()
