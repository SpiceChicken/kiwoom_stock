"""Consolidated normal-cycle safety and capability contracts."""

from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.session import CycleContext, SessionEndReason
from kiwoom_stock.application.ports import (
    MarketDataCollectionError,
    MarketDataFailureKind,
)
from kiwoom_stock.core.database import TradeLogger, TradeLoggerLifecycleError
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.domain.models import PhysicalObservation, Position, PositionStatus
from kiwoom_stock.domain.state import PhysicalStateHydrationSource
from kiwoom_stock.domain.strategy import TargetStopPolicy
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring import engine as engine_module
from kiwoom_stock.monitoring.manager import StockManager
from kiwoom_stock.application.runtime import TradingRuntime


SEOUL = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=SEOUL)
CONTEXT = CycleContext(now=NOW, xkrx_session_date=NOW.date())


class _FakeMarketGateway:
    """Fake external market I/O; all business components remain real."""

    def get_top_trading_value(self, market_tp="001"):
        return [
            {"stk_cd": "B", "stk_nm": "Due"},
            {"stk_cd": "A", "stk_nm": "Active"},
        ]


class _ProviderOnlyFake:
    """Fake only the provider gateway consumed by the real analyzer/collector."""

    def __init__(self, marks, *, fail_code=None):
        self.marks = marks
        self.fail_code = fail_code
        self.calls = []

    def get_top_trading_value(self, market_tp="001"):
        return [
            {"stk_cd": code, "stk_nm": code}
            for code in sorted(self.marks)
        ]

    def get_minute_chart(self, code, tic="1"):
        self.calls.append(("chart", code, tic))
        mark = self.marks[code]
        return [
            {
                "cur_prc": mark,
                "open_pric": mark,
                "high_pric": mark,
                "low_pric": mark,
                "trde_qty": 10,
            }
            for _ in range(15)
        ]

    def get_stock_basic_info(self, code):
        self.calls.append(("basic", code))
        if code == self.fail_code:
            raise RuntimeError(f"provider failed for {code}")
        return {
            "trde_pre": 1,
            "trde_qty": 1_000,
            "cur_prc": self.marks[code],
            "mac": 1_000,
        }

    def get_tick_strength(self, code):
        self.calls.append(("strength", code))
        return [{"cntr_str": 100} for _ in range(5)]

    def get_order_book(self, code):
        raise AssertionError(f"order book should be skipped for neutral input: {code}")


class _RecordingNotifier:
    def __init__(self, events):
        self.events = events

    def start_status_session(self):
        self.events.append("status.start")

    def notify_critical(self, _message):
        self.events.append("kill.notify")

    def notify_buy(self, _data):
        self.events.append("paper.buy")

    def notify_sell(self, _position):
        self.events.append("paper.sell")

    def collect_status(self, _data):
        self.events.append("status.collect")

    def flush_status(self, _regime):
        self.events.append("status.flush")

    def notify_error(self, _message):
        self.events.append("error.notify")


class _PhysicalFakeAnalyzer:
    def __init__(self, tracker, marks, events, *, fail_partial=False):
        self._tracker = tracker
        self._marks = marks
        self._events = events
        self._fail_partial = fail_partial
        self.supply_cache = {}
        self.market_regime = SimpleNamespace(value="neutral")

    def update_priority_supply(self, targets):
        self._events.append(("market.batch", tuple(targets)))
        if self._fail_partial:
            raise RuntimeError("partial market batch")
        observations = tuple(
            PhysicalObservation(
                stock_code=code,
                observed_at=NOW,
                current_price=self._marks[code],
                cumulative_volume=1000.0,
                strength=100.0,
                prev_strength_5m=100.0,
                vwap=self._marks[code],
                atr_percent=0.5,
                vol_ratio=1.0,
                rsi=50.0,
                tot_sel_req=100.0,
                tot_buy_req=100.0,
            )
            for code in targets
        )
        results = self._tracker.process_observations(observations)
        self.supply_cache = {
            code: SupplyData(
                stock_code=code,
                cur_prc=self._marks[code],
                atr_percent=0.5,
                down_atr_percent=0.5,
                forces=results[code]["forces"],
                continuity=results[code]["continuity"],
            )
            for code in targets
        }
        self._events.append("physical.commit")


