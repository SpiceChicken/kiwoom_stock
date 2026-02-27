import pytest
from unittest.mock import MagicMock
from kiwoom_stock.core.state_manager import PhysicalStateTracker

def test_time_freeze_defense_volume_unchanged():
    """[병목/로직 타격] 거래량 동결 시 감쇠(Drag) 검증"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    tracker._db_executor = MagicMock()

    # 1. 첫 틱 (정상 가속)
    # 자기력(Magnetic)을 0으로 만들기 위해 매수/매도 잔량을 1000:1000으로 설정
    res1 = tracker.process_tick(
        stock_code="005930", strength=110, current_price=50000, vwap=50000, atr_percent=1.5,
        vol_ratio=1.2, rsi=60, 
        tot_sel_req=1000, tot_buy_req=1000, # 💥 자기력 중립
        total_volume=100.0, market_cap=50_000_000_000.0
    )
    
    # 2. 두 번째 틱 (거래량 동결 -> 엔진 꺼짐)
    res2 = tracker.process_tick(
        stock_code="005930", strength=110, current_price=50000, vwap=50000, atr_percent=1.5,
        vol_ratio=1.2, rsi=60, 
        tot_sel_req=1000, tot_buy_req=1000, # 💥 자기력 중립
        total_volume=100.0, market_cap=50_000_000_000.0
    )

    # 엔진이 꺼지고 마찰력만 남았으므로 점수는 반드시 떨어져야 함
    assert res2["forces"].get("impulse", 0.0) == 0.0

def test_time_freeze_and_weber_fechner_law():
    """[상태 타격] 시총 기반 로그 스케일 충격량 검증"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    tracker._db_executor = MagicMock()

    # 1. 초기화
    tracker.process_tick(
        stock_code="005930", strength=100, current_price=50000, vwap=50000, 
        atr_percent=1.5, vol_ratio=1.0, rsi=50, tot_sel_req=1000, tot_buy_req=1000, 
        total_volume=100.0, market_cap=50_000_000_000.0
    )
    
    # 2. 미미한 거래량 증가 (소형주 컷오프 미달)
    res_small = tracker.process_tick(
        stock_code="005930", strength=110, current_price=50000, vwap=50000, 
        atr_percent=1.5, vol_ratio=1.2, rsi=60, tot_sel_req=1000, tot_buy_req=1000, 
        total_volume=110.0, market_cap=50_000_000_000.0
    )
    assert res_small["forces"]["impulse"] == 0.0