import os
import ast
import subprocess
import sys
from dataclasses import asdict, fields
from pathlib import Path

import pytest

from kiwoom_stock.domain import (
    ForeignData,
    MarketRegime,
    PgmData,
    Position,
    SupplyData,
    TargetStopPolicy,
    calculate_position_return_percentage_points,
)


def test_domain_models_are_reexported_from_legacy_paths():
    from kiwoom_stock.core.schema import ForeignData as LegacyForeignData
    from kiwoom_stock.core.schema import PgmData as LegacyPgmData
    from kiwoom_stock.core.schema import SupplyData as LegacySupplyData
    from kiwoom_stock.core.types import MarketRegime as LegacyMarketRegime
    from kiwoom_stock.monitoring.manager import Position as LegacyPosition

    assert LegacyForeignData is ForeignData
    assert LegacyPgmData is PgmData
    assert LegacySupplyData is SupplyData
    assert LegacyMarketRegime is MarketRegime
    assert LegacyPosition is Position


def test_reporting_contracts_preserve_legacy_import_identity():
    from kiwoom_stock.application import reporting as legacy
    from kiwoom_stock.application import reporting_contracts as contracts

    for name in (
        "DailyReportRequest",
        "DailyReportResult",
        "DailyReportStats",
        "NarrationResult",
        "NarrationStatus",
        "ReportArtifact",
        "ReportStageResult",
    ):
        assert getattr(legacy, name) is getattr(contracts, name)


def test_operational_public_import_matrix_remains_loadable():
    """Freeze the module paths used by current callers and operators."""

    from kiwoom_stock.api.services.account import AccountService
    from kiwoom_stock.application.execution import ExecutionPolicy
    from kiwoom_stock.core.config import Settings as LegacySettings
    from kiwoom_stock.core.database import TradeLogger
    from kiwoom_stock.core.swing_ledger import SwingLedger
    from kiwoom_stock.monitoring.reporter import DailyReporter
    from kiwoom_stock.settings import Settings

    assert LegacySettings is Settings
    assert AccountService.__module__ == "kiwoom_stock.api.services.account"
    assert ExecutionPolicy.__module__ == "kiwoom_stock.application.execution"
    assert TradeLogger.__module__ == "kiwoom_stock.core.database"
    assert SwingLedger.__module__ == "kiwoom_stock.core.swing_ledger"
    assert DailyReporter.__module__ == "kiwoom_stock.monitoring.reporter"


def test_position_field_contract_and_profit_calculation_are_preserved():
    expected_fields = [
        "id",
        "stock_code",
        "stock_name",
        "buy_price",
        "buy_time",
        "buy_regime",
        "status",
        "thrust",
        "gravity",
        "drag",
        "magnetic",
        "jerk",
        "impulse",
        "net_force",
        "sell_price",
        "profit_rate",
        "sell_time",
        "sell_reason",
        "atr_percent",
        "down_atr_percent",
        "owning_session_date",
        "state_changed_at",
    ]
    assert [field.name for field in fields(Position)] == expected_fields

    position = Position(
        id=1,
        stock_code="005930",
        stock_name="Samsung",
        buy_price=100.0,
        buy_time="2026-07-18 09:00:00",
        buy_regime="STABLE_BULL",
    )
    assert position.status == "OPEN"
    assert position.calc_profit_rate == 0.0

    position.sell_price = 112.345
    assert position.calc_profit_rate == 12.345


def test_strategy_domain_surface_exports_policy_and_return_ssot():
    policy = TargetStopPolicy()

    assert policy.target_profit_percentage_points == 3.0
    assert calculate_position_return_percentage_points(100.0, 102.555) == pytest.approx(
        2.555
    )


def test_strategy_policy_dependency_direction_has_no_settings_or_pydantic_import():
    root = Path(__file__).resolve().parents[1] / "src" / "kiwoom_stock"
    forbidden = {
        "kiwoom_stock.settings",
        "pydantic",
        "pydantic_settings",
    }

    for path in (root / "domain" / "strategy.py", root / "monitoring" / "strategy.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)

        assert imports.isdisjoint(forbidden)


def test_supply_data_defaults_and_serialized_shape_are_preserved():
    data = SupplyData(stock_code="005930")
    rendered = asdict(data)

    assert rendered["stock_code"] == "005930"
    assert rendered["strength"] == 100.0
    assert rendered["trend_rsi"] == 50.0
    assert rendered["atr_percent"] == 0.5
    assert rendered["down_atr_percent"] == 0.5
    assert rendered["pgm_data"] == {
        "netprps_prica": 0.0,
        "all_trde_rt": 0.0,
        "buy_cntr_amt": 0.0,
        "sel_cntr_amt": 0.0,
    }
    assert rendered["foreign_data"] == {"netprps_prica": 0.0, "trde_prica": 1.0}
    assert rendered["forces"] == {}


def test_package_module_help_has_no_runtime_side_effects(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "kiwoom_stock", "--help"],
        cwd=tmp_path,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0
    assert "Kiwoom stock monitoring package utilities" in result.stdout
    assert not (tmp_path / "trades.db").exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()


def test_package_check_config_reports_missing_settings_without_runtime_side_effects(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("KIWOOM_")
    }
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")

    result = subprocess.run(
        [sys.executable, "-m", "kiwoom_stock", "--check-config"],
        cwd=tmp_path,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "KIWOOM_PROCESS_NAME" in result.stderr
    assert not (tmp_path / "trades.db").exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()
