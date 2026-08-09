"""Phase 2 market-input correctness contracts."""

from datetime import datetime
import inspect
import math
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
import requests

from kiwoom_stock.api.exceptions import KiwoomAPIError
from kiwoom_stock.api.services.market import MarketService
from kiwoom_stock.application import runtime as runtime_module
from kiwoom_stock.application.session import CycleContext
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.domain import indicators as domain_indicators
from kiwoom_stock.domain.indicators import INDICATOR_PERIOD, MIN_INDICATOR_ROWS
from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
    PhysicalStateValidationError,
    PhysicalStateWrite,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    KiwoomMarketDataGatewayAdapter,
    MarketSnapshot,
    ReadOnlyBoundaryError,
    fetch_market_snapshot,
    validate_market_snapshot,
)
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.monitoring import engine as engine_module
from kiwoom_stock.monitoring.collector import (
    MarketDataCollectionError,
    MarketDataCollector,
    MarketDataFailureKind,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.manager import StockManager


SEOUL = ZoneInfo("Asia/Seoul")


@pytest.mark.parametrize(
    ("category", "expected_kind"),
    [
        ("timeout", MarketDataFailureKind.TIMEOUT),
        ("invalid_json", MarketDataFailureKind.PARSE),
        ("invalid_contract", MarketDataFailureKind.MALFORMED),
        ("connection", MarketDataFailureKind.FETCH),
        ("transport", MarketDataFailureKind.FETCH),
        ("http_status", MarketDataFailureKind.FETCH),
        ("api_rejected", MarketDataFailureKind.FETCH),
        ("future_provider_category", MarketDataFailureKind.FETCH),
    ],
)
def test_production_kiwoom_error_categories_cross_typed_collector_boundary(
    category,
    expected_kind,
):
    class FailingMarketService:
        def get_minute_chart(self, _stock_code, _tic):
            raise KiwoomAPIError("provider secret", category=category)

    collector = MarketDataCollector(
        KiwoomMarketDataGatewayAdapter(FailingMarketService())
    )

    with pytest.raises(MarketDataCollectionError) as raised:
        collector.fetch_minute_chart("005930", "5")

    assert raised.value.kind is expected_kind
    assert raised.value.operation == "minute_chart_5m"
    assert isinstance(raised.value.__cause__, KiwoomAPIError)
    assert "provider secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("category", "expected_kind"),
    [
        ("timeout", MarketDataFailureKind.TIMEOUT),
        ("invalid_json", MarketDataFailureKind.PARSE),
        ("invalid_contract", MarketDataFailureKind.MALFORMED),
        ("api_rejected", MarketDataFailureKind.FETCH),
    ],
)
def test_shadow_snapshot_prefetch_uses_same_kiwoom_error_taxonomy(
    category,
    expected_kind,
):
    class FailingMarketService:
        def get_stock_basic_info(self, _stock_code):
            raise KiwoomAPIError("provider secret", category=category)

    client = SimpleNamespace(
        ensure_auth_ready=MagicMock(),
        market=FailingMarketService(),
    )

    with pytest.raises(MarketDataCollectionError) as raised:
        fetch_market_snapshot(client, stock_code="005930", proxy_code="069500")

    assert raised.value.kind is expected_kind
    assert raised.value.operation == "stock_basic"
    client.ensure_auth_ready.assert_called_once_with()


@pytest.mark.parametrize(
    ("category", "expected_kind"),
    [
        ("timeout", MarketDataFailureKind.TIMEOUT),
        ("invalid_json", MarketDataFailureKind.PARSE),
        ("invalid_contract", MarketDataFailureKind.MALFORMED),
        ("rate_limited", MarketDataFailureKind.FETCH),
    ],
)
def test_auth_preflight_is_neutral_and_redacts_provider_context(
    category,
    expected_kind,
    caplog,
):
    secret = "Bearer injected-provider-secret"

    def fail_auth():
        raise KiwoomAPIError(secret, category=category)

    market_service = MagicMock()
    client = SimpleNamespace(market=market_service, ensure_auth_ready=fail_auth)
    adapter = KiwoomMarketDataGatewayAdapter.from_client(client)

    with pytest.raises(MarketDataCollectionError) as raised:
        adapter.preflight()

    error = raised.value
    assert error.kind is expected_kind
    assert error.operation == "auth_preflight"
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in str(error)
    assert secret not in repr(error)
    assert secret not in caplog.text
    market_service.get_stock_basic_info.assert_not_called()


