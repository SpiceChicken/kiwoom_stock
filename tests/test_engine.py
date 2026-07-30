from types import SimpleNamespace
import threading
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.application.session import (
    CriticalNotificationOutcome,
    SessionEndReason,
    TradingSessionResult,
)
from kiwoom_stock.monitoring import engine as engine_module
from kiwoom_stock.monitoring.engine import (
    TradingEngine,
    TradingEngineLifecycleError,
)
from kiwoom_stock.monitoring.manager import Position


class _RiskStrategy:
    def __init__(self, *, monitoring=True, loss_limit=-5.0):
        self.monitoring = monitoring
        self.total_loss_limit = loss_limit
        self.monitoring_checks = 0
        self.kill_checks = []

    def is_monitoring_time(self):
        self.monitoring_checks += 1
        return self.monitoring

    def is_kill_switch_activated(self, total_pnl):
        self.kill_checks.append(total_pnl)
        return total_pnl <= self.total_loss_limit


class _RiskDatabase:
    def __init__(self, realized_pnl=-2.0):
        self.realized_pnl = realized_pnl
        self.reads = 0

    def get_today_realized_pnl(self):
        self.reads += 1
        return self.realized_pnl

    def __getattr__(self, name):
        raise AssertionError(f"unexpected DB access: {name}")


class _RiskManager:
    def __init__(self, *, total_pnl, active_positions):
        self.total_pnl = total_pnl
        self.active_positions = active_positions
        self.realized_inputs = []

    def get_total_pnl_status(self, realized_pnl):
        self.realized_inputs.append(realized_pnl)
        return self.total_pnl

    def __getattr__(self, name):
        raise AssertionError(f"unexpected manager/order access: {name}")


class _RiskNotifier:
    def __init__(self, critical_error=None):
        self.critical_error = critical_error
        self.critical_messages = []
        self.error_messages = []

    def notify_critical(self, message):
        self.critical_messages.append(message)
        if self.critical_error is not None:
            raise self.critical_error

    def notify_error(self, message):
        self.error_messages.append(message)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected notifier access: {name}")


class _CloseRecorder:
    def __init__(self, name, events, error=None, *, terminal_on_error=False):
        self.name = name
        self.events = events
        self.error = error
        self.terminal_on_error = terminal_on_error
        self.is_closed = False

    def close(self):
        self.events.append(f"{self.name}.close")
        if self.error is not None:
            if self.terminal_on_error:
                self.is_closed = True
            raise self.error
        self.is_closed = True


class _RetryableLedger:
    def __init__(self, events, *, error):
        self.events = events
        self.error = error
        self.is_closed = False
        self.close_entered = threading.Event()
        self.release_close = threading.Event()
        self.block_close = False

    def close(self):
        self.events.append("ledger.close")
        self.close_entered.set()
        if self.block_close:
            assert self.release_close.wait(timeout=5)
        if self.error is not None:
            raise self.error
        self.is_closed = True


class _ExecutorRecorder:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def shutdown(self, **kwargs):
        self.events.append(("executor.shutdown", kwargs))
        if self.error is not None:
            raise self.error


def _lifecycle_engine(*, executor_error=None, physical_error=None, ledger_error=None):
    events = []
    engine = TradingEngine.__new__(TradingEngine)
    engine._lifecycle_lock = threading.Lock()
    engine._close_complete = threading.Event()
    engine._closing = False
    engine._closed = False
    engine._work_closed = False
    engine._executor_close_complete = False
    engine._physical_close_complete = False
    engine._ledger_close_complete = False
    engine._lifecycle_failure = None
    engine._lifecycle_process_control = None
    engine._terminal_result = None
    engine.executor = _ExecutorRecorder(events, executor_error)
    engine.physical_state_repository = _CloseRecorder(
        "physical",
        events,
        physical_error,
    )
    engine.db = _CloseRecorder(
        "ledger",
        events,
        ledger_error,
        terminal_on_error=True,
    )
    return engine, events


def _unexpected_boundary(*_args, **_kwargs):
    raise AssertionError("post-threshold engine boundary was called")


