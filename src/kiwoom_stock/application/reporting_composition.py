"""Explicit assembly for the post-market reporting graph.

The factory only wires already-created gateways and configuration; it never
opens a network connection or executes a report at import time.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from kiwoom_stock.application.reporting import PostMarketReportUseCase
from kiwoom_stock.infrastructure.reporting import (
    CollectorMinuteChartSource,
    CsvReportArtifactStore,
    TradeLoggerReportDataSource,
)
from kiwoom_stock.monitoring.reporter import DailyReporter


def build_post_market_use_case(
    *,
    database_path: Path,
    output_dir: Path,
    market_collector: Any,
    narrator: Any,
    publisher: Any,
    database_factory: Any = None,
) -> PostMarketReportUseCase:
    """Wire reporting ports around explicit infrastructure dependencies."""
    source = TradeLoggerReportDataSource(
        database_path, database_factory=database_factory
    )
    return PostMarketReportUseCase(
        data_source=source,
        minute_source=CollectorMinuteChartSource(market_collector),
        artifact_store=CsvReportArtifactStore(output_dir),
        narrator=narrator,
        publisher=publisher,
    )


def build_daily_reporter(
    *,
    database_path: Path,
    output_dir: Path,
    market_collector: Any,
    narrator: Any,
    publisher: Any,
    clock: Optional[Callable[[], datetime]] = None,
    database_factory: Any = None,
) -> DailyReporter:
    """Build the compatibility facade with the typed reporting use case."""
    use_case = build_post_market_use_case(
        database_path=database_path,
        output_dir=output_dir,
        market_collector=market_collector,
        narrator=narrator,
        publisher=publisher,
        database_factory=database_factory,
    )
    return DailyReporter(publisher, use_case=use_case, clock=clock)