class _ShapeBase:
    def __init__(self, operation: str, shape: str):
        self.operation = operation
        self.shape = shape

    def _list_payload(self, operation, key, valid):
        if operation != self.operation:
            return {key: valid}
        if self.shape == "missing":
            return {}
        if self.shape == "wrong":
            return {key: {}}
        return {key: []}

    def request(self, _endpoint, api_id, payload, *, read_only=False):
        assert read_only is True
        if api_id == "ka10032":
            return self._list_payload(
                "top",
                "trde_prica_upper",
                [{"stk_cd": "005930", "stk_nm": "Samsung"}],
            )
        if api_id == "ka10080":
            operation = "minute" if payload["stk_cd"] == "005930" else "proxy"
            return self._list_payload(
                operation,
                "stk_min_pole_chart_qry",
                list(reversed(_chart())),
            )
        if api_id == "ka10046":
            return self._list_payload(
                "strength",
                "cntr_str_tm",
                [{"cntr_str": value} for value in ("120", "115", "110", "105", "100")],
            )
        if api_id == "ka10001":
            return {
                "trde_pre": "2", "trde_qty": "5000",
                "cur_prc": "114", "mac": "1000",
            }
        if api_id == "ka10004":
            return {"tot_sel_req": "1000", "tot_buy_req": "2000"}
        raise AssertionError(f"unexpected api id: {api_id}")


def _shape_client(operation: str, shape: str):
    return SimpleNamespace(
        market=MarketService(_ShapeBase(operation, shape)),
        ensure_auth_ready=MagicMock(),
    )


@pytest.mark.parametrize("shape", ["missing", "wrong", "empty"])
def test_minute_shape_failure_has_runtime_shadow_parity(shape):
    expected = (
        MarketDataFailureKind.EMPTY
        if shape == "empty"
        else MarketDataFailureKind.MALFORMED
    )
    runtime_client = _shape_client("minute", shape)
    runtime_collector = MarketDataCollector(
        KiwoomMarketDataGatewayAdapter.from_client(runtime_client)
    )
    with pytest.raises(MarketDataCollectionError) as runtime_error:
        runtime_collector.fetch_indicator_chart("005930", "5")

    shadow_client = _shape_client("minute", shape)
    with pytest.raises(MarketDataCollectionError) as shadow_error:
        fetch_market_snapshot(
            shadow_client,
            stock_code="005930",
            proxy_code="069500",
        )

    assert runtime_error.value.kind is shadow_error.value.kind is expected
    assert runtime_error.value.operation == shadow_error.value.operation == "minute_chart_5m"


@pytest.mark.parametrize("shape", ["missing", "wrong", "empty"])
def test_strength_shape_failure_has_runtime_shadow_parity(shape):
    expected = (
        MarketDataFailureKind.EMPTY
        if shape == "empty"
        else MarketDataFailureKind.MALFORMED
    )
    runtime_client = _shape_client("strength", shape)
    runtime_analyzer = MarketAnalyzer(
        KiwoomMarketDataGatewayAdapter.from_client(runtime_client),
        {},
        MagicMock(),
    )
    with pytest.raises(MarketDataCollectionError) as runtime_error:
        runtime_analyzer.update_priority_supply(["005930"])

    shadow_client = _shape_client("strength", shape)
    with pytest.raises(MarketDataCollectionError) as shadow_error:
        fetch_market_snapshot(
            shadow_client,
            stock_code="005930",
            proxy_code="069500",
        )

    assert runtime_error.value.kind is expected
    assert runtime_error.value.operation == "tick_strength"
    assert shadow_error.value.kind is expected
    assert shadow_error.value.operation == "tick_strength"


@pytest.mark.parametrize("shape", ["missing", "wrong", "empty"])
def test_top_discovery_shape_preserves_malformed_vs_present_empty(shape):
    client = _shape_client("top", shape)
    manager = StockManager(
        KiwoomMarketDataGatewayAdapter.from_client(client),
        SimpleNamespace(load_active_positions=lambda: {}),
        {},
    )

    with pytest.raises(MarketDataCollectionError) as raised:
        manager.update_target_stocks()

    assert raised.value.kind is (
        MarketDataFailureKind.EMPTY
        if shape == "empty"
        else MarketDataFailureKind.MALFORMED
    )
    assert raised.value.operation == "top_trading_value"


