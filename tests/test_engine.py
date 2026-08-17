from types import SimpleNamespace
import inspect
import threading
from unittest.mock import MagicMock
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.session import (
    CriticalNotificationOutcome,
    CycleContext,
    SessionEndReason,
    TradingSessionResult,
)
from kiwoom_stock.monitoring import engine as engine_module
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.monitoring.engine import (
    TradingEngine,
)
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.domain.models import (
    PhysicalContinuityEvidence,
    PositionDecision,
    PositionDecisionResult,
    PositionStatus,
)
from kiwoom_stock.domain.state import PhysicalStateValidationError


class _RiskStrategy:
    def __init__(self, *, monitoring=True, score_floor=-5.0):
        self.monitoring = monitoring
        self.cumulative_trade_return_score_floor = score_floor
        self.monitoring_checks = 0
        self.kill_checks = []

    def is_monitoring_time(self, now=None):
        self.monitoring_checks += 1
        return self.monitoring

    def is_kill_switch_activated(self, cumulative_trade_return_score):
        self.kill_checks.append(cumulative_trade_return_score)
        return (
            cumulative_trade_return_score
            <= self.cumulative_trade_return_score_floor
        )


class _RiskDatabase:
    def __init__(self, realized_score=-2.0):
        self.realized_score = realized_score
        self.reads = 0
        self.session_dates = []

    def get_cumulative_realized_trade_return_score(self, session_date):
        self.reads += 1
        self.session_dates.append(session_date)
        return self.realized_score

    def __getattr__(self, name):
        raise AssertionError(f"unexpected DB access: {name}")


class _RiskManager:
    def __init__(self, *, cumulative_score, active_positions):
        self.cumulative_score = cumulative_score
        self.active_positions = active_positions
        self.realized_inputs = []

    def calculate_fresh_cumulative_trade_return_score(self, realized_score, fresh_marks):
        self.realized_inputs.append(realized_score)
        return self.cumulative_score

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

    def manager_factory(candidate_market, candidate_ledger, filters, **kwargs):
        assert candidate_market is market
        assert candidate_ledger is expected_ledger
        assert filters == {"max_stocks": 50}
        assert kwargs == {"clock": engine_module.seoul_now}
        events.append("manager")
        return manager

    monkeypatch.setattr(engine_module, "PhysicalStateTracker", state_tracker_factory)
    monkeypatch.setattr(engine_module, "MarketAnalyzer", analyzer_factory)
    monkeypatch.setattr(
        engine_module,
        "TradingStrategy",
        lambda config, **kwargs: events.append(("strategy", config, kwargs))
        or object(),
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
        market_gateway=client.market,
    )

    assert engine.db is ledger
    assert engine.physical_state_repository is physical_repository
    assert engine.executor is executor
    assert events == [
        "state_tracker",
        "analyzer",
        (
            "strategy",
            {"debug_mode": False},
            {"clock": engine_module.seoul_now},
        ),
        "manager",
        ("notifier", {}, config),
        ("executor", {"max_workers": 3}),
    ]


@pytest.mark.parametrize(
    "omitted",
    ["ledger", "physical_state_repository", "market_gateway"],
)
def test_engine_requires_all_composition_root_dependencies(omitted):
    dependencies = {
        "ledger": object(),
        "physical_state_repository": object(),
        "market_gateway": object(),
    }
    dependencies.pop(omitted)

    with pytest.raises(TypeError, match=omitted):
        TradingEngine(SimpleNamespace(market=object()), {}, **dependencies)