def _record_open_position(ledger, *, buy_price=100.0):
    return ledger.record_buy(
        {
            "stock_code": "A",
            "stock_name": "Active",
            "buy_price": buy_price,
            "thrust": 0.0,
            "gravity": 0.0,
            "drag": 0.0,
            "magnetic": 0.0,
            "jerk": 0.0,
            "impulse": 0.0,
            "net_force": 0.0,
            "buy_time": NOW.strftime("%Y-%m-%d %H:%M:%S"),
            "buy_regime": "neutral",
            "status": "OPEN",
            "owning_session_date": NOW.date(),
            "state_changed_at": NOW,
        }
    )


def _record_position(
    ledger,
    *,
    stock_code,
    owning_session_date,
    state_changed_at,
    status=PositionStatus.OPEN,
    buy_price=100.0,
):
    position_id = ledger.record_buy(
        {
            "stock_code": stock_code,
            "stock_name": stock_code,
            "buy_price": buy_price,
            "thrust": 0.0,
            "gravity": 0.0,
            "drag": 0.0,
            "magnetic": 0.0,
            "jerk": 0.0,
            "impulse": 0.0,
            "net_force": 0.0,
            "buy_time": state_changed_at.strftime("%Y-%m-%d %H:%M:%S"),
            "buy_regime": "neutral",
            "status": PositionStatus.OPEN,
            "owning_session_date": owning_session_date,
            "state_changed_at": state_changed_at,
        }
    )
    if status is PositionStatus.OVERNIGHT:
        position = Position(**ledger.load_active_positions()[stock_code])
        ledger.mark_position_overnight(position, state_changed_at=state_changed_at)
    return position_id


def _build_real_analyzer_engine(
    tmp_path,
    marks,
    events,
    *,
    fail_code=None,
    second_active=False,
):
    fallback_now = NOW + timedelta(days=20)
    ledger = TradeLogger(tmp_path / "real-pipeline.sqlite3", clock=lambda: fallback_now)
    previous_session = date(2026, 7, 31)
    previous_at = datetime(2026, 7, 31, 15, 20, tzinfo=SEOUL)
    overnight_id = _record_position(
        ledger,
        stock_code="A",
        owning_session_date=previous_session,
        state_changed_at=previous_at,
        status=PositionStatus.OVERNIGHT,
    )
    if second_active:
        _record_position(
            ledger,
            stock_code="C",
            owning_session_date=CONTEXT.xkrx_session_date,
            state_changed_at=NOW - timedelta(minutes=30),
        )
    repository = AsyncPhysicalStateRepository(ledger)
    gateway = _ProviderOnlyFake(marks, fail_code=fail_code)
    engine = TradingEngine(
        SimpleNamespace(market=gateway),
        {
            "fast_interval": 0,
            "slow_interval": 0,
            "max_workers": 1,
            "strategy": {
                "debug_mode": True,
                "cumulative_trade_return_score_floor": -5.0,
            },
            "filters": {"max_stocks": 10},
        },
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=gateway,
        target_stop_policy=TargetStopPolicy(),
        notifier=_RecordingNotifier(events),
        wall_clock=lambda: fallback_now,
    )
    engine.analyzer._clock = lambda: CONTEXT.now
    engine.stock_mgr.stocks = sorted(marks)
    engine.stock_mgr.stock_names.update({code: code for code in marks})
    engine._last_global_update = 100.0

    original_reconcile = engine.stock_mgr.reconcile_overnight_positions
    def reconcile(context=None):
        events.append("overnight.reconcile")
        return original_reconcile(context)
    engine.stock_mgr.reconcile_overnight_positions = reconcile

    original_tracker = engine.state_tracker.process_observations
    def process_observations(observations):
        result = original_tracker(observations)
        events.append(("physical.commit", tuple(result)))
        return result
    engine.state_tracker.process_observations = process_observations

    original_score = engine._check_terminal_status
    def score(context, fresh_marks):
        events.append(("score", dict(fresh_marks)))
        return original_score(context, fresh_marks)
    engine._check_terminal_status = score

    original_decisions = engine._process_decisions
    def decisions(verdicts, context=None):
        events.append("decisions")
        return original_decisions(verdicts, context)
    engine._process_decisions = decisions
    return engine, ledger, repository, gateway, overnight_id