def test_default_runtime_factory_injects_kiwoom_market_adapter(monkeypatch):
    captured = {}

    def fake_engine(client, config, **kwargs):
        captured.update(client=client, config=config, **kwargs)
        return object()

    monkeypatch.setattr(runtime_module, "TradingEngine", fake_engine)
    client = SimpleNamespace(market=object())
    ledger = object()
    repository = object()
    market_gateway = KiwoomMarketDataGatewayAdapter(client.market)

    runtime_module._default_engine_factory(
        client,
        {"mode": "paper"},
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=market_gateway,
    )

    assert captured["client"] is client
    assert captured["ledger"] is ledger
    assert captured["physical_state_repository"] is repository
    adapter = captured["market_gateway"]
    assert adapter is market_gateway


def _chart(
    *,
    timestamped: bool = False,
    row_count: int = MIN_INDICATOR_ROWS,
) -> list[dict[str, object]]:
    rows = []
    for index in range(row_count):
        close = 100.0 + index + (2.0 if index % 3 == 0 else 0.0)
        row: dict[str, object] = {
            "cur_prc": str(close),
            "open_pric": str(close - 0.5),
            "high_pric": str(close + 2.0),
            "low_pric": str(close - 2.0),
            "trde_qty": f"+{1_000 + index:,}",
        }
        if timestamped:
            row["cntr_tm"] = f"2026080810{index:02d}00"
        rows.append(row)
    return rows


def _snapshot(chart_volume: object) -> MarketSnapshot:
    chart = [
        {
            key: float(str(value).replace(",", ""))
            for key, value in row.items()
        }
        for row in _chart()
    ]
    chart[0]["trde_qty"] = chart_volume
    return MarketSnapshot(
        basic={
            "cur_prc": 100.0,
            "trde_pre": 1.5,
            "trde_qty": 5_000.0,
            "mac": 1_000.0,
        },
        stock_chart=chart,
        proxy_chart=[dict(row) for row in chart],
        strength=[{"cntr_str": 100.0} for _ in range(5)],
        order_book={"tot_sel_req": 1_000.0, "tot_buy_req": 2_000.0},
    )


@pytest.mark.parametrize("volume", [0, 1.0])
def test_market_only_chart_allows_zero_and_positive_volume(volume):
    validate_market_snapshot(_snapshot(volume))


@pytest.mark.parametrize("volume", [-1, math.inf, True])
def test_market_only_chart_rejects_negative_nonfinite_and_nonnumeric_volume(volume):
    with pytest.raises(ReadOnlyBoundaryError, match="trde_qty"):
        validate_market_snapshot(_snapshot(volume))


_DEFAULT_RESULT = object()


class _CollectorGateway:
    def __init__(self, result=_DEFAULT_RESULT, error: Exception | None = None):
        self.result = [] if result is _DEFAULT_RESULT else result
        self.error = error

    def get_minute_chart(self, _stock_code, tic):
        if self.error is not None:
            raise self.error
        return self.result

    def get_stock_basic_info(self, _stock_code):
        if self.error is not None:
            raise self.error
        return self.result


def test_collector_preserves_normal_empty_response():
    collector = MarketDataCollector(_CollectorGateway([]))

    assert collector.fetch_minute_chart("005930", "5") == []


@pytest.mark.parametrize(
    ("gateway", "expected_kind"),
    [
        (_CollectorGateway(error=requests.Timeout("late")), MarketDataFailureKind.TIMEOUT),
        (_CollectorGateway(error=RuntimeError("fetch")), MarketDataFailureKind.FETCH),
        (_CollectorGateway(result=None), MarketDataFailureKind.MALFORMED),
        (
            _CollectorGateway(result=[{"cur_prc": "not-a-number"}]),
            MarketDataFailureKind.MALFORMED,
        ),
        (
            _CollectorGateway(
                result=[
                    {
                        "cur_prc": "nan",
                        "open_pric": "1",
                        "high_pric": "1",
                        "low_pric": "1",
                        "trde_qty": "1",
                    }
                ]
            ),
            MarketDataFailureKind.PARSE,
        ),
    ],
)
def test_collector_distinguishes_timeout_fetch_malformed_and_parse(gateway, expected_kind):
    collector = MarketDataCollector(gateway)

    with pytest.raises(MarketDataCollectionError) as raised:
        collector.fetch_minute_chart("005930", "5")

    assert raised.value.kind is expected_kind


