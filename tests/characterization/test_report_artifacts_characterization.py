"""Golden contracts for the current post-market CSV artifacts."""

import csv
from datetime import datetime
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from kiwoom_stock.application.credentials import KiwoomClientCredentials, SensitiveText
from kiwoom_stock.reporting.minute_chart import _extract_and_save_1min_chart
from kiwoom_stock.reporting.trade_analysis import _analyze_trade_efficiency
from kiwoom_stock.settings import KiwoomApiMode, KiwoomEndpoint


TARGET_DATE = "2026-07-17"


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 18, 9, 30, 0)
        if tz is None:
            return value
        return value.replace(tzinfo=tz)


class _Config:
    def __init__(self, output_dir: Path, database_path: Path):
        self.OUTPUT_DIR_STR = str(output_dir)
        self.CONFIG = {}
        self.database_path = database_path
        self.configured_dates = []
        self.settings = SimpleNamespace(
            kiwoom=SimpleNamespace(
                api_mode=KiwoomApiMode.MOCK,
                credentials_dir=output_dir / "unused-credentials",
                endpoint=KiwoomEndpoint.MOCK,
            ),
            database=SimpleNamespace(path=self.database_path),
        )

    def validate_environment_settings(self):
        return self.settings

    def activate_runtime_settings(self, settings, *, today):
        assert settings is self.settings
        self.configured_dates.append(today)
        return self.settings

    def configure_from_environment(self, *, today):
        return self.activate_runtime_settings(self.settings, today=today)


class _Database:
    def __init__(self, rows):
        self.rows = tuple(dict(row) for row in rows)
        self.queried_dates = []
        self.close_calls = 0

    def get_today_traded_targets(self, target_date):
        self.queried_dates.append(target_date)
        return tuple(dict(row) for row in self.rows)

    def close(self):
        self.close_calls += 1


class _Collector:
    def __init__(self, rows_by_code):
        self.rows_by_code = rows_by_code
        self.calls = []

    def fetch_minute_chart(self, code, tic):
        self.calls.append((code, tic))
        return [dict(row) for row in self.rows_by_code[code]]


def _run_minute_export(tmp_path, *, target_rows, rows_by_code):
    database_path = tmp_path / "configured-report.db"
    config = _Config(tmp_path, database_path)
    database = _Database(target_rows)
    collector = _Collector(rows_by_code)
    clients = []
    credentials = KiwoomClientCredentials(
        SensitiveText("fake-app-key"),
        SensitiveText("fake-secret-key"),
    )

    class Provider:
        def load(self):
            return credentials

    def client_factory(**credentials):
        clients.append(credentials)
        return SimpleNamespace(
            market=object(),
            ensure_auth_ready=lambda: None,
            close=lambda: None,
        )

    saved = _extract_and_save_1min_chart(
        TARGET_DATE,
        config_module=config,
        datetime_type=_FrozenDateTime,
        client_factory=client_factory,
        collector_factory=lambda _market: collector,
        database_factory=lambda path: database
        if path == database_path
        else pytest.fail(f"unexpected DB path: {path}"),
        target_logger=SimpleNamespace(
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        ),
        credential_provider_factory=lambda _path: Provider(),
    )
    assert config.configured_dates == [_FrozenDateTime(2026, 7, 18).date()]
    assert database.queried_dates == [TARGET_DATE]
    assert database.close_calls == 1
    assert clients == [
        {
            "credentials": credentials,
            "endpoint": KiwoomEndpoint.MOCK,
        }
    ]
    return saved, collector


def test_minute_chart_golden_filters_reverses_deduplicates_and_skips_no_data(tmp_path):
    target_rows = [
        {"stock_code": "005930", "stock_name": "삼성전자"},
        {"stock_code": "005930", "stock_name": "삼성전자"},
        {"stock_code": "000660", "stock_name": "SK하이닉스"},
        {"stock_code": "035420", "stock_name": "NAVER"},
    ]
    rows_by_code = {
        "005930": [
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
        ],
        "000660": [],
        "035420": [{"체결시간": "20260718100000", "현재가": "empty-after-filter"}],
    }

    saved, collector = _run_minute_export(
        tmp_path,
        target_rows=target_rows,
        rows_by_code=rows_by_code,
    )

    expected = tmp_path / "삼성전자_005930_1min_2026-07-17.csv"
    assert saved == [str(expected)]
    assert sorted(collector.calls) == [
        ("000660", "1"),
        ("005930", "1"),
        ("035420", "1"),
    ]
    assert collector.calls.count(("005930", "1")) == 1

    payload = expected.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    assert payload.decode("utf-8-sig").splitlines() == [
        "체결시간,cntr_tm,현재가",
        "20260717100000,20260718100000,100",
        "20260717100200,20260718100200,102",
    ]