def _build_engine(tmp_path: Path, marks, events, *, floor=-5.0, fail_partial=False):
    ledger = TradeLogger(tmp_path / "trades.sqlite3", clock=lambda: NOW)
    position_id = _record_open_position(ledger)
    repository = AsyncPhysicalStateRepository(ledger)
    gateway = _FakeMarketGateway()
    notifier = _RecordingNotifier(events)
    engine = TradingEngine(
        SimpleNamespace(market=gateway),
        {
            "fast_interval": 0,
            "slow_interval": 0,
            "max_workers": 1,
            "strategy": {
                "debug_mode": True,
                "cumulative_trade_return_score_floor": floor,
            },
            "filters": {"max_stocks": 10},
        },
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=gateway,
        target_stop_policy=TargetStopPolicy(),
        notifier=notifier,
        wall_clock=lambda: NOW,
    )
    engine.stock_mgr.stocks = ["B", "A"]
    engine.stock_mgr.stock_names.update({"A": "Active", "B": "Due"})
    engine.analyzer = _PhysicalFakeAnalyzer(
        engine.state_tracker,
        marks,
        events,
        fail_partial=fail_partial,
    )
    engine._last_global_update = 100.0

    original_reconcile = engine.stock_mgr.reconcile_overnight_positions

    def reconcile(context=None):
        events.append("overnight.reconcile")
        return original_reconcile(context)

    engine.stock_mgr.reconcile_overnight_positions = reconcile
    original_score = engine._check_terminal_status

    def score(context, fresh_marks):
        events.append(("score", dict(fresh_marks)))
        return original_score(context, fresh_marks)

    engine._check_terminal_status = score
    original_decisions = engine._process_decisions

    def decisions(verdicts, context=None):
        events.append("decisions")
        return original_decisions(verdicts, context)

    engine._process_decisions = decisions
    return engine, ledger, position_id


def _trade_row(ledger, position_id):
    return ledger.conn.execute(
        "SELECT status, sell_price, sell_reason FROM trades WHERE id = ?",
        (position_id,),
    ).fetchone()


def test_exact_threshold_uses_restart_fresh_mark_and_kills_before_any_strategy_transition(
    tmp_path,
):
    events = []
    engine, ledger, position_id = _build_engine(
        tmp_path, {"A": 95.0, "B": 101.0}, events
    )
    try:
        assert engine.stock_mgr.active_positions["A"].sell_price is None

        result = engine._run_normal_cycle(CONTEXT, 100.0)

        assert result is not None
        assert result.reason is SessionEndReason.KILL_SWITCH
        assert result.cumulative_trade_return_score == -5.0
        assert events == [
            "overnight.reconcile",
            ("market.batch", ("A", "B")),
            "physical.commit",
            "status.start",
            ("score", {"A": 95.0}),
            "kill.notify",
        ]
        row = _trade_row(ledger, position_id)
        assert tuple(row) == ("OPEN", None, None)
        assert engine.stock_mgr.active_positions["A"].sell_price is None
        assert engine._last_check_time == {}
    finally:
        engine.close()


def test_non_kill_fresh_target_transitions_only_after_score(tmp_path):
    events = []
    engine, ledger, position_id = _build_engine(
        tmp_path, {"A": 103.0, "B": 101.0}, events
    )
    try:
        assert engine._run_normal_cycle(CONTEXT, 100.0) is None

        assert events[:6] == [
            "overnight.reconcile",
            ("market.batch", ("A", "B")),
            "physical.commit",
            "status.start",
            ("score", {"A": 103.0}),
            "decisions",
        ]
        assert events.index("paper.sell") > events.index("decisions")
        row = _trade_row(ledger, position_id)
        assert row[0] == "CLOSED"
        assert row[1] == 103.0
        assert "Fixed Target" in row[2]
        assert "A" not in engine.stock_mgr.active_positions
        assert engine._last_check_time == {"A": 100.0, "B": 100.0}
    finally:
        engine.close()