def test_collector_preserves_signed_commified_prices_and_nonnegative_volume():
    rows = _chart()
    rows[0].update(
        cur_prc="-70,000",
        open_pric="+69,500",
        high_pric="+70,500",
        low_pric="-69,000",
        trde_qty="+1,000",
    )
    collector = MarketDataCollector(_CollectorGateway(rows))

    first = collector.fetch_minute_chart("005930", "5")[-1]

    assert first == {
        "cur_prc": -70_000.0,
        "open_pric": 69_500.0,
        "high_pric": 70_500.0,
        "low_pric": -69_000.0,
        "trde_qty": 1_000.0,
    }


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "nondigit",
        "short",
        "long",
        "impossible",
        "integer",
        "partial",
        "mixed_key",
        "multiple_keys",
        "duplicate",
    ],
)
def test_collector_rejects_noncanonical_chart_timestamps(case):
    rows = _chart(timestamped=True)
    if case == "empty":
        rows[0]["cntr_tm"] = ""
    elif case == "nondigit":
        rows[0]["cntr_tm"] = "20260808100A00"
    elif case == "short":
        rows[0]["cntr_tm"] = "2026080810000"
    elif case == "long":
        rows[0]["cntr_tm"] = "202608081000000"
    elif case == "impossible":
        rows[0]["cntr_tm"] = "20260230100000"
    elif case == "integer":
        rows[0]["cntr_tm"] = 20260808100000
    elif case == "partial":
        rows[0].pop("cntr_tm")
    elif case == "mixed_key":
        rows[0]["dt"] = rows[0].pop("cntr_tm")
    elif case == "multiple_keys":
        rows[0]["dt"] = rows[0]["cntr_tm"]
    else:
        rows[1]["cntr_tm"] = rows[0]["cntr_tm"]

    collector = MarketDataCollector(_CollectorGateway(rows))

    with pytest.raises(MarketDataCollectionError) as raised:
        collector.fetch_minute_chart("005930", "5")

    assert raised.value.kind is MarketDataFailureKind.MALFORMED


def test_timestamp_free_kiwoom_chart_is_reversed_once_from_newest_first():
    oldest_first = _chart()
    collector = MarketDataCollector(
        _CollectorGateway(list(reversed(oldest_first)))
    )

    normalized = collector.fetch_minute_chart("005930", "5")

    assert [row["cur_prc"] for row in normalized] == [
        float(row["cur_prc"]) for row in oldest_first
    ]


def test_collector_rejects_negative_chart_volume():
    rows = _chart()
    rows[0]["trde_qty"] = "-1"
    collector = MarketDataCollector(_CollectorGateway(rows))

    with pytest.raises(MarketDataCollectionError) as raised:
        collector.fetch_minute_chart("005930", "5")

    assert raised.value.kind is MarketDataFailureKind.MALFORMED


class _MutableGateway:
    def __init__(self):
        self.chart_result: object = list(reversed(_chart()))
        self.chart_error: Exception | None = None

    def get_minute_chart(self, _stock_code, tic):
        if self.chart_error is not None:
            raise self.chart_error
        return self.chart_result

    def get_stock_basic_info(self, _stock_code):
        return {
            "trde_pre": "+2.0",
            "trde_qty": "+5,000",
            "cur_prc": "-114",
            "mac": "1000",
        }

    def get_tick_strength(self, _stock_code):
        return [{"cntr_str": value} for value in ("120", "115", "110", "105", "100")]

    def get_order_book(self, _stock_code):
        return {"tot_sel_req": "1,000", "tot_buy_req": "2,000"}


class _MemoryPhysicalRepository:
    def __init__(self):
        self.submissions = []

    def load_physical_state(self, _stock_code):
        return PhysicalStateLoadResult(PhysicalStateHydrationSource.INITIAL, None)

    def persist_physical_state(self, state, forces):
        return self.persist_physical_state_batch(
            (PhysicalStateWrite(state, tuple(dict(forces).items())),)
        ).items[0]

    def persist_physical_state_batch(self, writes):
        writes = tuple(writes)
        receipts = tuple(
            PhysicalStateCommitReceipt(
                write.state.stock_code,
                write.state.last_observed_at.isoformat(),
                write.state.updated_at,
            )
            for write in writes
        )
        self.submissions.extend(
            (write.state, dict(write.forces)) for write in writes
        )
        return PhysicalStateBatchCommitReceipt(
            receipts[0].generation, receipts, receipts[0].committed_at
        )

    def close(self):
        pass