@pytest.mark.parametrize(
    ("columns", "expected_column"),
    [
        (("체결시간", "cntr_tm", "dt", "date"), "체결시간"),
        (("cntr_tm", "dt", "date"), "cntr_tm"),
        (("dt", "date"), "dt"),
        (("date",), "date"),
    ],
)
def test_minute_chart_time_column_priority(tmp_path, columns, expected_column):
    target_prefix = TARGET_DATE.replace("-", "")
    first = {column: "20260718100000" for column in columns}
    second = {column: f"{target_prefix}100100" for column in columns}
    first[expected_column] = f"{target_prefix}100000"
    second[expected_column] = "20260718100100"
    first["marker"] = "kept"
    second["marker"] = "discarded"

    saved, _collector = _run_minute_export(
        tmp_path,
        target_rows=[{"stock_code": "005930", "stock_name": "Sample"}],
        rows_by_code={"005930": [first, second]},
    )

    payload = Path(saved[0]).read_text(encoding="utf-8-sig")
    assert payload.splitlines()[1].endswith(",kept")
    assert "discarded" not in payload


def test_minute_chart_no_targets_creates_no_artifact_or_fetch(tmp_path):
    saved, collector = _run_minute_export(
        tmp_path,
        target_rows=[],
        rows_by_code={},
    )

    assert saved == []
    assert collector.calls == []
    assert list(tmp_path.glob("*_1min_*.csv")) == []


def _trade_row(case, *, profit_rate=0.0, net_force=0.0, forces=None):
    force_values = {
        "thrust": -1.0,
        "gravity": -1.0,
        "drag": -1.0,
        "magnetic": -1.0,
        "jerk": -1.0,
        "impulse": -1.0,
    }
    if forces:
        force_values.update(forces)
    return {
        "case": case,
        "stock_code": "005930",
        "stock_name": "Sample",
        "buy_price": 100.0,
        **force_values,
        "net_force": net_force,
        "profit_rate": profit_rate,
        "status": "CLOSED",
    }


def test_trade_analysis_golden_force_order_boundaries_schema_filename_and_bytes(tmp_path):
    force_candidates = [
        ("thrust", "Thrust"),
        ("gravity", "Gravity"),
        ("drag", "Drag"),
        ("magnetic", "Magnetic"),
        ("jerk", "Jerk"),
        ("impulse", "Impulse"),
    ]
    rows = [
        _trade_row(f"candidate-{field}", forces={field: 3.0})
        for field, _label in force_candidates
    ]
    rows.extend(
        [
            _trade_row(
                "positive-max",
                forces={"thrust": 1.0, "gravity": 2.0, "drag": 3.0},
            ),
            _trade_row("tie", forces={"thrust": 2.0, "gravity": 2.0}),
            _trade_row("no-positive"),
            _trade_row("plus-two", profit_rate=2.0, net_force=9.0),
            _trade_row("minus-two", profit_rate=-2.0, net_force=9.0),
            _trade_row("profit-force", profit_rate=2.01, net_force=1.0),
            _trade_row("profit-weak", profit_rate=2.01, net_force=0.99),
            _trade_row("loss-force", profit_rate=-2.01, net_force=1.0),
            _trade_row("loss-weak", profit_rate=-2.01, net_force=0.99),
        ]
    )
    database_path = tmp_path / "configured-analysis.db"
    config = _Config(tmp_path, database_path)
    database = _Database(rows)

    result = _analyze_trade_efficiency(
        TARGET_DATE,
        config_module=config,
        datetime_type=_FrozenDateTime,
        database_factory=lambda path: database
        if path == database_path
        else pytest.fail(f"unexpected DB path: {path}"),
        target_logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    )

    expected = tmp_path / "physics_trade_analysis_2026-07-17.csv"
    assert result == str(expected)
    assert database.queried_dates == [TARGET_DATE]
    assert database.close_calls == 1

    payload = expected.read_bytes()
    assert payload.startswith(b"\xef\xbb\xbf")
    decoded = payload.decode("utf-8-sig")
    expected_header = tuple(rows[0]) + ("primary_driver", "judgement")
    assert tuple(decoded.splitlines()[0].split(",")) == expected_header
    serialized_rows = list(csv.DictReader(StringIO(decoded)))
    assert serialized_rows[0] == {
        "case": "candidate-thrust",
        "stock_code": "005930",
        "stock_name": "Sample",
        "buy_price": "100.0",
        "thrust": "3.0",
        "gravity": "-1.0",
        "drag": "-1.0",
        "magnetic": "-1.0",
        "jerk": "-1.0",
        "impulse": "-1.0",
        "net_force": "0.0",
        "profit_rate": "0.0",
        "status": "CLOSED",
        "primary_driver": "Thrust",
        "judgement": "➖ 보합(마찰 상쇄)",
    }
    assert "🎯 정밀타격".encode() in payload
    assert "🤔 요행(가속부족)".encode() in payload
    assert "❌ 엔진과열(오판)".encode() in payload
    assert "⚠️ 억지진입(동력부족)".encode() in payload
    assert "➖ 보합(마찰 상쇄)".encode() in payload

    frame = pd.read_csv(expected, keep_default_na=False)
    assert frame["case"].tolist() == [row["case"] for row in rows]
    assert frame["primary_driver"].tolist() == [
        *(label for _field, label in force_candidates),
        "Drag",
        "Thrust",
        "None",
        "None",
        "None",
        "None",
        "None",
        "None",
        "None",
    ]
    assert frame.set_index("case")["judgement"].to_dict() == {
        **{f"candidate-{field}": "➖ 보합(마찰 상쇄)" for field, _label in force_candidates},
        "positive-max": "➖ 보합(마찰 상쇄)",
        "tie": "➖ 보합(마찰 상쇄)",
        "no-positive": "➖ 보합(마찰 상쇄)",
        "plus-two": "➖ 보합(마찰 상쇄)",
        "minus-two": "➖ 보합(마찰 상쇄)",
        "profit-force": "🎯 정밀타격",
        "profit-weak": "🤔 요행(가속부족)",
        "loss-force": "❌ 엔진과열(오판)",
        "loss-weak": "⚠️ 억지진입(동력부족)",
    }