def test_shadow_validation_failure_skips_evaluation_and_all_decisions():
    engine = TradingEngine.__new__(TradingEngine)
    engine._paper_only = True
    engine._shadow_cycle_lock = threading.Lock()
    engine._shadow_cycle_state = "not-started"
    engine.analyzer = SimpleNamespace(
        update_regime=MagicMock(),
        market_regime=MarketRegime.NEUTRAL,
        update_priority_supply=MagicMock(
            side_effect=PhysicalStateValidationError("invalid observation")
        ),
        supply_cache={},
    )
    engine.strategy = SimpleNamespace(update_context=MagicMock())
    engine.stock_mgr = SimpleNamespace(stocks=[], stock_names={})
    engine.state_tracker = SimpleNamespace(load_or_initialize=MagicMock())
    engine.notifier = SimpleNamespace(start_status_session=MagicMock())
    engine._evaluate_stocks = MagicMock()
    engine._process_decisions = MagicMock()
    engine._execute_paper_transition = MagicMock()
    engine._execute_order = MagicMock()

    with pytest.raises(PhysicalStateValidationError, match="invalid observation"):
        engine.run_shadow_cycle("005930")

    engine._evaluate_stocks.assert_not_called()
    engine._process_decisions.assert_not_called()
    engine._execute_paper_transition.assert_not_called()
    engine._execute_order.assert_not_called()


def test_real_analyzer_missing_chart_key_is_terminal_before_all_decisions():
    code = "005930"
    tracker = MagicMock()
    analyzer = MarketAnalyzer(
        MagicMock(),
        {"proxy_code": "069500"},
        tracker,
    )
    analyzer.collector = MagicMock()
    analyzer.collector.fetch_indicator_chart.side_effect = (
        lambda *args, **kwargs: analyzer.collector.fetch_minute_chart(
            *args, **kwargs
        )
    )
    valid_row = {
        "cur_prc": "80000", "open_pric": "80000",
        "high_pric": "80000", "low_pric": "80000", "trde_qty": "10",
    }
    analyzer.collector.fetch_minute_chart.return_value = [
        valid_row.copy() for _ in range(15)
    ]
    analyzer.collector.fetch_stock_basic.return_value = {
        "trde_pre": "2", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000",
    }
    analyzer.collector.fetch_tick_strength.return_value = [
        {"cntr_str": "100"}
    ] * 5
    analyzer.collector.fetch_order_book.return_value = {
        "tot_sel_req": 1, "tot_buy_req": 1,
    }
    analyzer.state_tracker.process_observations.return_value = {
        code: {
            "forces": {"current_velocity": 1.0},
            "continuity": PhysicalContinuityEvidence(1, "initial", None, 0),
        }
    }
    analyzer.update_priority_supply([code])
    stale = analyzer.supply_cache[code]
    assert stale.forces == {"current_velocity": 1.0}
    tracker_calls = analyzer.state_tracker.process_observations.call_count

    analyzer.collector.fetch_minute_chart.return_value = [
        {key: value for key, value in valid_row.items() if key != "cur_prc"}
        for _ in range(15)
    ]
    analyzer.update_regime = MagicMock()
    analyzer.market_regime = MarketRegime.NEUTRAL
    engine = TradingEngine.__new__(TradingEngine)
    engine._paper_only = True
    engine._shadow_cycle_lock = threading.Lock()
    engine._shadow_cycle_state = "not-started"
    engine.analyzer = analyzer
    engine.strategy = SimpleNamespace(update_context=MagicMock())
    engine.stock_mgr = SimpleNamespace(stocks=[], stock_names={})
    engine.state_tracker = tracker
    engine.notifier = SimpleNamespace(start_status_session=MagicMock())
    engine._evaluate_stocks = MagicMock()
    engine._process_decisions = MagicMock()
    engine._execute_paper_transition = MagicMock()
    engine._execute_order = MagicMock()

    with pytest.raises(PhysicalStateValidationError, match="KeyError"):
        engine.run_shadow_cycle(code)

    assert stale.forces == {}
    assert stale.continuity is None
    assert analyzer.state_tracker.process_observations.call_count == tracker_calls
    assert engine._shadow_cycle_state == "terminal"
    engine._evaluate_stocks.assert_not_called()
    engine._process_decisions.assert_not_called()
    engine._execute_paper_transition.assert_not_called()
    engine._execute_order.assert_not_called()