@pytest.mark.parametrize(
    ("failure", "expected_kind"),
    [
        ("empty", MarketDataFailureKind.EMPTY),
        ("timeout", MarketDataFailureKind.TIMEOUT),
        ("fetch", MarketDataFailureKind.FETCH),
        ("malformed", MarketDataFailureKind.MALFORMED),
        ("parse", MarketDataFailureKind.PARSE),
    ],
)
def test_failed_supply_cycle_clears_stale_force_without_state_or_db_advance(
    failure,
    expected_kind,
):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=SEOUL)
    gateway = _MutableGateway()
    repository = _MemoryPhysicalRepository()
    tracker = PhysicalStateTracker(repository, clock=lambda: now)
    analyzer = MarketAnalyzer(
        gateway,
        {"proxy_code": "069500"},
        tracker,
        clock=lambda: now,
    )
    analyzer.update_priority_supply(["005930"])
    data = analyzer.supply_cache["005930"]
    committed_state = tracker.current_state("005930")
    assert data.forces
    assert data.continuity is not None
    assert len(repository.submissions) == 1

    direct_error = None
    if failure == "empty":
        gateway.chart_result = []
    elif failure == "timeout":
        direct_error = MarketDataCollectionError(
            MarketDataFailureKind.TIMEOUT,
            "minute_chart_5m",
        )
        gateway.chart_error = direct_error
    elif failure == "fetch":
        gateway.chart_error = RuntimeError("fetch")
    elif failure == "malformed":
        gateway.chart_result = [{"cur_prc": "100"}]
    else:
        bad_chart = _chart()
        bad_chart[0]["trde_qty"] = "nan"
        gateway.chart_result = bad_chart

    with pytest.raises(MarketDataCollectionError) as raised:
        analyzer.update_priority_supply(["005930"])

    assert raised.value.kind is expected_kind
    assert raised.value.operation == "minute_chart_5m"
    if direct_error is not None:
        assert raised.value is direct_error
    assert data.forces == {}
    assert data.continuity is None
    assert tracker.current_state("005930") is committed_state
    assert len(repository.submissions) == 1


def test_late_target_input_failure_prevents_all_tracker_submissions():
    tracker = MagicMock()
    analyzer = MarketAnalyzer(MagicMock(), {}, tracker)
    analyzer.collector = MagicMock()
    normalized_chart = MarketDataCollector(
        _CollectorGateway(list(reversed(_chart())))
    ).fetch_minute_chart("FIRST", "5")
    market_error = MarketDataCollectionError(
        MarketDataFailureKind.EMPTY,
        "minute_chart_5m",
    )
    analyzer.collector.fetch_indicator_chart.side_effect = [
        normalized_chart,
        market_error,
    ]
    analyzer.collector.fetch_stock_basic.return_value = {
        "trde_pre": "2",
        "trde_qty": "5000",
        "cur_prc": "114",
        "mac": "1000",
    }
    analyzer.collector.fetch_tick_strength.return_value = [
        {"cntr_str": "100"}
    ] * 5
    analyzer.collector.fetch_order_book.return_value = {
        "tot_sel_req": "1000",
        "tot_buy_req": "2000",
    }

    with pytest.raises(MarketDataCollectionError) as raised:
        analyzer.update_priority_supply(["FIRST", "SECOND"])

    assert raised.value is market_error
    assert raised.value.kind is MarketDataFailureKind.EMPTY
    assert raised.value.operation == "minute_chart_5m"
    tracker.process_observations.assert_not_called()
    assert "FIRST" not in analyzer.supply_cache
    assert "SECOND" not in analyzer.supply_cache


def test_first_input_failure_does_not_hydrate_or_ack_polling_and_is_retry_due(
    monkeypatch,
):
    tracker = MagicMock()
    analyzer = MarketAnalyzer(_CollectorGateway([]), {}, tracker)
    engine = TradingEngine.__new__(TradingEngine)
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._last_check_time = {}
    engine.stock_mgr = SimpleNamespace(
        stocks=["005930"],
        active_positions={},
    )
    engine.analyzer = analyzer
    engine.notifier = SimpleNamespace(start_status_session=MagicMock())
    engine._checkpoint_shadow_lifecycle = MagicMock()
    engine._assert_open_for_work = MagicMock()
    monkeypatch.setattr(engine_module.time_mod, "time", lambda: 100.0)

    first_due = engine._get_due_targets()
    with pytest.raises(MarketDataCollectionError) as raised:
        engine._prepare_cycle(first_due)
    second_due = engine._get_due_targets()

    assert first_due == second_due == ["005930"]
    assert raised.value.kind is MarketDataFailureKind.EMPTY
    assert raised.value.operation == "minute_chart_5m"
    assert engine._last_check_time == {}
    tracker.load_or_initialize.assert_not_called()
    tracker.process_observations.assert_not_called()
    engine.notifier.start_status_session.assert_not_called()