def _forbid_engine_clock(monkeypatch):
    monkeypatch.setattr(
        engine_module,
        "time_mod",
        SimpleNamespace(
            time=_unexpected_boundary,
            sleep=_unexpected_boundary,
        ),
    )


def _patch_constructor_dependencies(monkeypatch, events, expected_ledger, expected_physical):
    market = object()
    state_tracker = object()
    manager = SimpleNamespace(stock_names={})
    executor = _ExecutorRecorder(events)

    def state_tracker_factory(candidate):
        assert candidate is expected_physical
        events.append("state_tracker")
        return state_tracker

    def analyzer_factory(candidate_market, market_config, candidate_tracker):
        assert candidate_market is market
        assert market_config == {"proxy_code": "069500"}
        assert candidate_tracker is state_tracker
        events.append("analyzer")
        return object()

    def manager_factory(candidate_market, candidate_ledger, filters):
        assert candidate_market is market
        assert candidate_ledger is expected_ledger
        assert filters == {"max_stocks": 50}
        events.append("manager")
        return manager

    monkeypatch.setattr(engine_module, "PhysicalStateTracker", state_tracker_factory)
    monkeypatch.setattr(engine_module, "MarketAnalyzer", analyzer_factory)
    monkeypatch.setattr(
        engine_module,
        "TradingStrategy",
        lambda config: events.append(("strategy", config)) or object(),
    )
    monkeypatch.setattr(engine_module, "StockManager", manager_factory)
    monkeypatch.setattr(
        engine_module,
        "Notifier",
        lambda stock_names, config: events.append(("notifier", stock_names, config))
        or object(),
    )
    monkeypatch.setattr(
        engine_module,
        "ThreadPoolExecutor",
        lambda **kwargs: events.append(("executor", kwargs)) or executor,
    )
    return SimpleNamespace(market=market), executor


def test_engine_uses_exact_injected_ledger_and_physical_repository(monkeypatch):
    events = []
    ledger = _CloseRecorder("ledger", events)
    physical_repository = _CloseRecorder("physical", events)
    client, executor = _patch_constructor_dependencies(
        monkeypatch,
        events,
        ledger,
        physical_repository,
    )
    config = {
        "market": {"proxy_code": "069500"},
        "strategy": {"debug_mode": False},
        "filters": {"max_stocks": 50},
        "max_workers": 3,
    }

    engine = TradingEngine(
        client,
        config,
        ledger=ledger,
        physical_state_repository=physical_repository,
    )

    assert engine.db is ledger
    assert engine.physical_state_repository is physical_repository
    assert engine.executor is executor
    assert events == [
        "state_tracker",
        "analyzer",
        ("strategy", {"debug_mode": False}),
        "manager",
        ("notifier", {}, config),
        ("executor", {"max_workers": 3}),
    ]


def test_engine_legacy_constructor_builds_one_ledger_and_wrapper_with_warning(
    monkeypatch,
):
    events = []
    ledger = _CloseRecorder("ledger", events)
    physical_repository = _CloseRecorder("physical", events)
    ledger_factory = MagicMock(return_value=ledger)

    def physical_factory(candidate):
        assert candidate is ledger
        events.append("physical.construct")
        return physical_repository

    monkeypatch.setattr(engine_module, "TradeLogger", ledger_factory)
    monkeypatch.setattr(engine_module, "AsyncPhysicalStateRepository", physical_factory)
    client, _ = _patch_constructor_dependencies(
        monkeypatch,
        events,
        ledger,
        physical_repository,
    )
    config = {
        "market": {"proxy_code": "069500"},
        "strategy": {},
        "filters": {"max_stocks": 50},
    }

    with pytest.warns(DeprecationWarning, match="persistence fallback is deprecated"):
        engine = TradingEngine(client, config)

    assert engine.db is ledger
    assert engine.physical_state_repository is physical_repository
    ledger_factory.assert_called_once_with()
    assert events.count("physical.construct") == 1


