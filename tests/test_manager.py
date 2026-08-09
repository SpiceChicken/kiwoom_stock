from math import isnan
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.domain.strategy import StrategySemanticsValidationError
from kiwoom_stock.monitoring.manager import StockManager, Position


_MISSING = object()


def _assert_position_snapshot(position, expected):
    actual = vars(position)
    assert actual.keys() == expected.keys()
    for name, expected_value in expected.items():
        actual_value = actual[name]
        if isinstance(expected_value, float) and isnan(expected_value):
            assert actual_value is expected_value
        else:
            assert actual_value == expected_value

def test_manager_orchestrator_decoupling():
    """[의존성 타격] Manager가 Strategy 클래스 없이 독립적으로 생성되는가?"""
    mock_client = MagicMock()
    mock_db = MagicMock()
    mock_db.load_open_positions.return_value = {}
    
    # Manager 초기화 시 Strategy 파라미터가 완전히 사라졌는지 검증
    manager = StockManager(mock_client, mock_db, filter_config={})
    
    assert getattr(manager, 'strategy', None) is None

def test_update_position_data_pure_update():
    """[역할 타격] update_position_data가 데이터 갱신만 수행하는가? (월권 방지)"""
    mock_db = MagicMock()
    mock_db.load_open_positions.return_value = {}
    manager = StockManager(MagicMock(), mock_db, filter_config={})
    
    mock_pos = Position(
        id=1, stock_code="005930", stock_name="삼성전자", buy_price=50000, 
        buy_time="2026-02-10", buy_regime="BULL"
    )
    manager.active_positions = {"005930": mock_pos}
    before = vars(mock_pos).copy()
    
    verdict = {
        "stock_code": "005930",
        "price": 51000,
        "atr_percent": 0.5,
        "down_atr_percent": 0.5
    }
    
    updated_pos = manager.update_position_data(verdict)
    
    # Assert: 순수하게 속성값만 갱신되었는지 확인
    assert updated_pos is not None
    assert updated_pos.sell_price == 51000
    assert updated_pos.atr_percent == 0.5
    assert updated_pos.down_atr_percent == 0.5
    for name, value in before.items():
        if name not in {"sell_price", "atr_percent", "down_atr_percent"}:
            assert getattr(updated_pos, name) == value


@pytest.mark.parametrize(
    ("field", "invalid_value", "error_field"),
    [
        ("buy_price", _MISSING, "buy_price"),
        ("buy_price", None, "buy_price"),
        ("buy_price", True, "buy_price"),
        ("buy_price", float("nan"), "buy_price"),
        ("buy_price", float("inf"), "buy_price"),
        ("buy_price", 0.0, "buy_price"),
        ("buy_price", -1.0, "buy_price"),
        ("price", _MISSING, "current_price"),
        ("price", None, "current_price"),
        ("price", True, "current_price"),
        ("price", float("nan"), "current_price"),
        ("price", float("inf"), "current_price"),
        ("price", 0.0, "current_price"),
        ("price", -1.0, "current_price"),
        ("atr_percent", _MISSING, "atr_percent"),
        ("atr_percent", None, "atr_percent"),
        ("atr_percent", True, "atr_percent"),
        ("atr_percent", "0.5", "atr_percent"),
        ("atr_percent", float("nan"), "atr_percent"),
        ("atr_percent", float("inf"), "atr_percent"),
        ("atr_percent", -0.1, "atr_percent"),
        ("down_atr_percent", _MISSING, "down_atr_percent"),
        ("down_atr_percent", None, "down_atr_percent"),
        ("down_atr_percent", True, "down_atr_percent"),
        ("down_atr_percent", "0.5", "down_atr_percent"),
        ("down_atr_percent", float("nan"), "down_atr_percent"),
        ("down_atr_percent", float("inf"), "down_atr_percent"),
        ("down_atr_percent", -0.1, "down_atr_percent"),
    ],
)
def test_update_position_data_validates_complete_candidate_before_mutation(
    field,
    invalid_value,
    error_field,
):
    database = MagicMock()
    database.load_open_positions.return_value = {}
    manager = StockManager(MagicMock(), database, filter_config={})
    position = Position(
        id=1,
        stock_code="005930",
        stock_name="Samsung",
        buy_price=50_000.0,
        buy_time="2026-08-09 10:00:00",
        buy_regime="STABLE_BULL",
        sell_price=50_100.0,
        atr_percent=1.25,
        down_atr_percent=0.75,
    )
    manager.active_positions = {"005930": position}
    verdict = {
        "stock_code": "005930",
        "price": 51_000.0,
        "atr_percent": 2.5,
        "down_atr_percent": 1.5,
    }
    if field == "buy_price":
        if invalid_value is _MISSING:
            del position.buy_price
        else:
            position.buy_price = invalid_value
    elif invalid_value is _MISSING:
        verdict.pop(field)
    else:
        verdict[field] = invalid_value
    before = vars(position).copy()

    with pytest.raises(StrategySemanticsValidationError, match=error_field):
        manager.update_position_data(verdict)

    _assert_position_snapshot(position, before)


def test_cumulative_trade_return_score_is_simple_sum_for_unequal_prices():
    database = MagicMock()
    database.load_active_positions.return_value = {}
    manager = StockManager(MagicMock(), database, filter_config={})
    manager.active_positions = {
        "A": Position(
            id=1,
            stock_code="A",
            stock_name="A",
            buy_price=100.0,
            sell_price=110.0,
            buy_time="2026-08-03 10:00:00",
            buy_regime="STABLE_BULL",
        ),
        "B": Position(
            id=2,
            stock_code="B",
            stock_name="B",
            buy_price=100_000.0,
            sell_price=95_000.0,
            buy_time="2026-08-03 10:00:00",
            buy_regime="STABLE_BULL",
        ),
    }

    assert manager.calculate_cumulative_trade_return_score(1.25) == 6.25
    assert {
        "quantity",
        "notional",
        "fee",
        "fees",
        "tax",
        "currency",
        "weight",
    }.isdisjoint(vars(next(iter(manager.active_positions.values()))))


@pytest.mark.parametrize("value", [True, float("nan"), float("inf")])
def test_cumulative_trade_return_score_rejects_invalid_realized_score(value):
    database = MagicMock()
    database.load_active_positions.return_value = {}
    manager = StockManager(MagicMock(), database, filter_config={})

    with pytest.raises(StrategySemanticsValidationError):
        manager.calculate_cumulative_trade_return_score(value)
