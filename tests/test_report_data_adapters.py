"""Offline contracts for the reporting data, minute, and CSV adapters."""

from copy import deepcopy
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.application.reporting import (
    ReportArtifact,
    analyze_trade_rows,
)
from kiwoom_stock.infrastructure.reporting import (
    CollectorMinuteChartSource,
    CsvReportArtifactStore,
    TradeLoggerReportDataSource,
    read_traded_targets,
)
from kiwoom_stock.reporting import minute_chart, trade_analysis


TARGET_DATE = "2026-07-17"


class _FakeDatabase:
    def __init__(self, rows=(), *, query_error=None, close_error=None):
        self.rows = list(rows)
        self.query_error = query_error
        self.close_error = close_error
        self.queried_dates = []
        self.close_calls = 0
        self.worker_alive = True

    def get_today_traded_targets(self, target_date):
        self.queried_dates.append(target_date)
        if self.query_error is not None:
            raise self.query_error
        return self.rows

    def close(self):
        self.close_calls += 1
        self.worker_alive = False
        if self.close_error is not None:
            raise self.close_error


class _Collector:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def fetch_minute_chart(self, stock_code, tic):
        self.calls.append((stock_code, tic))
        return self.rows


def _trade_row(**overrides):
    row = {
        "id": 1,
        "stock_code": "005930",
        "stock_name": "Sample",
        "buy_price": 100.0,
        "thrust": 3.0,
        "gravity": -1.0,
        "drag": -1.0,
        "magnetic": -1.0,
        "jerk": -1.0,
        "impulse": -1.0,
        "net_force": 1.0,
        "profit_rate": 2.01,
        "status": "CLOSED",
    }
    row.update(overrides)
    return row


def test_data_source_uses_only_configured_path_copies_rows_and_closes_worker(
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    configured_path = tmp_path / "configured-report.db"
    original = _trade_row()
    database = _FakeDatabase([original])
    factory_paths = []

    def database_factory(path):
        factory_paths.append(path)
        return database

    source = TradeLoggerReportDataSource(
        configured_path,
        database_factory=database_factory,
    )
    rows = source.load_trades(TARGET_DATE)

    original["stock_name"] = "mutated after query"
    assert factory_paths == [configured_path]
    assert database.queried_dates == [TARGET_DATE]
    assert database.close_calls == 1
    assert database.worker_alive is False
    assert rows[0]["stock_name"] == "Sample"
    assert rows[0] is not original
    assert not (tmp_path / "trades.db").exists()


def test_data_source_raises_close_error_after_success(tmp_path):
    close_error = RuntimeError("close failed")
    database = _FakeDatabase([_trade_row()], close_error=close_error)
    source = TradeLoggerReportDataSource(
        tmp_path / "configured.db",
        database_factory=lambda _path: database,
    )

    with pytest.raises(RuntimeError) as caught:
        source.load_trades(TARGET_DATE)

    assert caught.value is close_error
    assert database.close_calls == 1


def test_data_source_preserves_query_error_and_notes_close_interruption(tmp_path):
    query_error = RuntimeError("query failed")
    close_error = KeyboardInterrupt("close interrupted")
    database = _FakeDatabase(
        query_error=query_error,
        close_error=close_error,
    )
    target_logger = MagicMock()
    source = TradeLoggerReportDataSource(
        tmp_path / "configured.db",
        database_factory=lambda _path: database,
        target_logger=target_logger,
    )

    with pytest.raises(RuntimeError) as caught:
        source.load_trades(TARGET_DATE)

    assert caught.value is query_error
    assert query_error.__notes__ == [
        "report DB close also failed: close interrupted"
    ]
    assert database.close_calls == 1
    assert database.worker_alive is False
    target_logger.critical.assert_called_once()


def test_minute_source_uses_one_minute_tic_and_copies_collector_rows():
    original = {"체결시간": "20260717100000", "현재가": "100"}
    collector = _Collector([original])
    source = CollectorMinuteChartSource(collector)

    rows = source.load_minutes("005930", TARGET_DATE)
    rows[0]["현재가"] = "changed"

    assert collector.calls == [("005930", "1")]
    assert original["현재가"] == "100"
    assert rows[0] is not original

    collector.rows = None
    assert source.load_minutes("000660", TARGET_DATE) == ()
    assert collector.calls[-1] == ("000660", "1")


def test_minute_artifact_preserves_priority_filter_reverse_bom_and_schema(tmp_path):
    rows = [
        {
            "체결시간": "20260717100200",
            "cntr_tm": "20260718100200",
            "현재가": "102",
        },
        {
            "체결시간": "20260718100100",
            "cntr_tm": "20260717100100",
            "현재가": "discarded",
        },
        {
            "체결시간": "20260717100000",
            "cntr_tm": "20260718100000",
            "현재가": "100",
        },
    ]
    before = deepcopy(rows)
    store = CsvReportArtifactStore(tmp_path)

    artifact = store.save_minute_chart(
        stock_code="005930",
        stock_name="삼성전자",
        target_date=TARGET_DATE,
        rows=rows,
    )

    expected_path = tmp_path / "삼성전자_005930_1min_2026-07-17.csv"
    assert artifact == ReportArtifact(
        kind="minute_chart",
        logical_name=expected_path.name,
        reference=str(expected_path),
    )
    payload = expected_path.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8-sig").splitlines() == [
        "체결시간,cntr_tm,현재가",
        "20260717100000,20260718100000,100",
        "20260717100200,20260718100200,102",
    ]
    assert rows == before