def test_engine_legacy_constructor_failure_preserves_primary_and_closes_fallback(
    monkeypatch,
):
    events = []
    ledger = _CloseRecorder(
        "ledger",
        events,
        error=KeyboardInterrupt("ledger cleanup interrupted"),
    )
    physical_repository = _CloseRecorder(
        "physical",
        events,
        error=SystemExit(9),
    )
    primary_error = RuntimeError("state tracker unavailable")
    monkeypatch.setattr(engine_module, "TradeLogger", MagicMock(return_value=ledger))
    monkeypatch.setattr(
        engine_module,
        "AsyncPhysicalStateRepository",
        MagicMock(return_value=physical_repository),
    )
    monkeypatch.setattr(
        engine_module,
        "PhysicalStateTracker",
        MagicMock(side_effect=primary_error),
    )

    with pytest.warns(DeprecationWarning), pytest.raises(RuntimeError) as caught:
        TradingEngine(SimpleNamespace(market=object()), {})

    assert caught.value is primary_error
    assert events == ["physical.close", "ledger.close"]


@pytest.mark.parametrize(
    ("ledger", "physical_repository"),
    [(object(), None), (None, object())],
)
def test_engine_rejects_partial_persistence_injection(ledger, physical_repository):
    with pytest.raises(ValueError, match="must be injected together"):
        TradingEngine(
            SimpleNamespace(market=object()),
            {},
            ledger=ledger,
            physical_state_repository=physical_repository,
        )


def _risk_engine(*, total_pnl=-5.0, active_positions=None, critical_error=None, monitoring=True):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None
    engine.strategy = _RiskStrategy(monitoring=monitoring)
    engine.db = _RiskDatabase()
    engine.stock_mgr = _RiskManager(
        total_pnl=total_pnl,
        active_positions={} if active_positions is None else active_positions,
    )
    engine.notifier = _RiskNotifier(critical_error=critical_error)
    engine.analyzer = SimpleNamespace(
        update_regime=_unexpected_boundary,
        market_regime=SimpleNamespace(value="tripwire"),
    )
    engine._get_due_targets = _unexpected_boundary
    engine._prepare_cycle = _unexpected_boundary
    engine._evaluate_stocks = _unexpected_boundary
    engine._process_decisions = _unexpected_boundary
    return engine


@pytest.mark.parametrize(
    "reason",
    [SessionEndReason.MARKET_CLOSED, SessionEndReason.USER_INTERRUPT],
)
def test_normal_session_result_contract(reason):
    result = TradingSessionResult(reason=reason)

    assert result.requires_attention is False
    assert result.post_market_allowed is True
    assert result.exit_code == 0


