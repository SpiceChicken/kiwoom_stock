"""Compatibility wrapper for the installed trade-analysis helper."""

import logging
from datetime import datetime
from typing import Optional

from kiwoom_stock.core import config
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.reporting.trade_analysis import _analyze_trade_efficiency


logger = logging.getLogger(__name__)


def analyze_trade_efficiency(target_date_str: Optional[str] = None):
    """Preserve the legacy root-tool callable while delegating its implementation."""

    return _analyze_trade_efficiency(
        target_date_str,
        config_module=config,
        datetime_type=datetime,
        database_factory=TradeLogger,
        target_logger=logger,
    )


if __name__ == "__main__":
    analyze_trade_efficiency()
