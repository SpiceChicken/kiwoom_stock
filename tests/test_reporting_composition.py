from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from kiwoom_stock.application.reporting import NarrationResult
from kiwoom_stock.application.reporting_composition import build_daily_reporter


class _Database:
    def __init__(self, path):
        self.path = path
        self.closed = False

    def get_today_traded_targets(self, target_date):
        return []

    def close(self):
        self.closed = True


class _Collector:
    def fetch_minute_chart(self, code, tic="1"):
        return []


class _Publisher:
    def summary_enabled(self):
        return False

    def publish_telemetry(self, **kwargs):
        return False


class _Narrator:
    def narrate(self, **kwargs):
        return NarrationResult.unavailable()


def test_factory_wires_typed_reporter_without_running_io(tmp_path: Path):
    reporter = build_daily_reporter(
        database_path=tmp_path / "trades.db",
        output_dir=tmp_path,
        market_collector=_Collector(),
        narrator=_Narrator(),
        publisher=_Publisher(),
        clock=lambda: datetime(2026, 7, 19),
        database_factory=_Database,
    )

    result = reporter.run_pipeline()

    assert result.failed_stage is None
    assert result.stages[0].stage == "minute_chart"


def test_runtime_reporter_factory_defers_monitor_graph_access(monkeypatch, tmp_path):
    import main as main_module

    runtime = SimpleNamespace(
        settings=SimpleNamespace(database=SimpleNamespace(path=tmp_path / "trades.db")),
        output_dir_str=str(tmp_path),
    )
    # Lightweight monitors used by startup/session tests do not expose an
    # analyzer.  Creating the factory must still be safe; only invocation
    # requires the production collector graph.
    monitor = SimpleNamespace()
    sentinel = object()
    monkeypatch.setattr(main_module, "build_daily_reporter", lambda **kwargs: sentinel)

    factory = main_module._reporter_factory_for_runtime(runtime, monitor)

    assert callable(factory)
    assert factory.__name__ == "factory"

    monitor.analyzer = SimpleNamespace(collector=object())
    notifier = SimpleNamespace(ai_client=object())
    assert factory(notifier) is sentinel