def test_partial_batch_blocks_score_decisions_and_poll_ack(tmp_path):
    events = []
    engine, ledger, position_id = _build_engine(
        tmp_path,
        {"A": 95.0, "B": 101.0},
        events,
        fail_partial=True,
    )
    try:
        with pytest.raises(RuntimeError, match="partial market batch"):
            engine._run_normal_cycle(CONTEXT, 100.0)

        assert events == [
            "overnight.reconcile",
            ("market.batch", ("A", "B")),
        ]
        assert tuple(_trade_row(ledger, position_id)) == ("OPEN", None, None)
        assert engine._last_check_time == {}
    finally:
        engine.close()


@pytest.mark.parametrize(
    ("mark", "expected_kill"),
    [(95.001, False), (95.0, True), (94.999, True)],
)
def test_fresh_score_threshold_before_exact_and_below(
    tmp_path,
    mark,
    expected_kill,
):
    events = []
    engine, ledger, position_id = _build_engine(
        tmp_path, {"A": mark, "B": 101.0}, events
    )
    try:
        result = engine._check_terminal_status(CONTEXT, {"A": mark})

        assert (result is not None) is expected_kill
        assert tuple(_trade_row(ledger, position_id)) == ("OPEN", None, None)
        assert engine.stock_mgr.active_positions["A"].sell_price is None
    finally:
        engine.close()


def test_no_due_active_reconciles_but_does_not_score_or_ack(tmp_path):
    events = []
    engine, ledger, position_id = _build_engine(
        tmp_path, {"A": 95.0, "B": 101.0}, events
    )
    engine._get_due_targets = lambda: []
    try:
        assert engine._run_normal_cycle(CONTEXT, 100.0) is None
        assert events == ["overnight.reconcile"]
        assert engine._last_check_time == {}
        assert tuple(_trade_row(ledger, position_id)) == ("OPEN", None, None)
    finally:
        engine.close()


def test_closed_session_uses_one_wall_clock_read_and_returns_market_closed(monkeypatch):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._terminal_result = None
    engine._work_closed = False
    engine._closing = False
    engine._closed = False
    reads = []

    def wall_clock():
        reads.append(NOW)
        return NOW

    engine._wall_clock = wall_clock
    monkeypatch.setattr(engine_module, "current_krx_session", lambda _now: None)
    monkeypatch.setattr(
        engine_module.time_mod,
        "time",
        lambda: (_ for _ in ()).throw(AssertionError("scheduler clock was read")),
    )

    assert engine.run().reason is SessionEndReason.MARKET_CLOSED
    assert reads == [NOW]


def test_real_analyzer_reopens_actual_overnight_then_commits_all_active_and_kills(
    tmp_path,
):
    events = []
    engine, ledger, repository, _gateway, overnight_id = _build_real_analyzer_engine(
        tmp_path,
        {"A": 98.0, "B": 101.0, "C": 97.0},
        events,
        second_active=True,
    )
    try:
        assert engine.stock_mgr.active_positions["A"].status is PositionStatus.OVERNIGHT

        result = engine._run_normal_cycle(CONTEXT, 100.0)

        assert result is not None
        assert result.reason is SessionEndReason.KILL_SWITCH
        assert result.cumulative_trade_return_score == -5.0
        assert events == [
            "overnight.reconcile",
            ("physical.commit", ("A", "B", "C")),
            "status.start",
            ("score", {"A": 98.0, "C": 97.0}),
            "kill.notify",
        ]
        row = ledger.conn.execute(
            "SELECT status, owning_session_date, state_changed_at, sell_price "
            "FROM trades WHERE id = ?",
            (overnight_id,),
        ).fetchone()
        assert tuple(row) == (
            "OPEN",
            CONTEXT.xkrx_session_date.isoformat(),
            CONTEXT.now.isoformat(),
            None,
        )
        reopened = engine.stock_mgr.active_positions["A"]
        assert reopened.status is PositionStatus.OPEN
        assert reopened.owning_session_date == CONTEXT.xkrx_session_date
        assert reopened.state_changed_at == CONTEXT.now
        assert reopened.sell_price is None
        for code in ("A", "B", "C"):
            loaded = repository.load_physical_state(code)
            assert loaded.source is PhysicalStateHydrationSource.PERSISTED
            assert loaded.state is not None
            assert loaded.state.last_observed_at == CONTEXT.now
        assert engine._last_check_time == {}
    finally:
        engine.close()