def test_kill_session_result_contract_allows_exact_threshold_and_zero_positions():
    result = TradingSessionResult(
        reason=SessionEndReason.KILL_SWITCH,
        total_pnl=-5.0,
        loss_limit=-5.0,
        critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
    )

    assert result.requires_attention is True
    assert result.post_market_allowed is False
    assert result.exit_code == 1
    assert result.unresolved_position_codes == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"total_pnl": None},
        {"total_pnl": -4.9999},
        {"critical_notification_outcome": CriticalNotificationOutcome.NOT_APPLICABLE},
        {
            "critical_notification_outcome": CriticalNotificationOutcome.CALL_RAISED,
            "critical_notification_error_type": None,
        },
        {
            "critical_notification_outcome": CriticalNotificationOutcome.CALL_RAISED,
            "critical_notification_error_type": 1,
        },
        {
            "critical_notification_outcome": CriticalNotificationOutcome.CALL_RAISED,
            "critical_notification_error_type": "",
        },
        {"critical_notification_error_type": "RuntimeError"},
        {"unresolved_position_codes": ("B", "A")},
        {"unresolved_position_codes": ("A", "A")},
        {"unresolved_position_codes": ["A"]},
    ],
)
def test_kill_session_result_rejects_invalid_state_combinations(overrides):
    values = {
        "reason": SessionEndReason.KILL_SWITCH,
        "total_pnl": -5.0,
        "loss_limit": -5.0,
        "critical_notification_outcome": CriticalNotificationOutcome.CALL_RETURNED,
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        TradingSessionResult(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_pnl", float("nan")),
        ("total_pnl", float("inf")),
        ("total_pnl", float("-inf")),
        ("total_pnl", True),
        ("loss_limit", float("nan")),
        ("loss_limit", float("inf")),
        ("loss_limit", float("-inf")),
        ("loss_limit", False),
    ],
)
def test_kill_session_result_rejects_non_finite_or_boolean_pnl(field, value):
    values = {
        "reason": SessionEndReason.KILL_SWITCH,
        "total_pnl": -5.0,
        "loss_limit": -5.0,
        "critical_notification_outcome": CriticalNotificationOutcome.CALL_RETURNED,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        TradingSessionResult(**values)


def test_normal_session_result_rejects_kill_metadata():
    with pytest.raises(ValueError):
        TradingSessionResult(
            reason=SessionEndReason.MARKET_CLOSED,
            total_pnl=-5.0,
        )


def test_engine_close_orders_resources_and_is_idempotent():
    engine, events = _lifecycle_engine()

    engine.close()
    engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._closed is True
    assert engine._closing is False
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine.run()
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._evaluate_stocks([])
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._prepare_cycle([])


def test_engine_close_attempts_every_step_and_surfaces_ordinary_lifecycle_failure():
    engine, events = _lifecycle_engine(
        executor_error=RuntimeError("executor failed"),
        physical_error=ValueError("physical close failed"),
        ledger_error=RuntimeError("ledger close failed"),
    )

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert "evaluation executor (RuntimeError)" in str(first_error.value)
    assert "physical-state repository (ValueError)" in str(first_error.value)
    assert "paper ledger (RuntimeError)" in str(first_error.value)
    assert str(repeated_error.value) == str(first_error.value)
    assert engine._closed is True
    assert engine._closing is False


def test_engine_retries_only_an_incomplete_ledger_and_keeps_first_failure():
    engine, events = _lifecycle_engine()
    ledger = _RetryableLedger(events, error=RuntimeError("ledger remains open"))
    engine.db = ledger

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._work_closed is True
    assert engine._closed is False
    assert engine._executor_close_complete is True
    assert engine._physical_close_complete is True
    assert engine._ledger_close_complete is False
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._prepare_cycle([])

    ledger.error = None
    with pytest.raises(TradingEngineLifecycleError) as recovered_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert str(recovered_error.value) == str(first_error.value)
    assert str(repeated_error.value) == str(first_error.value)
    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
        "ledger.close",
    ]
    assert ledger.is_closed is True
    assert engine._ledger_close_complete is True
    assert engine._closed is True


def test_engine_retry_concurrent_waiter_observes_first_failure_without_repeating_steps(
    monkeypatch,
):
    engine, events = _lifecycle_engine()
    ledger = _RetryableLedger(events, error=RuntimeError("ledger remains open"))
    engine.db = ledger

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()

    ledger.error = None
    ledger.block_close = True
    ledger.close_entered.clear()
    errors = []

    def close_and_capture():
        try:
            engine.close()
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=close_and_capture)
    owner.start()
    assert ledger.close_entered.wait(timeout=5)

    close_event = engine._close_complete
    original_wait = close_event.wait
    waiter_entered = threading.Event()

    def observed_wait(*args, **kwargs):
        waiter_entered.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(close_event, "wait", observed_wait)
    waiter = threading.Thread(target=close_and_capture)
    waiter.start()
    assert waiter_entered.wait(timeout=5)
    ledger.release_close.set()
    owner.join(timeout=5)
    waiter.join(timeout=5)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, TradingEngineLifecycleError) for error in errors)
    assert all(str(error) == str(first_error.value) for error in errors)
    assert events.count("ledger.close") == 2
    assert events.count("physical.close") == 1
    assert sum(event[0] == "executor.shutdown" for event in events if isinstance(event, tuple)) == 1
    assert engine._closed is True


def test_engine_does_not_retry_a_ledger_reporting_terminal_after_close_error():
    engine, events = _lifecycle_engine(
        ledger_error=RuntimeError("historical ledger close error"),
    )

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert str(repeated_error.value) == str(first_error.value)
    assert events.count("ledger.close") == 1
    assert engine.db.is_closed is True
    assert engine._ledger_close_complete is True
    assert engine._closed is True