def test_minute_artifact_without_time_column_reverses_all_rows(tmp_path):
    store = CsvReportArtifactStore(tmp_path)

    artifact = store.save_minute_chart(
        stock_code="005930",
        stock_name="Sample",
        target_date=TARGET_DATE,
        rows=[{"marker": "first"}, {"marker": "second"}],
    )

    assert artifact is not None
    assert Path(artifact.reference).read_text(encoding="utf-8-sig").splitlines() == [
        "marker",
        "second",
        "first",
    ]


@pytest.mark.parametrize(
    "rows",
    [
        (),
        ({"체결시간": "20260718100000", "현재가": "other date"},),
    ],
)
def test_minute_artifact_skips_empty_or_filtered_empty_rows(tmp_path, rows):
    store = CsvReportArtifactStore(tmp_path)

    artifact = store.save_minute_chart(
        stock_code="005930",
        stock_name="Sample",
        target_date=TARGET_DATE,
        rows=rows,
    )

    assert artifact is None
    assert list(tmp_path.glob("*_1min_*.csv")) == []


def test_trade_artifact_preserves_analyzed_schema_bytes_and_input_rows(tmp_path):
    original = _trade_row()
    analyzed = analyze_trade_rows([original])
    analyzed_before = deepcopy(analyzed)
    store = CsvReportArtifactStore(tmp_path)

    artifact = store.save_trade_analysis(
        target_date=TARGET_DATE,
        rows=analyzed,
    )

    expected_path = tmp_path / "physics_trade_analysis_2026-07-17.csv"
    assert artifact == ReportArtifact(
        kind="trade_analysis",
        logical_name=expected_path.name,
        reference=str(expected_path),
    )
    payload = expected_path.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8-sig").splitlines() == [
        (
            "id,stock_code,stock_name,buy_price,thrust,gravity,drag,magnetic,"
            "jerk,impulse,net_force,profit_rate,status,primary_driver,judgement"
        ),
        (
            "1,005930,Sample,100.0,3.0,-1.0,-1.0,-1.0,-1.0,-1.0,1.0,"
            "2.01,CLOSED,Thrust,🎯 정밀타격"
        ),
    ]
    assert analyzed == analyzed_before
    assert "primary_driver" not in original


def test_trade_artifact_skips_empty_rows(tmp_path):
    store = CsvReportArtifactStore(tmp_path)

    assert store.save_trade_analysis(target_date=TARGET_DATE, rows=()) is None
    assert list(tmp_path.glob("physics_trade_analysis_*.csv")) == []


def test_helpers_share_data_boundary_and_keep_injected_signatures():
    assert minute_chart._read_traded_targets is read_traded_targets
    assert trade_analysis._read_traded_targets is read_traded_targets
    assert tuple(inspect.signature(read_traded_targets).parameters) == (
        "target_date_str",
        "database_path",
        "database_factory",
        "target_logger",
    )
    assert tuple(
        inspect.signature(minute_chart._extract_and_save_1min_chart).parameters
    ) == (
        "target_date_str",
        "config_module",
        "datetime_type",
        "client_factory",
        "collector_factory",
        "database_factory",
        "target_logger",
        "credential_provider_factory",
    )
    assert tuple(
        inspect.signature(trade_analysis._analyze_trade_efficiency).parameters
    ) == (
        "target_date_str",
        "config_module",
        "datetime_type",
        "database_factory",
        "target_logger",
    )


def test_trade_helper_delegates_rules_to_analyze_trade_rows(
    tmp_path,
    monkeypatch,
):
    database = _FakeDatabase([_trade_row()])
    calls = []

    def analyze(rows):
        calls.append(tuple(dict(row) for row in rows))
        return ({"delegated": "yes"},)

    monkeypatch.setattr(trade_analysis, "analyze_trade_rows", analyze)
    config = SimpleNamespace(
        OUTPUT_DIR_STR=str(tmp_path),
        configure_from_environment=lambda **_kwargs: SimpleNamespace(
            database=SimpleNamespace(path=tmp_path / "configured.db")
        ),
    )
    frozen_datetime = SimpleNamespace(
        now=lambda: SimpleNamespace(date=lambda: "unused")
    )

    result = trade_analysis._analyze_trade_efficiency(
        TARGET_DATE,
        config_module=config,
        datetime_type=frozen_datetime,
        database_factory=lambda _path: database,
        target_logger=MagicMock(),
    )

    assert len(calls) == 1
    assert result == str(tmp_path / "physics_trade_analysis_2026-07-17.csv")
    assert Path(result).read_text(encoding="utf-8-sig").splitlines() == [
        "delegated",
        "yes",
    ]