def test_continuous_supply_market_failure_skips_ack_and_all_decisions(monkeypatch):
    tracker = MagicMock()
    analyzer = MarketAnalyzer(_CollectorGateway([]), {}, tracker)
    engine = TradingEngine.__new__(TradingEngine)
    engine._paper_only = False
    engine._terminal_result = None
    engine._last_global_update = 100.0
    engine._last_check_time = {}
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._assert_open_for_work = MagicMock()
    engine._checkpoint_shadow_lifecycle = MagicMock()
    context = CycleContext(
        datetime(2026, 8, 3, 10, 0, tzinfo=SEOUL),
        datetime(2026, 8, 3).date(),
    )
    engine._create_cycle_context = MagicMock(side_effect=[context, None])
    engine.analyzer = analyzer
    engine.strategy = SimpleNamespace(
        is_monitoring_time=MagicMock(return_value=True),
    )
    engine.stock_mgr = SimpleNamespace(
        stocks=["005930"],
        active_positions={},
        reconcile_overnight_positions=MagicMock(),
    )
    engine.notifier = SimpleNamespace(
        start_status_session=MagicMock(),
        notify_error=MagicMock(),
        flush_status=MagicMock(),
    )
    engine._ack_due_targets = MagicMock()
    engine._evaluate_stocks = MagicMock()
    engine._process_decisions = MagicMock()
    engine._execute_paper_transition = MagicMock()
    engine._execute_order = MagicMock()
    sleeps = []
    monkeypatch.setattr(engine_module.time_mod, "time", lambda: 100.0)
    monkeypatch.setattr(engine_module.time_mod, "sleep", sleeps.append)

    result = engine.run()

    assert result.reason.value == "market_closed"
    assert engine._last_check_time == {}
    tracker.load_or_initialize.assert_not_called()
    tracker.process_observations.assert_not_called()
    engine.notifier.start_status_session.assert_not_called()
    engine._ack_due_targets.assert_not_called()
    engine._evaluate_stocks.assert_not_called()
    engine._process_decisions.assert_not_called()
    engine._execute_paper_transition.assert_not_called()
    engine._execute_order.assert_not_called()
    assert sleeps == [10]


@pytest.mark.parametrize(
    "outcome",
    [
        MarketDataCollectionError(MarketDataFailureKind.TIMEOUT, "top_trading_value"),
        [],
        [{"stk_cd": "005930"}],
    ],
)
def test_target_discovery_failure_preserves_exact_prior_snapshot(outcome):
    class DiscoveryGateway:
        def get_top_trading_value(self, market_tp="001"):
            assert market_tp == "001"
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    manager = StockManager(
        DiscoveryGateway(),
        SimpleNamespace(load_active_positions=lambda: {}),
        {},
    )
    prior_stocks = ["PRIOR"]
    prior_names = {"PRIOR": "Prior Name"}
    manager.stocks = prior_stocks
    manager.stock_names = prior_names

    with pytest.raises(MarketDataCollectionError):
        manager.update_target_stocks()

    assert manager.stocks is prior_stocks
    assert manager.stock_names is prior_names
    assert manager.stocks == ["PRIOR"]
    assert manager.stock_names == {"PRIOR": "Prior Name"}


