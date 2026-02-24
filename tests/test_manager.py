import pytest
from unittest.mock import MagicMock
from kiwoom_stock.monitoring.manager import StockManager, Position

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
        buy_score=80.0, buy_time="2026-02-10", buy_regime="BULL"
    )
    manager.active_positions = {"005930": mock_pos}
    
    verdict = {
        "stock_code": "005930",
        "price": 51000,
        "score": 90.0,
        "atr_percent": 2.0,
    }
    
    updated_pos = manager.update_position_data(verdict)
    
    # Assert: 순수하게 속성값만 갱신되었는지 확인
    assert updated_pos is not None
    assert updated_pos.sell_price == 51000
    assert updated_pos.current_score == 90.0
    assert updated_pos.atr_percent == 2.0