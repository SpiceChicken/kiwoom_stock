"""Compatibility wrapper for the installed one-minute chart helper."""

import logging
from datetime import datetime
from typing import Optional

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.core import config
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.monitoring.collector import MarketDataCollector
from kiwoom_stock.reporting.minute_chart import _extract_and_save_1min_chart


logger = logging.getLogger(__name__)


def extract_and_save_1min_chart(target_date_str: Optional[str] = None):
    """Preserve the legacy root-tool callable while delegating its implementation."""

    return _extract_and_save_1min_chart(
        target_date_str,
        config_module=config,
        datetime_type=datetime,
        client_factory=KiwoomClient,
        collector_factory=MarketDataCollector,
        database_factory=TradeLogger,
        target_logger=logger,
    )


if __name__ == "__main__":
    extract_and_save_1min_chart()