def _risk_engine(
    *,
    cumulative_score=-5.0,
    active_positions=None,
    critical_error=None,
    monitoring=True,
):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None
    engine._wall_clock = lambda: datetime(
        2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")
    )
    engine.strategy = _RiskStrategy(monitoring=monitoring)
    engine.db = _RiskDatabase()
    engine.stock_mgr = _RiskManager(
        cumulative_score=cumulative_score,
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


def _cycle_context():
    return CycleContext(
        now=datetime(2026, 8, 3, 10, 0, tzinfo=ZoneInfo("Asia/Seoul")),
        xkrx_session_date=datetime(2026, 8, 3).date(),
    )


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
        cumulative_trade_return_score=-5.0,
        cumulative_trade_return_score_floor=-5.0,
        critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
    )

    assert result.requires_attention is True
    assert result.post_market_allowed is False
    assert result.exit_code == 1
    assert result.unresolved_position_codes == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"cumulative_trade_return_score": None},
        {"cumulative_trade_return_score": -4.9999},
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
        "cumulative_trade_return_score": -5.0,
        "cumulative_trade_return_score_floor": -5.0,
        "critical_notification_outcome": CriticalNotificationOutcome.CALL_RETURNED,
    }
    values.update(overrides)

    with pytest.raises((TypeError, ValueError)):
        TradingSessionResult(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cumulative_trade_return_score", float("nan")),
        ("cumulative_trade_return_score", float("inf")),
        ("cumulative_trade_return_score", float("-inf")),
        ("cumulative_trade_return_score", True),
        ("cumulative_trade_return_score_floor", float("nan")),
        ("cumulative_trade_return_score_floor", float("inf")),
        ("cumulative_trade_return_score_floor", float("-inf")),
        ("cumulative_trade_return_score_floor", False),
    ],
)
def test_kill_session_result_rejects_non_finite_or_boolean_score(field, value):
    values = {
        "reason": SessionEndReason.KILL_SWITCH,
        "cumulative_trade_return_score": -5.0,
        "cumulative_trade_return_score_floor": -5.0,
        "critical_notification_outcome": CriticalNotificationOutcome.CALL_RETURNED,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        TradingSessionResult(**values)


def test_kill_session_result_rejects_positive_floor_and_old_keyword_names():
    with pytest.raises(ValueError, match="floor"):
        TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            cumulative_trade_return_score=-5.0,
            cumulative_trade_return_score_floor=0.1,
            critical_notification_outcome=(
                CriticalNotificationOutcome.CALL_RETURNED
            ),
        )

    with pytest.raises(TypeError):
        TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            total_pnl=-5.0,
            loss_limit=-5.0,
        )


def test_normal_session_result_rejects_kill_metadata():
    with pytest.raises(ValueError):
        TradingSessionResult(
            reason=SessionEndReason.MARKET_CLOSED,
            cumulative_trade_return_score=-5.0,
        )


