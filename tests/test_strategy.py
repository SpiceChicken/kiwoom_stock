import pytest
from unittest.mock import MagicMock, patch
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

def test_momentum_calculation_and_entry_trigger(strategy):
    """[전략 타격] 모멘텀 기반 🔥강력추천 및 ⚠️고점경계 상태 검증"""
    mock_data = MagicMock(spec=SupplyData)
    mock_data.stock_code = "005930"
    mock_data.cur_prc = 50000
    mock_data.atr_percent = 1.5

    # 1. 상승 가속 (Momentum > 0)
    strategy.history["005930"] = [70.0, 75.0, 80.0] # Avg = 75.0
    mock_data.total_score = 88.0 # Score >= 85
    result_buy = strategy.evaluate(mock_data)
    
    assert result_buy["is_buy_signal"] is True
    assert result_buy["status"] == "🔥강력추천 (가속 돌파)"
    assert result_buy["momentum"] == 13.0 # 88.0 - 75.0

    # 2. 감속 경계 (Momentum < 0)
    strategy.history["005930"] = [95.0, 95.0, 95.0] # Avg = 95.0
    mock_data.total_score = 88.0 # Score >= 85
    result_warn = strategy.evaluate(mock_data)
    
    assert result_warn["is_buy_signal"] is False
    assert result_warn["status"] == "⚠️고점경계 (감속 중)"
    assert result_warn["momentum"] == -7.0 # 88.0 - 95.0

@patch('kiwoom_stock.monitoring.strategy.datetime') # [추가] 시공간 고정
def test_dynamic_stop_loss(mock_datetime, strategy):
    """[전략 타격] ATR 기반 동적 청산 로직 검증"""
    
    # 전략 엔진이 장 중(12시)이라고 믿게 만듭니다.
    from datetime import datetime
    mock_datetime.now.return_value = datetime(2026, 2, 10, 12, 0, 0)
    mock_datetime.combine.side_effect = datetime.combine # 원래 기능 유지

    """[전략 타격] ATR 기반 동적 청산 로직 검증"""
    pos = Position(
        id=1, 
        stock_code="005930", 
        stock_name="삼성전자",
        buy_price=10000, 
        buy_score=88.0, 
        buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL"
    )
    pos.sell_price = 9500 # -5% 손실 중
    pos.atr_percent = 1.0 # 동적 손절선: -(1.0 * 3.0)/100 = -0.03 (-3%)
    pos.current_score = 80.0
    
    # 5% 손실은 3% 손절선을 이탈했으므로 즉각 Stop Loss 발동해야 함
    exit_reason = strategy.get_exit_reason(pos, strong_threshold=85.0)
    assert "Stop Loss" in exit_reason