def test_discovery_failure_backoff_retries_without_global_or_poll_ack(monkeypatch):
    engine = TradingEngine.__new__(TradingEngine)
    engine._paper_only = False
    engine._terminal_result = None
    engine._last_global_update = 0.0
    engine._last_check_time = {}
    engine.fast_interval = 10
    engine.slow_interval = 60
    engine._assert_open_for_work = MagicMock()
    context = CycleContext(
        datetime(2026, 8, 3, 10, 0, tzinfo=SEOUL),
        datetime(2026, 8, 3).date(),
    )
    engine._create_cycle_context = MagicMock(
        side_effect=[context, context, None]
    )
    engine.analyzer = SimpleNamespace(
        update_regime=MagicMock(),
        market_regime=MarketRegime.NEUTRAL,
    )
    engine.strategy = SimpleNamespace(
        update_context=MagicMock(),
        is_monitoring_time=MagicMock(return_value=True),
    )
    failure = MarketDataCollectionError(
        MarketDataFailureKind.TIMEOUT,
        "top_trading_value",
    )
    engine.stock_mgr = SimpleNamespace(
        update_target_stocks=MagicMock(side_effect=[failure, None]),
        reconcile_overnight_positions=MagicMock(),
    )
    engine.notifier = SimpleNamespace(
        notify_error=MagicMock(),
        flush_status=MagicMock(),
    )
    engine._get_due_targets = MagicMock(return_value=[])
    engine._prepare_cycle = MagicMock()
    engine._ack_due_targets = MagicMock()
    engine._evaluate_stocks = MagicMock()
    engine._process_decisions = MagicMock()
    sleeps = []
    monkeypatch.setattr(engine_module.time_mod, "time", lambda: 100.0)
    monkeypatch.setattr(engine_module.time_mod, "sleep", sleeps.append)

    result = engine.run()

    assert result.reason.value == "market_closed"
    assert engine.stock_mgr.update_target_stocks.call_count == 2
    assert engine.analyzer.update_regime.call_count == 2
    assert engine._last_global_update == 100.0
    engine._get_due_targets.assert_called_once_with()
    engine._prepare_cycle.assert_not_called()
    engine._ack_due_targets.assert_not_called()
    engine._evaluate_stocks.assert_not_called()
    engine._process_decisions.assert_not_called()
    engine.notifier.flush_status.assert_not_called()
    assert sleeps == [10, 1]


class _RegimeGateway:
    def __init__(self, rows):
        self.rows = rows
        self.error: Exception | None = None

    def get_minute_chart(self, _stock_code, tic):
        assert tic == "60"
        if self.error is not None:
            raise self.error
        return self.rows


def test_regime_rsi_and_atr_are_invariant_for_ascending_and_descending_chart():
    ascending = _chart(timestamped=True)
    descending = list(reversed(ascending))
    left = MarketAnalyzer(_RegimeGateway(ascending), {}, MagicMock())
    right = MarketAnalyzer(_RegimeGateway(descending), {}, MagicMock())

    left.update_regime()
    right.update_regime()

    assert left.market_rsi == right.market_rsi
    assert tuple(left.market_atr_history) == tuple(right.market_atr_history)
    assert left.market_regime is right.market_regime
    assert [row["cntr_tm"] for row in left.supply_cache["069500"].chart_data] == [
        row["cntr_tm"] for row in ascending
    ]
    assert right.supply_cache["069500"].chart_data == left.supply_cache["069500"].chart_data


def test_true_range_gap_uses_immediately_previous_close():
    chart = [
        {"cur_prc": "100", "high_pric": "101", "low_pric": "99"},
        {"cur_prc": "110", "high_pric": "112", "low_pric": "109"},
    ]

    assert MarketAnalyzer._calculate_true_ranges(chart) == [12.0]


@pytest.mark.parametrize(
    ("replacement", "expected_kind"),
    [
        ([], MarketDataFailureKind.EMPTY),
        ([{"cur_prc": "100"}], MarketDataFailureKind.MALFORMED),
    ],
)
def test_regime_failure_clears_stale_regime_and_chart_without_advancing_atr(
    replacement,
    expected_kind,
):
    gateway = _RegimeGateway(_chart(timestamped=True))
    analyzer = MarketAnalyzer(gateway, {}, MagicMock())
    analyzer.update_regime()
    prior_history = tuple(analyzer.market_atr_history)
    assert analyzer.market_regime is not MarketRegime.UNKNOWN

    gateway.rows = replacement
    with pytest.raises(MarketDataCollectionError) as raised:
        analyzer.update_regime()

    assert raised.value.kind is expected_kind
    assert analyzer.market_regime is MarketRegime.UNKNOWN
    assert analyzer.supply_cache["069500"].chart_data == []
    assert tuple(analyzer.market_atr_history) == prior_history


def test_regime_fetch_failure_is_terminal_before_shadow_decision_boundaries():
    gateway = _RegimeGateway(_chart(timestamped=True))
    gateway.error = requests.Timeout("late")
    analyzer = MarketAnalyzer(gateway, {}, MagicMock())
    engine = TradingEngine.__new__(TradingEngine)
    engine._paper_only = True
    engine._shadow_cycle_lock = threading.Lock()
    engine._shadow_cycle_state = "not-started"
    engine.analyzer = analyzer
    engine.strategy = SimpleNamespace(update_context=MagicMock())
    engine.stock_mgr = SimpleNamespace(stocks=[], stock_names={})
    engine._evaluate_stocks = MagicMock()
    engine._process_decisions = MagicMock()
    engine._execute_paper_transition = MagicMock()
    engine._execute_order = MagicMock()

    with pytest.raises(MarketDataCollectionError) as raised:
        engine.run_shadow_cycle("005930")

    assert raised.value.kind is MarketDataFailureKind.TIMEOUT
    assert analyzer.market_regime is MarketRegime.UNKNOWN
    engine.strategy.update_context.assert_not_called()
    engine._evaluate_stocks.assert_not_called()
    engine._process_decisions.assert_not_called()
    engine._execute_paper_transition.assert_not_called()
    engine._execute_order.assert_not_called()