@pytest.mark.parametrize(
    ("executor_error", "physical_error", "process_control"),
    [
        (
            KeyboardInterrupt("executor interrupted"),
            RuntimeError("physical close failed"),
            "executor",
        ),
        (
            RuntimeError("executor failed"),
            SystemExit(7),
            "physical",
        ),
    ],
)
def test_engine_close_attempts_all_cleanup_then_reraises_process_control(
    executor_error,
    physical_error,
    process_control,
):
    expected_error = (
        executor_error if process_control == "executor" else physical_error
    )
    engine, events = _lifecycle_engine(
        executor_error=executor_error,
        physical_error=physical_error,
        ledger_error=RuntimeError("ledger close failed"),
    )

    with pytest.raises(type(expected_error)) as first_error:
        engine.close()
    with pytest.raises(type(expected_error)) as repeated_error:
        engine.close()

    assert first_error.value is expected_error
    assert repeated_error.value is expected_error
    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._lifecycle_failure is not None
    assert any("ledger close failed" in failure for failure in engine._lifecycle_failure)
    assert engine._closed is True
    assert engine._closing is False


def test_closed_engine_keeps_latched_kill_result_read_only():
    engine, events = _lifecycle_engine()
    terminal_result = TradingSessionResult(
        reason=SessionEndReason.KILL_SWITCH,
        total_pnl=-5.0,
        loss_limit=-5.0,
        critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
    )
    engine._terminal_result = terminal_result

    engine.close()

    assert engine.run() is terminal_result
    assert len(events) == 3


def test_kill_switch_latches_once_without_tick_order_or_ledger_mutation(monkeypatch):
    first_position = object()
    second_position = object()
    active_positions = {"B": second_position, "A": first_position}
    engine = _risk_engine(total_pnl=-5.0, active_positions=active_positions)
    before_mapping = dict(active_positions)
    _forbid_engine_clock(monkeypatch)

    first_result = engine.run()
    second_result = engine.run()

    assert first_result is second_result
    assert first_result.reason is SessionEndReason.KILL_SWITCH
    assert first_result.total_pnl == -5.0
    assert first_result.loss_limit == -5.0
    assert first_result.unresolved_position_codes == ("A", "B")
    assert first_result.critical_notification_outcome is CriticalNotificationOutcome.CALL_RETURNED
    assert active_positions == before_mapping
    assert active_positions["A"] is first_position
    assert active_positions["B"] is second_position
    assert engine.strategy.monitoring_checks == 1
    assert engine.strategy.kill_checks == [-5.0]
    assert engine.db.reads == 1
    assert engine.stock_mgr.realized_inputs == [-2.0]
    assert engine.notifier.error_messages == []
    assert engine.notifier.critical_messages == [
        "🚨 KILL-SWITCH ACTIVATED (PnL: -5.0%, Limit: -5.0%) | "
        "자동 청산을 실행하지 않았습니다. 미해결 활성 포지션: 2개"
    ]


def test_kill_switch_with_no_active_positions_is_still_terminal(monkeypatch):
    engine = _risk_engine(total_pnl=-6.0)
    _forbid_engine_clock(monkeypatch)

    result = engine.run()

    assert result.reason is SessionEndReason.KILL_SWITCH
    assert result.unresolved_position_codes == ()
    assert result.requires_attention is True
    assert len(engine.notifier.critical_messages) == 1


def test_loss_just_above_limit_keeps_monitoring_without_latching():
    engine = _risk_engine(total_pnl=-4.9999)

    assert engine._check_system_status() is True
    assert engine._terminal_result is None
    assert engine.strategy.kill_checks == [-4.9999]
    assert engine.notifier.critical_messages == []


@pytest.mark.parametrize(
    ("error", "expected_type"),
    [(RuntimeError("offline"), "RuntimeError"), (KeyboardInterrupt(), "KeyboardInterrupt")],
)
def test_critical_notifier_failure_stays_terminal_without_generic_retry(
    monkeypatch, error, expected_type
):
    engine = _risk_engine(total_pnl=-5.0, critical_error=error)
    _forbid_engine_clock(monkeypatch)

    first_result = engine.run()
    second_result = engine.run()

    assert first_result is second_result
    assert first_result.reason is SessionEndReason.KILL_SWITCH
    assert first_result.critical_notification_outcome is CriticalNotificationOutcome.CALL_RAISED
    assert first_result.critical_notification_error_type == expected_type
    assert len(engine.notifier.critical_messages) == 1
    assert engine.notifier.error_messages == []
    assert engine.db.reads == 1


