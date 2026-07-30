"""Generate the legacy physics trade-efficiency CSV report."""

from datetime import datetime
import logging
from pathlib import Path
from typing import Any, Optional

from kiwoom_stock.application.reporting import analyze_trade_rows
from kiwoom_stock.core import config
from kiwoom_stock.infrastructure.reporting import (
    CsvReportArtifactStore,
    read_traded_targets as _read_traded_targets,
)


logger = logging.getLogger(__name__)


def _analyze_trade_efficiency(
    target_date_str: Optional[str],
    *,
    config_module: Any,
    datetime_type: Any,
    database_factory: Any,
    target_logger: logging.Logger,
) -> Optional[str]:
    """Run the implementation with explicit dependencies for legacy wrappers."""

    if target_date_str is None:
        target_date_str = datetime_type.now().strftime("%Y-%m-%d")

    settings = config_module.configure_from_environment(
        today=datetime_type.now().date()
    )

    targets = _read_traded_targets(
        target_date_str,
        database_path=settings.database.path,
        database_factory=database_factory,
        target_logger=target_logger,
    )

    if not targets:
        target_logger.info("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
        return None

    artifact_store = CsvReportArtifactStore(
        Path(config_module.OUTPUT_DIR_STR),
        target_logger=target_logger,
    )
    artifact = artifact_store.save_trade_analysis(
        target_date=target_date_str,
        rows=analyze_trade_rows(targets),
    )
    return artifact.reference if artifact is not None else None


def analyze_trade_efficiency(target_date_str: Optional[str] = None) -> Optional[str]:
    """Generate the report using the production compatibility dependencies."""

    return _analyze_trade_efficiency(
        target_date_str,
        config_module=config,
        datetime_type=datetime,
        database_factory=None,
        target_logger=logger,
    )
