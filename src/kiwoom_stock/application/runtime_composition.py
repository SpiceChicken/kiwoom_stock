"""Typed runtime build plans kept separate from resource acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict

from kiwoom_stock.core.config import report_output_dir_for
from kiwoom_stock.domain.strategy import TargetStopPolicy
from kiwoom_stock.settings import Settings


@dataclass(frozen=True)
class TradingRuntimePlan:
    """Immutable inputs consumed after settings activation and before I/O."""

    settings: Settings
    database_path: Path
    app_config: Dict[str, Any]
    output_dir_str: str
    target_stop_policy: TargetStopPolicy


def build_trading_runtime_plan(
    settings: Settings,
    *,
    today: date,
    compatibility_module: Any,
) -> TradingRuntimePlan:
    """Build pure runtime inputs without constructing a client or ledger."""

    system_config, strategy_config = settings.to_legacy_mappings()
    return TradingRuntimePlan(
        settings=settings,
        database_path=settings.database.path,
        app_config={**system_config, **strategy_config},
        output_dir_str=str(report_output_dir_for(settings, today, compatibility_module)),
        target_stop_policy=settings.strategy.target_stop_policy,
    )
