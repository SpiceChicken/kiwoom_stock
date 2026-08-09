import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from kiwoom_stock.core import indicators as legacy_indicators
from kiwoom_stock.core import physics_engine as legacy_physics
from kiwoom_stock.domain import indicators as domain_indicators
from kiwoom_stock.domain import physics as domain_physics
from kiwoom_stock.domain.strategy import (
    StrategySemanticsValidationError,
    TargetStopPolicy,
    calculate_position_return_percentage_points,
)


def test_indicator_legacy_path_reexports_domain_functions():
    names = [
        "calculate_roc",
        "calculate_disparity",
        "calculate_slope",
        "calculate_volume_ratio",
        "calculate_volatility_ratio",
        "calculate_sma",
        "calculate_ema",
        "calculate_rsi",
        "calculate_bollinger_bands",
        "calculate_atr",
        "calculate_atr_percent",
    ]

    for name in names:
        assert getattr(legacy_indicators, name) is getattr(domain_indicators, name)


def test_physics_legacy_path_reexports_domain_functions():
    names = [
        "_sigmoid",
        "_rational_penalty",
        "_calculate_thrust_force",
        "_calculate_gravity_force",
        "_calculate_drag_force",
        "_calculate_magnetic_force",
        "_calculate_jerk_force",
        "_calculate_impulse",
        "calculate_net_velocity",
    ]

    for name in names:
        assert getattr(legacy_physics, name) is getattr(domain_physics, name)


def test_domain_calculation_modules_have_no_forbidden_dependencies():
    forbidden_modules = {
        "requests",
        "boto3",
        "slack_sdk",
        "google.generativeai",
        "sqlite3",
    }
    forbidden_calls = {
        ("os", "getenv"),
        ("datetime", "now"),
        ("time", "sleep"),
        ("os", "makedirs"),
    }
    domain_dir = Path(__file__).resolve().parents[1] / "src" / "kiwoom_stock" / "domain"

    for path in [
        domain_dir / "indicators.py",
        domain_dir / "physics.py",
        domain_dir / "strategy.py",
    ]:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imports = set()
        calls = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name):
                    calls.add((node.func.value.id, node.func.attr))

        assert imports.isdisjoint(forbidden_modules)
        assert calls.isdisjoint(forbidden_calls)


def test_target_stop_policy_is_frozen_normalized_and_defaults_to_percentage_points():
    policy = TargetStopPolicy()

    assert policy.unit_version == "percentage-points-v1"
    assert policy.target_profit_percentage_points == 3.0
    assert policy.stop_loss_percentage_points == 3.0
    with pytest.raises(FrozenInstanceError):
        policy.target_profit_percentage_points = 4.0


@pytest.mark.parametrize(
    ("kwargs", "buy_price", "current_price"),
    [
        ({"unit_version": "ratio-v0"}, 100.0, 103.0),
        ({"target_profit_percentage_points": True}, 100.0, 103.0),
        ({"target_profit_percentage_points": "3.0"}, 100.0, 103.0),
        ({"target_profit_percentage_points": float("nan")}, 100.0, 103.0),
        ({"stop_loss_percentage_points": float("inf")}, 100.0, 103.0),
        ({"stop_loss_percentage_points": 0.0}, 100.0, 103.0),
        ({"stop_loss_percentage_points": -1.0}, 100.0, 103.0),
        ({}, True, 103.0),
        ({}, "100", 103.0),
        ({}, 0.0, 103.0),
        ({}, -100.0, 103.0),
        ({}, 100.0, float("nan")),
        ({}, 100.0, float("inf")),
        ({}, 100.0, 0.0),
    ],
)
def test_target_stop_policy_and_return_reject_invalid_domain_values(
    kwargs,
    buy_price,
    current_price,
):
    if kwargs:
        with pytest.raises(StrategySemanticsValidationError):
            TargetStopPolicy(**kwargs)
    else:
        with pytest.raises(StrategySemanticsValidationError):
            calculate_position_return_percentage_points(buy_price, current_price)


def test_position_return_preserves_fractional_percentage_points_without_rounding():
    assert calculate_position_return_percentage_points(10_000, 10_255.5) == 2.555
    assert calculate_position_return_percentage_points(10_000, 9_744.5) == -2.555
