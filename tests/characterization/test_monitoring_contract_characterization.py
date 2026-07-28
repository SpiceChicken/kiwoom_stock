"""Characterize monitoring cadence, target selection, and the shallow data pipeline."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from kiwoom_stock.core import state_manager as state_manager_module
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.monitoring import analyzer as analyzer_module
from kiwoom_stock.monitoring import engine as engine_module
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.monitoring.collector import MarketDataCollector
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.manager import Position, StockManager


def _frozen_datetime(value):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return value
            return value.replace(tzinfo=tz)

    return FrozenDateTime


def test_fast_and_slow_polling_boundaries_use_position_or_strength(monkeypatch):
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._last_check_time = {
        "HELD_DUE": 90.0,
        "HELD_WAIT": 90.0001,
        "STRONG_DUE": 90.0,
        "SLOW_DUE": 40.0,
        "SLOW_WAIT": 40.0001,
    }
    engine.stock_mgr = SimpleNamespace(
        stocks=["HELD_DUE", "HELD_WAIT", "STRONG_DUE", "SLOW_DUE", "SLOW_WAIT"],
        active_positions={"HELD_DUE": object(), "HELD_WAIT": object()},
    )
    engine.analyzer = SimpleNamespace(
        supply_cache={
            "STRONG_DUE": SupplyData(stock_code="STRONG_DUE", strength=100.0),
            "SLOW_DUE": SupplyData(stock_code="SLOW_DUE", strength=99.9999),
            "SLOW_WAIT": SupplyData(stock_code="SLOW_WAIT", strength=0.0),
        }
    )
    monkeypatch.setattr(engine_module.time_mod, "time", lambda: 100.0)

    assert engine._get_due_targets() == ["HELD_DUE", "STRONG_DUE", "SLOW_DUE"]
    assert engine._last_check_time == {
        "HELD_DUE": 100.0,
        "HELD_WAIT": 90.0001,
        "STRONG_DUE": 100.0,
        "SLOW_DUE": 100.0,
        "SLOW_WAIT": 40.0001,
    }


@pytest.mark.parametrize(
    ("now", "expected_events", "expected_last_update"),
    [
        (159.9999, [], 100.0),
        (160.0, ["regime", "context", "targets"], 160.0),
    ],
)
def test_global_regime_and_target_refresh_boundary(monkeypatch, now, expected_events, expected_last_update):
    events = []

    class Analyzer:
        market_regime = MarketRegime.STABLE_BULL

        def update_regime(self):
            events.append("regime")

    class Strategy:
        def update_context(self, regime):
            assert regime is MarketRegime.STABLE_BULL
            events.append("context")

    class Manager:
        def update_target_stocks(self):
            events.append("targets")

    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._last_global_update = 100.0
    engine.analyzer = Analyzer()
    engine.strategy = Strategy()
    engine.stock_mgr = Manager()
    engine._check_system_status = Mock(return_value=True)
    engine._get_due_targets = Mock(return_value=[])
    monkeypatch.setattr(engine_module.time_mod, "time", lambda: now)
    monkeypatch.setattr(engine_module.time_mod, "sleep", Mock(side_effect=KeyboardInterrupt))

    engine.run()

    assert events == expected_events
    assert engine._last_global_update == expected_last_update


def test_target_selection_excludes_etfs_caps_ranked_list_and_keeps_active_positions():
    upper = [
        {"stk_cd": "A", "stk_nm": "Alpha"},
        {"stk_cd": "ETF", "stk_nm": "KODEX ETF"},
        {"stk_cd": "A", "stk_nm": "Alpha duplicate"},
        {"stk_cd": "B", "stk_nm": "Beta"},
        {"stk_cd": "C", "stk_nm": "Gamma"},
    ]
    market = SimpleNamespace(get_top_trading_value=Mock(return_value=upper))
    database = SimpleNamespace(load_open_positions=lambda: {})
    manager = StockManager(
        market,
        database,
        filter_config={"etf_keywords": ["ETF"], "max_stocks": 2},
    )
    manager.active_positions = {
        "HELD": Position(
            id=1,
            stock_code="HELD",
            stock_name="Held position",
            buy_price=100.0,
            buy_time="2026-07-17 10:00:00",
            buy_regime="STABLE_BULL",
        )
    }

    manager.update_target_stocks()

    assert manager.stocks == ["A", "B", "HELD"]
    assert manager.stock_names == {"A": "Alpha", "B": "Beta", "HELD": "Held position"}
    market.get_top_trading_value.assert_called_once_with(market_tp="001")


@pytest.mark.parametrize(
    ("rsi", "prior_atr", "expected"),
    [
        (60.0, [], MarketRegime.STABLE_BULL),
        (60.0, [10.0, 10.0, 10.0, 10.0], MarketRegime.VOLATILE_BULL),
        (40.0, [], MarketRegime.QUIET_BEAR),
        (50.0, [], MarketRegime.NEUTRAL),
    ],
)
def test_market_regime_rsi_boundaries_and_default_proxy(monkeypatch, rsi, prior_atr, expected):
    now = datetime(2026, 7, 17, 10, 0, 0)
    monkeypatch.setattr(analyzer_module, "datetime", _frozen_datetime(now))
    analyzer = MarketAnalyzer(SimpleNamespace(), {}, MagicMock())
    assert analyzer.market_proxy_code == "069500"
    analyzer.market_atr_history.extend(prior_atr)
    analyzer.supply_cache["069500"].chart_data = [
        {"cur_prc": "100", "high_pric": "120", "low_pric": "80"},
        {"cur_prc": "100", "high_pric": "120", "low_pric": "80"},
    ]
    monkeypatch.setattr(analyzer, "_update_chart_data", lambda _code, _tic: None)
    monkeypatch.setattr(analyzer_module.ind, "calculate_rsi", lambda _closes, period: rsi)

    analyzer.update_regime()

    assert analyzer.market_regime is expected


class _FailingMarket:
    def __getattr__(self, _name):
        def fail(*_args, **_kwargs):
            raise RuntimeError("characterized upstream failure")

        return fail


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (lambda collector: collector.fetch_stock_basic("A"), {}),
        (lambda collector: collector.fetch_tick_strength("A"), []),
        (lambda collector: collector.fetch_minute_chart("A"), []),
        (lambda collector: collector.fetch_program_trade(), {}),
        (lambda collector: collector.fetch_foreign_window_trade(), {}),
        (lambda collector: collector.fetch_order_book("A"), {}),
        (lambda collector: collector.fetch_recent_ticks("A"), []),
    ],
)
def test_collector_current_error_to_empty_contract_is_explicit(operation, expected):
    collector = MarketDataCollector(_FailingMarket())

    assert operation(collector) == expected


class _NoDatabase:
    def __init__(self):
        self.submissions = []

    def get_last_physical_state(self, _stock_code):
        return None

    def submit_physical_state(self, stock_code, forces):
        self.submissions.append((stock_code, dict(forces)))


class _FakeMarketGateway:
    def __init__(self):
        self.calls = []

    def get_minute_chart(self, stock_code, tic):
        self.calls.append(("minute", stock_code, tic))
        return [
            {
                "cur_prc": str(80_000 + index),
                "open_pric": str(79_900 + index),
                "high_pric": str(80_100 + index),
                "low_pric": str(79_800 + index),
                "trde_qty": str(100 + index),
            }
            for index in range(15)
        ]

    def get_stock_basic_info(self, stock_code):
        self.calls.append(("basic", stock_code))
        return {"trde_pre": "2.0", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000"}

    def get_tick_strength(self, stock_code):
        self.calls.append(("strength", stock_code))
        return [{"cntr_str": str(value)} for value in (120, 115, 110, 105, 100)]

    def get_order_book(self, stock_code):
        self.calls.append(("order_book", stock_code))
        return {"tot_sel_req": "50000", "tot_buy_req": "5000"}


def test_shallow_market_pipeline_runs_collector_indicators_state_and_physics(monkeypatch):
    now = datetime(2026, 7, 17, 10, 0, 0)
    monkeypatch.setattr(analyzer_module, "datetime", _frozen_datetime(now))
    monkeypatch.setattr(state_manager_module, "datetime", _frozen_datetime(now))
    repository = _NoDatabase()
    state_tracker = PhysicalStateTracker(repository)
    gateway = _FakeMarketGateway()
    analyzer = MarketAnalyzer(gateway, {"proxy_code": "069500"}, state_tracker)

    analyzer.update_priority_supply(["005930"])

    data = analyzer.supply_cache["005930"]
    assert data.cur_prc == 80_500.0
    assert data.strength == 120.0
    assert data.mac == 1000.0
    assert data.forces["current_velocity"] == -0.5938
    assert data.forces["magnetic"] > 0.0
    assert [call[0] for call in gateway.calls] == ["minute", "basic", "strength", "order_book"]
    assert len(repository.submissions) == 1