def test_closed_engine_keeps_latched_kill_result_read_only():
    engine, events = _lifecycle_engine()
    terminal_result = TradingSessionResult(
        reason=SessionEndReason.KILL_SWITCH,
        cumulative_trade_return_score=-5.0,
        cumulative_trade_return_score_floor=-5.0,
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
    engine = _risk_engine(cumulative_score=-5.0, active_positions=active_positions)
    before_mapping = dict(active_positions)
    first_result = engine._check_terminal_status(
        _cycle_context(), {"A": 1.0, "B": 1.0}
    )
    _forbid_engine_clock(monkeypatch)
    second_result = engine.run()

    assert first_result is second_result
    assert first_result.reason is SessionEndReason.KILL_SWITCH
    assert first_result.cumulative_trade_return_score == -5.0
    assert first_result.cumulative_trade_return_score_floor == -5.0
    assert first_result.unresolved_position_codes == ("A", "B")
    assert first_result.critical_notification_outcome is CriticalNotificationOutcome.CALL_RETURNED
    assert active_positions == before_mapping
    assert active_positions["A"] is first_position
    assert active_positions["B"] is second_position
    assert engine.strategy.monitoring_checks == 0
    assert engine.strategy.kill_checks == [-5.0]
    assert engine.db.reads == 1
    assert engine.db.session_dates == [datetime(2026, 8, 3).date()]
    assert engine.stock_mgr.realized_inputs == [-2.0]
    assert engine.notifier.error_messages == []
    assert engine.notifier.critical_messages == [
        "🚨 KILL-SWITCH ACTIVATED — Cumulative trade return score: -5.0 "
        "percentage-points; floor: -5.0 percentage-points. "
        "No automatic liquidation was attempted. Unresolved active positions: 2."
    ]


def test_canonical_kill_switch_source_uses_only_score_semantics():
    canonical_source = "\n".join(
        (
            inspect.getsource(TradingEngine._check_terminal_status),
            inspect.getsource(TradingEngine._create_kill_switch_result),
            inspect.getsource(TradingSessionResult),
        )
    ).lower()

    for forbidden in (
        "pnl",
        "portfolio",
        "profit/loss",
        "profit-loss",
        "total_loss_limit",
        "손익",
        "손실",
    ):
        assert forbidden not in canonical_source


def test_kill_switch_with_no_active_positions_is_still_terminal(monkeypatch):
    engine = _risk_engine(cumulative_score=-6.0)
    result = engine._check_terminal_status(_cycle_context(), {})

    assert result.reason is SessionEndReason.KILL_SWITCH
    assert result.unresolved_position_codes == ()
    assert result.requires_attention is True
    assert len(engine.notifier.critical_messages) == 1


def test_loss_just_above_limit_keeps_monitoring_without_latching():
    engine = _risk_engine(cumulative_score=-4.9999)

    assert engine._check_terminal_status(_cycle_context(), {}) is None
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
    engine = _risk_engine(cumulative_score=-5.0, critical_error=error)
    first_result = engine._check_terminal_status(_cycle_context(), {})
    _forbid_engine_clock(monkeypatch)
    second_result = engine.run()

    assert first_result is second_result
    assert first_result.reason is SessionEndReason.KILL_SWITCH
    assert first_result.critical_notification_outcome is CriticalNotificationOutcome.CALL_RAISED
    assert first_result.critical_notification_error_type == expected_type
    assert len(engine.notifier.critical_messages) == 1
    assert engine.notifier.error_messages == []
    assert engine.db.reads == 1


def test_critical_notifier_system_exit_is_not_swallowed(monkeypatch):
    engine = _risk_engine(cumulative_score=-5.0, critical_error=SystemExit(7))

    with pytest.raises(SystemExit) as caught:
        engine._check_terminal_status(_cycle_context(), {})

    assert caught.value.code == 7
    assert engine._terminal_result is None
    assert len(engine.notifier.critical_messages) == 1
    assert engine.notifier.error_messages == []


def test_market_close_returns_normal_typed_result_without_pnl_or_time_access(monkeypatch):
    engine = _risk_engine(monitoring=False)
    result = engine._check_monitoring_status(_cycle_context())

    assert result is False
    assert engine.strategy.monitoring_checks == 1
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

    engine._create_cycle_context = interrupted_check

    assert engine.run() == TradingSessionResult(reason=SessionEndReason.USER_INTERRUPT)


def test_pre_threshold_exception_retains_error_notification_and_retry(monkeypatch):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None
    engine.notifier = _RiskNotifier()
    calls = 0
    sleeps = []

    def context_check():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("risk data unavailable")
        raise KeyboardInterrupt

    engine._create_cycle_context = context_check
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
    mock_updated_pos.status = PositionStatus.OPEN
    mock_manager.active_positions = {"005930": mock_updated_pos}
    mock_manager.update_position_data.return_value = mock_updated_pos
    
    # 언패킹 폭발 방지
    mock_manager.apply_paper_sell.return_value = (True, mock_updated_pos)
    mock_manager.apply_paper_buy.return_value = (True, mock_updated_pos)
    
    mock_strategy.decide_position.return_value = PositionDecisionResult(
        PositionDecision.SELL,
        "Kinetic Exit (Velocity Drop)",
    )
    
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
    mock_manager.reconcile_overnight_positions.assert_called_once_with()
    mock_strategy.decide_position.assert_called_once_with(
        mock_updated_pos,
        80000,
        {"net_force": -3.5, "current_velocity": 7.0},
    )
    
    # Assert 3: Manager 매도 집행
    mock_manager.apply_paper_sell.assert_called_once_with(
        verdict, "Kinetic Exit (Velocity Drop)", None
    )
    
    # Assert 4: 💥 Notifier에 알림 발송이 정상적으로 지시되었는지까지 검증 (파이프라인의 종착지)
    mock_notifier.notify_sell.assert_called_once_with(mock_updated_pos)