@pytest.mark.parametrize(
    "row_count",
    [MIN_INDICATOR_ROWS - 1, MIN_INDICATOR_ROWS],
)
def test_period_14_chart_minimum_has_runtime_supply_regime_shadow_parity(row_count):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=SEOUL)
    gateway = _MutableGateway()
    gateway.chart_result = list(reversed(_chart(row_count=row_count)))
    repository = _MemoryPhysicalRepository()
    tracker = PhysicalStateTracker(repository, clock=lambda: now)
    supply_analyzer = MarketAnalyzer(gateway, {}, tracker, clock=lambda: now)
    regime_analyzer = MarketAnalyzer(gateway, {}, MagicMock())
    client = SimpleNamespace(
        market=gateway,
        ensure_auth_ready=MagicMock(),
    )

    if row_count == MIN_INDICATOR_ROWS - 1:
        with pytest.raises(MarketDataCollectionError) as supply_error:
            supply_analyzer.update_priority_supply(["005930"])
        assert supply_error.value.kind is MarketDataFailureKind.MALFORMED
        with pytest.raises(MarketDataCollectionError) as regime_error:
            regime_analyzer.update_regime()
        assert regime_error.value.kind is MarketDataFailureKind.MALFORMED
        with pytest.raises(MarketDataCollectionError) as shadow_error:
            fetch_market_snapshot(
                client,
                stock_code="005930",
                proxy_code="069500",
            )
        assert shadow_error.value.kind is MarketDataFailureKind.MALFORMED
        assert tracker.current_state("005930") is None
        assert repository.submissions == []
    else:
        supply_analyzer.update_priority_supply(["005930"])
        regime_analyzer.update_regime()
        snapshot = fetch_market_snapshot(
            client,
            stock_code="005930",
            proxy_code="069500",
        )
        assert tracker.current_state("005930") is not None
        assert (
            len(snapshot.stock_chart)
            == len(snapshot.proxy_chart)
            == MIN_INDICATOR_ROWS
        )


def test_indicator_period_and_minimum_have_one_domain_owner(monkeypatch):
    assert MIN_INDICATOR_ROWS == INDICATOR_PERIOD + 1
    for calculation in (
        domain_indicators.calculate_rsi,
        domain_indicators.calculate_atr,
        domain_indicators.calculate_atr_percent,
    ):
        assert (
            inspect.signature(calculation).parameters["period"].default
            == INDICATOR_PERIOD
        )

    periods = []

    def capture_atr_percent(*_args, period, **_kwargs):
        periods.append(period)
        return 1.0

    monkeypatch.setattr(
        "kiwoom_stock.monitoring.analyzer.ind.calculate_atr_percent",
        capture_atr_percent,
    )
    analyzer = MarketAnalyzer(MagicMock(), {}, MagicMock())
    data = SimpleNamespace(cur_prc=100.0)
    normalized = MarketDataCollector(
        _CollectorGateway(list(reversed(_chart())))
    ).fetch_indicator_chart("005930", "5")

    analyzer._update_volatility_data(data, normalized)

    assert periods == [INDICATOR_PERIOD, INDICATOR_PERIOD]
    assert data.atr_percent == data.down_atr_percent == 1.0


def test_monitoring_layer_has_no_concrete_persistence_import_or_fallback():
    monitoring_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "kiwoom_stock"
        / "monitoring"
    )
    forbidden = (
        "kiwoom_stock.core.database",
        "kiwoom_stock.infrastructure.physical_state_repository",
    )
    for source_path in monitoring_root.glob("*.py"):
        source = source_path.read_text(encoding="utf-8")
        assert all(name not in source for name in forbidden), source_path

    signature = inspect.signature(TradingEngine)
    for name in ("ledger", "physical_state_repository", "market_gateway"):
        assert signature.parameters[name].default is inspect.Parameter.empty