def test_real_analyzer_partial_batch_keeps_reconcile_but_blocks_every_later_step(
    tmp_path,
):
    events = []
    engine, ledger, repository, gateway, overnight_id = _build_real_analyzer_engine(
        tmp_path,
        {"A": 95.0, "B": 101.0},
        events,
        fail_code="B",
    )
    engine.analyzer.supply_cache.update(
        {
            code: SupplyData(stock_code=code, forces={"stale": 1.0})
            for code in ("A", "B")
        }
    )
    try:
        with pytest.raises(MarketDataCollectionError) as raised:
            engine._run_normal_cycle(CONTEXT, 100.0)

        assert raised.value.kind is MarketDataFailureKind.FETCH
        assert raised.value.operation == "stock_basic"

        assert events == ["overnight.reconcile"]
        assert ("basic", "A") in gateway.calls
        assert ("basic", "B") in gateway.calls
        assert repository.load_physical_state("A").state is None
        assert repository.load_physical_state("B").state is None
        assert engine.analyzer.supply_cache["A"].forces == {}
        assert engine.analyzer.supply_cache["B"].forces == {}
        row = ledger.conn.execute(
            "SELECT status, owning_session_date, state_changed_at, sell_price "
            "FROM trades WHERE id = ?",
            (overnight_id,),
        ).fetchone()
        assert tuple(row) == (
            "OPEN",
            CONTEXT.xkrx_session_date.isoformat(),
            CONTEXT.now.isoformat(),
            None,
        )
        assert engine.stock_mgr.active_positions["A"].state_changed_at == CONTEXT.now
        assert engine._last_check_time == {}
    finally:
        engine.close()


def test_real_analyzer_non_kill_fixed_target_commits_after_fresh_score(tmp_path):
    events = []
    engine, ledger, _repository, _gateway, position_id = _build_real_analyzer_engine(
        tmp_path,
        {"A": 103.0, "B": 101.0},
        events,
    )
    try:
        assert engine._run_normal_cycle(CONTEXT, 100.0) is None
        assert events[:4] == [
            "overnight.reconcile",
            ("physical.commit", ("A", "B")),
            "status.start",
            ("score", {"A": 103.0}),
        ]
        assert events.index("decisions") < events.index("paper.sell")
        row = ledger.conn.execute(
            "SELECT status, sell_price, state_changed_at, sell_reason "
            "FROM trades WHERE id = ?",
            (position_id,),
        ).fetchone()
        assert row[0] == "CLOSED"
        assert row[1] == 103.0
        assert row[2] == CONTEXT.now.isoformat()
        assert "Fixed Target" in row[3]
        assert "A" not in engine.stock_mgr.active_positions
    finally:
        engine.close()


