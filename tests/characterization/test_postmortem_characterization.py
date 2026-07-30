"""Characterize the postmortem orchestration using fake external boundaries."""

from datetime import datetime
import importlib

import pandas as pd
import pytest


@pytest.fixture
def reporter_module(tmp_path, monkeypatch):
    # Importing legacy config creates its output directory. Keep that behavior
    # import-time filesystem behavior inside pytest's temporary directory.
    monkeypatch.setenv("KIWOOM_OUTPUT_DIR", str(tmp_path))
    return importlib.import_module("kiwoom_stock.monitoring.reporter")


class _RecordingNotifier:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail
        self.calls = []

    def send_daily_post_mortem(self, stats, csv_path):
        self.events.append("summary")
        self.calls.append((stats, csv_path))
        if self.fail:
            raise RuntimeError("summary failed")


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 18, 16, 0, 0)
        if tz is None:
            return value
        return value.replace(tzinfo=tz)


def test_postmortem_success_order_runs_real_stats_with_fake_io(
    reporter_module,
    monkeypatch,
):
    events = []
    notifier = _RecordingNotifier(events)
    reporter = reporter_module.DailyReporter(notifier)

    def extract(target_date):
        assert target_date == "2026-07-17"
        events.append("minute_chart")
        return ["/fake/minute-A.csv", "/fake/minute-B.csv"]

    def analyze(target_date):
        assert target_date == "2026-07-17"
        events.append("trade_analysis")
        return "/fake/trade-analysis.csv"

    def read_csv(path):
        assert path == "/fake/trade-analysis.csv"
        events.append("stats")
        return pd.DataFrame(
            {
                "profit_rate": [2.5, -1.0],
                "status": ["진입", "차단"],
            }
        )

    def upload(trade_csv_path, minute_chart_list):
        assert trade_csv_path == "/fake/trade-analysis.csv"
        assert minute_chart_list == [
            "/fake/minute-A.csv",
            "/fake/minute-B.csv",
        ]
        events.append("telemetry")

    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        extract,
    )
    monkeypatch.setattr(reporter_module, "analyze_trade_efficiency", analyze)
    monkeypatch.setattr(reporter_module.pd, "read_csv", read_csv)
    monkeypatch.setattr(reporter, "execute_slack_telemetry", upload)

    reporter.run_pipeline("2026-07-17")

    assert events == [
        "minute_chart",
        "trade_analysis",
        "stats",
        "summary",
        "telemetry",
    ]
    stats, csv_path = notifier.calls[0]
    assert csv_path == "/fake/trade-analysis.csv"
    assert stats["win_rate"] == "50.0% (1승 1패)"
    assert stats["total_pnl"] == "+1.50%"
    assert stats["defense_count"] == 1


@pytest.mark.parametrize(
    ("failed_stage", "expected_events"),
    [
        ("minute_chart", ["minute_chart"]),
        ("trade_analysis", ["minute_chart", "trade_analysis"]),
        ("summary", ["minute_chart", "trade_analysis", "stats", "summary"]),
    ],
)
def test_postmortem_current_failure_boundary_stops_later_stages(
    reporter_module,
    monkeypatch,
    failed_stage,
    expected_events,
):
    events = []
    notifier = _RecordingNotifier(events, fail=failed_stage == "summary")
    reporter = reporter_module.DailyReporter(notifier)

    def extract(_target_date):
        events.append("minute_chart")
        if failed_stage == "minute_chart":
            raise RuntimeError("minute chart failed")
        return ["/fake/minute.csv"]

    def analyze(_target_date):
        events.append("trade_analysis")
        if failed_stage == "trade_analysis":
            raise RuntimeError("trade analysis failed")
        return "/fake/trade-analysis.csv"

    def read_csv(_path):
        events.append("stats")
        return pd.DataFrame({"profit_rate": [1.0], "status": ["진입"]})

    def forbidden_upload(**_kwargs):
        events.append("telemetry")

    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        extract,
    )
    monkeypatch.setattr(reporter_module, "analyze_trade_efficiency", analyze)
    monkeypatch.setattr(reporter_module.pd, "read_csv", read_csv)
    monkeypatch.setattr(reporter, "execute_slack_telemetry", forbidden_upload)

    reporter.run_pipeline("2026-07-17")

    assert events == expected_events


