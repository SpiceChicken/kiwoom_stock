# tests/test_manager.py
import pytest
from unittest.mock import MagicMock
from kiwoom_stock.monitoring.manager import StockManager

@pytest.fixture
def manager(mock_db, strategy, mock_strategy_config):
    client = MagicMock() # API 클라이언트 Mock
    # filter config
    config = {"max_stocks": 10, "cooldown_minutes": 10}
    
    mgr = StockManager(client, mock_db, strategy, config)
    # 종목명 매핑 수동 주입
    mgr.stock_names["005930"] = "삼성전자"
    return mgr

def test_process_buy_order(manager):
    """[Manager] 매수 주문 처리 후 포지션이 생성되는가?"""
    # Given
    verdict = {
        "stock_code": "005930",
        "price": 80000.0,
        "score": 88.5,
        "regime": "안정적 강세장",
        "forces": {
            "thrust": 1.25,
            "gravity": -0.85,
            "drag": -0.15,
            "magnetic": 0.40,
            "jerk": 0.80,
            "impulse": 0.0,
            "net_force": 1.45,
            "current_velocity": 2.50
        }
    }
    
    # When
    success, data = manager.process_buy_order(verdict)
    
    # Then
    assert success is True
    assert "005930" in manager.active_positions
    # DB 저장 메서드가 호출되었는지 확인
    manager.db.record_buy.assert_called_once()
    
    # 생성된 포지션 객체 검증
    pos = manager.active_positions["005930"]
    assert pos.buy_price == 80000.0
    assert pos.status == "OPEN"

def test_process_sell_order(manager, sample_position):
    """[Manager] 매도 주문 처리 후 포지션이 목록에서 제거되는가?"""
    # Given: 이미 보유 중인 상태 설정
    manager.active_positions["005930"] = sample_position
    
    verdict = {"stock_code": "005930", "price": 82000.0, "score": 60.0}
    
    # When
    success, pos = manager.process_sell_order(verdict, reason="Take Profit")
    
    # Then
    assert success is True
    assert "005930" not in manager.active_positions # 목록에서 사라져야 함
    manager.db.record_sell.assert_called_once()
    assert pos.sell_price == 82000.0
    assert pos.sell_reason == "Take Profit"