def test_critical_notifier_system_exit_is_not_swallowed(monkeypatch):
    engine = _risk_engine(total_pnl=-5.0, critical_error=SystemExit(7))
    _forbid_engine_clock(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        engine.run()

    assert caught.value.code == 7
    assert engine._terminal_result is None
    assert len(engine.notifier.critical_messages) == 1
    assert engine.notifier.error_messages == []


def test_market_close_returns_normal_typed_result_without_pnl_or_time_access(monkeypatch):
    engine = _risk_engine(monitoring=False)
    _forbid_engine_clock(monkeypatch)

    result = engine.run()

    assert result == TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED)
    assert engine.strategy.monitoring_checks == 2
    assert engine.strategy.kill_checks == []
    assert engine.db.reads == 0
    assert engine.notifier.critical_messages == []


def test_engine_caught_keyboard_interrupt_returns_normal_typed_result():
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None

    def interrupted_check():
        raise KeyboardInterrupt

    engine._check_system_status = interrupted_check

    assert engine.run() == TradingSessionResult(reason=SessionEndReason.USER_INTERRUPT)


def test_pre_threshold_exception_retains_error_notification_and_retry(monkeypatch):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None
    engine.notifier = _RiskNotifier()
    calls = 0
    sleeps = []

    def status_check():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("risk data unavailable")
        raise KeyboardInterrupt

    engine._check_system_status = status_check
    monkeypatch.setattr(engine_module.time_mod, "sleep", sleeps.append)

    result = engine.run()

    assert result.reason is SessionEndReason.USER_INTERRUPT
    assert engine.notifier.error_messages == ["risk data unavailable"]
    assert engine.notifier.critical_messages == []
    assert sleeps == [10]


def test_process_decisions_pipeline():
    """[흐름 제어 타격] Engine -> Manager(업데이트) -> Strategy(판단) 오케스트레이션 검증"""
    # This method-level test does not need the production composition root. Bypass
    # __init__ so it cannot create the default trades.db, daemon worker, or executor.
    engine = TradingEngine.__new__(TradingEngine)
    
    mock_manager = MagicMock()
    mock_strategy = MagicMock()
    mock_notifier = MagicMock()

    engine.stock_mgr = mock_manager
    engine.strategy = mock_strategy
    engine.notifier = mock_notifier
    
    mock_updated_pos = MagicMock(spec=Position)
    mock_manager.active_positions = {"005930": mock_updated_pos}
    mock_manager.update_position_data.return_value = mock_updated_pos
    
    # 언패킹 폭발 방지
    mock_manager.process_sell_order.return_value = (True, mock_updated_pos)
    mock_manager.process_buy_order.return_value = (True, mock_updated_pos)
    
    mock_strategy.get_exit_reason.return_value = "Kinetic Exit (Velocity Drop)"
    
    verdict = {
        "stock_code": "005930", 
        "price": 80000, 
        "forces": {"net_force": -3.5, "current_velocity": 7.0}
    }
    
    # [Execute] 파이프라인 가동
    engine._process_decisions([verdict])
    
    # Assert 1: Manager 데이터 갱신
    mock_manager.update_position_data.assert_called_once_with(verdict)
    
    # Assert 2: Strategy 청산 판단
    mock_strategy.get_exit_reason.assert_called_once_with(
        mock_updated_pos, 80000, {"net_force": -3.5, "current_velocity": 7.0}
    )
    
    # Assert 3: Manager 매도 집행
    mock_manager.process_sell_order.assert_called_once_with(verdict, "Kinetic Exit (Velocity Drop)")
    
    # Assert 4: 💥 Notifier에 알림 발송이 정상적으로 지시되었는지까지 검증 (파이프라인의 종착지)
    mock_notifier.notify_sell.assert_called_once_with(mock_updated_pos)