def test_postmortem_stats_parse_failure_uses_defaults_and_continues(
    reporter_module,
    monkeypatch,
):
    events = []
    notifier = _RecordingNotifier(events)
    reporter = reporter_module.DailyReporter(notifier)

    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        lambda _target_date: (
            events.append("minute_chart") or ["/fake/minute.csv"]
        ),
    )
    monkeypatch.setattr(
        reporter_module,
        "analyze_trade_efficiency",
        lambda _target_date: (
            events.append("trade_analysis") or "/fake/trade.csv"
        ),
    )

    def fail_to_parse(_path):
        events.append("stats")
        raise pd.errors.ParserError("broken csv")

    def upload(**_kwargs):
        events.append("telemetry")

    monkeypatch.setattr(reporter_module.pd, "read_csv", fail_to_parse)
    monkeypatch.setattr(reporter, "execute_slack_telemetry", upload)

    assert reporter.run_pipeline("2026-07-17") is None

    assert events == [
        "minute_chart",
        "trade_analysis",
        "stats",
        "summary",
        "telemetry",
    ]
    stats, csv_path = notifier.calls[0]
    assert stats == {
        "date": "2026-07-18",
        "win_rate": "N/A",
        "total_pnl": 0.0,
        "defense_count": 0,
    }
    assert csv_path == "/fake/trade.csv"


def test_postmortem_missing_trade_csv_uses_defaults_without_reading(
    reporter_module,
    monkeypatch,
):
    events = []
    notifier = _RecordingNotifier(events)
    reporter = reporter_module.DailyReporter(notifier)

    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        lambda _target_date: events.append("minute_chart") or [],
    )
    monkeypatch.setattr(
        reporter_module,
        "analyze_trade_efficiency",
        lambda _target_date: events.append("trade_analysis") or None,
    )
    monkeypatch.setattr(
        reporter_module.pd,
        "read_csv",
        lambda _path: pytest.fail("a missing trade artifact must not be read"),
    )
    monkeypatch.setattr(
        reporter,
        "execute_slack_telemetry",
        lambda **_kwargs: events.append("telemetry"),
    )

    reporter.run_pipeline("2026-07-17")

    assert events == ["minute_chart", "trade_analysis", "summary", "telemetry"]
    assert notifier.calls == [
        (
            {
                "date": "2026-07-18",
                "win_rate": "N/A",
                "total_pnl": 0.0,
                "defense_count": 0,
            },
            None,
        )
    ]


def test_postmortem_telemetry_exception_is_contained_after_prior_stages(
    reporter_module,
    monkeypatch,
):
    events = []
    notifier = _RecordingNotifier(events)
    reporter = reporter_module.DailyReporter(notifier)

    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        lambda _target_date: (
            events.append("minute_chart") or ["/fake/minute.csv"]
        ),
    )
    monkeypatch.setattr(
        reporter_module,
        "analyze_trade_efficiency",
        lambda _target_date: (
            events.append("trade_analysis") or "/fake/trade.csv"
        ),
    )
    monkeypatch.setattr(
        reporter_module.pd,
        "read_csv",
        lambda _path: events.append("stats")
        or pd.DataFrame({"profit_rate": [0.0], "status": ["진입"]}),
    )

    def fail_upload(**_kwargs):
        events.append("telemetry")
        raise RuntimeError("telemetry failed")

    monkeypatch.setattr(reporter, "execute_slack_telemetry", fail_upload)

    assert reporter.run_pipeline("2026-07-17") is None
    assert events == [
        "minute_chart",
        "trade_analysis",
        "stats",
        "summary",
        "telemetry",
    ]


def test_stats_formula_threshold_format_and_defense_count(
    reporter_module,
    monkeypatch,
):
    reporter = reporter_module.DailyReporter(_RecordingNotifier([]))
    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(
        reporter_module.pd,
        "read_csv",
        lambda _path: pd.DataFrame(
            {
                "profit_rate": [2.0, 0.0, -1.25, 3.375],
                "status": ["진입", "수급 차단", None, "차단 후 해제"],
            }
        ),
    )

    assert reporter._load_and_parse_stats("/fake/trade.csv") == {
        "date": "2026-07-18",
        "win_rate": "50.0% (2승 2패)",
        "total_pnl": "+4.12%",
        "defense_count": 2,
    }


@pytest.mark.parametrize(
    "failure",
    [FileNotFoundError("missing"), pd.errors.ParserError("invalid csv")],
    ids=["missing-file", "parse-error"],
)
def test_stats_failures_return_exact_defaults(
    reporter_module,
    monkeypatch,
    failure,
):
    reporter = reporter_module.DailyReporter(_RecordingNotifier([]))
    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)

    def fail(_path):
        raise failure

    monkeypatch.setattr(reporter_module.pd, "read_csv", fail)

    assert reporter._load_and_parse_stats("/fake/trade.csv") == {
        "date": "2026-07-18",
        "win_rate": "N/A",
        "total_pnl": 0.0,
        "defense_count": 0,
    }
