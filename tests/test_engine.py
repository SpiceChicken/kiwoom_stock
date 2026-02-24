import pytest
from unittest.mock import MagicMock
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.manager import Position

def test_process_decisions_pipeline():
    """[흐름 제어 타격] Engine -> Manager(업데이트) -> Strategy(판단) 오케스트레이션 검증"""
    mock_client = MagicMock()
    engine = TradingEngine(mock_client, {})
    
    mock_manager = MagicMock()
    mock_strategy = MagicMock()
    mock_notifier = MagicMock()

    engine.stock_mgr = mock_manager
    engine.strategy = mock_strategy
    engine.notifier = mock_notifier
    
    mock_updated_pos = MagicMock(spec=Position)
    mock_manager.active_positions = {"005930": mock_updated_pos}
    mock_manager.update_position_data.return_value = mock_updated_pos
    
    # 언패킹 폭발 방지
    mock_manager.process_sell_order.return_value = (True, mock_updated_pos)
    mock_manager.process_buy_order.return_value = (True, mock_updated_pos)
    
    mock_strategy.get_exit_reason.return_value = "Kinetic Exit (Velocity Drop)"
    
    verdict = {
        "stock_code": "005930", 
        "price": 80000, 
        "forces": {"net_force": -3.5, "current_velocity": 7.0}
    }
    
    # [Execute] 파이프라인 가동
    engine._process_decisions([verdict])
    
    # Assert 1: Manager 데이터 갱신
    mock_manager.update_position_data.assert_called_once_with(verdict)
    
    # Assert 2: Strategy 청산 판단
    mock_strategy.get_exit_reason.assert_called_once_with(
        mock_updated_pos, 80000, {"net_force": -3.5, "current_velocity": 7.0}
    )
    
    # Assert 3: Manager 매도 집행
    mock_manager.process_sell_order.assert_called_once_with(verdict, "Kinetic Exit (Velocity Drop)")
    
    # Assert 4: 💥 Notifier에 알림 발송이 정상적으로 지시되었는지까지 검증 (파이프라인의 종착지)
    mock_notifier.notify_sell.assert_called_once_with(mock_updated_pos)