def test_trade_analysis_no_targets_creates_no_artifact(tmp_path):
    database_path = tmp_path / "empty-analysis.db"
    config = _Config(tmp_path, database_path)
    database = _Database([])

    assert _analyze_trade_efficiency(
        TARGET_DATE,
        config_module=config,
        datetime_type=_FrozenDateTime,
        database_factory=lambda _path: database,
        target_logger=SimpleNamespace(info=lambda *_args, **_kwargs: None),
    ) is None
    assert database.close_calls == 1
    assert list(tmp_path.glob("physics_trade_analysis_*.csv")) == []


def _daily_reporter(monkeypatch):
    from kiwoom_stock.monitoring import reporter as reporter_module

    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    return reporter_module.DailyReporter(SimpleNamespace()), reporter_module


def test_daily_reporter_stats_formula_format_and_defense_use_real_csv(
    tmp_path,
    monkeypatch,
):
    reporter, _reporter_module = _daily_reporter(monkeypatch)
    artifact = tmp_path / "physics_trade_analysis_2026-07-17.csv"
    pd.DataFrame(
        {
            "profit_rate": [2.0, 0.0, -1.25, 3.375],
            "status": ["진입", "수급 차단", None, "차단 후 해제"],
        }
    ).to_csv(artifact, index=False, encoding="utf-8-sig")

    assert reporter._load_and_parse_stats(str(artifact)) == {
        "date": "2026-07-18",
        "win_rate": "50.0% (2승 2패)",
        "total_pnl": "+4.12%",
        "defense_count": 2,
    }


def test_daily_reporter_stats_without_status_keeps_zero_defense(
    tmp_path,
    monkeypatch,
):
    reporter, _reporter_module = _daily_reporter(monkeypatch)
    artifact = tmp_path / "analysis-without-status.csv"
    pd.DataFrame({"profit_rate": [1.25, -0.25]}).to_csv(
        artifact,
        index=False,
        encoding="utf-8-sig",
    )

    assert reporter._load_and_parse_stats(str(artifact)) == {
        "date": "2026-07-18",
        "win_rate": "50.0% (1승 1패)",
        "total_pnl": "+1.00%",
        "defense_count": 0,
    }


def test_daily_reporter_missing_artifact_and_none_use_exact_defaults(
    tmp_path,
    monkeypatch,
):
    reporter, _reporter_module = _daily_reporter(monkeypatch)
    defaults = {
        "date": "2026-07-18",
        "win_rate": "N/A",
        "total_pnl": 0.0,
        "defense_count": 0,
    }

    assert reporter._load_and_parse_stats(None) == defaults
    assert reporter._load_and_parse_stats(str(tmp_path / "missing.csv")) == defaults


def test_daily_reporter_parse_error_uses_exact_defaults(tmp_path, monkeypatch):
    reporter, _reporter_module = _daily_reporter(monkeypatch)
    invalid_artifact = tmp_path / "invalid-utf8.csv"
    invalid_artifact.write_bytes(b"profit_rate,status\n\xff,\xff\n")

    assert reporter._load_and_parse_stats(str(invalid_artifact)) == {
        "date": "2026-07-18",
        "win_rate": "N/A",
        "total_pnl": 0.0,
        "defense_count": 0,
    }