def test_context_timestamp_overrides_ledger_fallback_for_all_paper_transitions(tmp_path):
    fallback_now = NOW + timedelta(days=30)
    ledger = TradeLogger(tmp_path / "context-clock.sqlite3", clock=lambda: fallback_now)
    manager = StockManager(
        _FakeMarketGateway(),
        ledger,
        {},
        clock=lambda: fallback_now,
        strict_paper_errors=True,
    )
    manager.stock_names.update({code: code for code in ("BUY", "SELL", "MARK", "REOPEN")})
    try:
        success, _ = manager.apply_paper_buy(
            {"stock_code": "BUY", "price": 100.0, "regime": "neutral", "forces": {}},
            CONTEXT,
        )
        assert success is True
        success, sold = manager.apply_paper_buy(
            {"stock_code": "SELL", "price": 100.0, "regime": "neutral", "forces": {}},
            CONTEXT,
        )
        assert success is True and sold is not None
        success, sold_position = manager.apply_paper_sell(
            {"stock_code": "SELL", "price": 101.0}, "test", CONTEXT
        )
        assert success is True and sold_position is not None
        success, _ = manager.apply_paper_buy(
            {"stock_code": "MARK", "price": 100.0, "regime": "neutral", "forces": {}},
            CONTEXT,
        )
        assert success is True
        marked = manager.apply_paper_mark_overnight(
            manager.active_positions["MARK"], CONTEXT
        )

        previous_at = datetime(2026, 7, 31, 15, 20, tzinfo=SEOUL)
        _record_position(
            ledger,
            stock_code="REOPEN",
            owning_session_date=date(2026, 7, 31),
            state_changed_at=previous_at,
            status=PositionStatus.OVERNIGHT,
        )
        manager.active_positions["REOPEN"] = Position(
            **ledger.load_active_positions()["REOPEN"]
        )
        assert manager.reconcile_overnight_positions(CONTEXT) == 1

        rows = {
            row["stock_code"]: row
            for row in ledger.conn.execute(
                "SELECT stock_code, status, state_changed_at FROM trades "
                "WHERE stock_code IN ('BUY', 'SELL', 'MARK', 'REOPEN')"
            ).fetchall()
        }
        assert {row["state_changed_at"] for row in rows.values()} == {
            CONTEXT.now.isoformat()
        }
        assert rows["BUY"]["status"] == "OPEN"
        assert rows["SELL"]["status"] == "CLOSED"
        assert rows["MARK"]["status"] == "OVERNIGHT"
        assert rows["REOPEN"]["status"] == "OPEN"
        assert manager.active_positions["BUY"].state_changed_at == CONTEXT.now
        assert sold_position.state_changed_at == CONTEXT.now
        assert marked.state_changed_at == CONTEXT.now
        assert manager.active_positions["REOPEN"].state_changed_at == CONTEXT.now
    finally:
        ledger.close()


def test_actual_weekend_stops_before_context_cycle_or_business_mutation(tmp_path):
    sunday = datetime(2026, 8, 9, 10, 0, tzinfo=SEOUL)
    engine, _ledger, _position_id = _build_engine(
        tmp_path, {"A": 95.0, "B": 101.0}, []
    )
    engine._wall_clock = lambda: sunday
    engine._run_normal_cycle = lambda *_args: (_ for _ in ()).throw(
        AssertionError("closed session must not create a business cycle")
    )
    try:
        result = engine.run()
        assert result.reason is SessionEndReason.MARKET_CLOSED
        assert engine._last_check_time == {}
    finally:
        engine.close()


def test_normal_runtime_real_stuck_ledger_worker_respects_shared_budget(tmp_path):
    release = threading.Event()
    entered = threading.Event()
    ledger = TradeLogger(tmp_path / "stuck-worker.sqlite3", clock=lambda: NOW)
    original_persist = ledger._persist_physical_task

    def stuck_persist(task):
        entered.set()
        release.wait(timeout=2.0)
        return original_persist(task)

    ledger._persist_physical_task = stuck_persist
    submit_result = []

    def submit():
        try:
            ledger.submit_physical_state(
                "STUCK",
                {
                    "thrust": 0.0,
                    "gravity": 0.0,
                    "drag": 0.0,
                    "magnetic": 0.0,
                    "jerk": 0.0,
                    "impulse": 0.0,
                    "net_force": 0.0,
                },
            )
        except Exception as error:
            submit_result.append(error)

    submitter = threading.Thread(target=submit, daemon=True)
    try:
        submitter.start()
        assert entered.wait(timeout=1.0), submit_result
        monitor = SimpleNamespace(close=ledger.close)
        runtime = TradingRuntime(
            settings=SimpleNamespace(),
            app_config={},
            output_dir_str=str(tmp_path),
            monitor=monitor,
            _market_owner=SimpleNamespace(close=lambda: None),
            _ledger=ledger,
            _shutdown_budget_seconds=0.05,
        )
        started = time.monotonic()
        with pytest.raises(
            TradeLoggerLifecycleError,
            match="shutdown deadline|did not stop|terminal state",
        ):
            runtime.shutdown_engine()
        assert time.monotonic() - started < 1.0
        assert monitor._stop_event.is_set()
    finally:
        release.set()
        submitter.join(timeout=2.0)
        ledger._worker_thread.join(timeout=2.0)
    assert not ledger._worker_thread.is_alive()
