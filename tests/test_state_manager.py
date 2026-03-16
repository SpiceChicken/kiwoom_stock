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

def test_downtrend_impulse_block_integration():
    """[파이프라인 타격] StateManager가 직전 가격을 정확히 추적하여 하락 틱의 Impulse를 차단하는가?"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    tracker._db_executor = MagicMock()

    # 1. 첫 틱 (가격 50000, 거래량 100)
    tracker.process_tick(
        stock_code="005930", strength=100, current_price=50000, vwap=50000, 
        atr_percent=1.5, vol_ratio=1.0, rsi=50, tot_sel_req=1000, tot_buy_req=1000, 
        total_volume=100.0, market_cap=50_000_000_000.0
    )
    
    # 2. 두 번째 틱 (대량 거래 터짐, 하지만 가격은 49000으로 하락)
    res_down = tracker.process_tick(
        stock_code="005930", strength=150, current_price=49000, vwap=50000, 
        atr_percent=1.5, vol_ratio=2.0, rsi=40, tot_sel_req=1000, tot_buy_req=1000, 
        total_volume=200.0, market_cap=50_000_000_000.0
    )
    
    # 거래량이 100 늘어 컷오프를 뚫었음에도, 가격이 하락했으므로 Impulse는 철저히 0.0이어야 함
    assert res_down["forces"]["impulse"] == 0.0

# =====================================================================
# 🛡️ V2.6 추가 테스트 (메모리 누수 차단 및 연료 고갈 감지 로직)
# =====================================================================

def test_sliding_window_max_length():
    """🔄 1. 120틱 슬라이딩 윈도우 한계 검증 (메모리 누수 차단)"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    tracker._db_executor = MagicMock()
    
    stock_code = "000660"
    total_vol = 0
    
    # 150번의 틱 데이터 강제 주입 (is_frozen을 피하기 위해 거래량 지속 증가)
    for i in range(150):
        total_vol += 10
        tracker.process_tick(
            stock_code=stock_code, strength=100.0, current_price=10000.0, 
            vwap=10000.0, atr_percent=0.5, vol_ratio=1.0, rsi=50.0, 
            tot_sel_req=1000, tot_buy_req=1000, total_volume=total_vol,
            market_cap=50_000_000_000.0
        )
    
    # 큐의 길이가 120을 절대 넘지 않는지(메모리 방어) 검증
    assert len(tracker._vol_history[stock_code]) == 120

def test_fuel_exhaustion_ratio_calculation():
    """⛽ 2. volume_drop_ratio 연산 검증"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    tracker._db_executor = MagicMock()
    
    stock_code = "000660"
    total_vol = 0
    
    # 과거 1분(60틱): 틱당 100주씩 엄청난 거래량 발생 (총 6000주)
    for _ in range(60):
        total_vol += 100
        tracker.process_tick(
            stock_code=stock_code, strength=100.0, current_price=10000.0, 
            vwap=10000.0, atr_percent=0.5, vol_ratio=1.0, rsi=50.0, 
            tot_sel_req=1000, tot_buy_req=1000, total_volume=total_vol,
            market_cap=50_000_000_000.0
        )
        
    # 최근 1분(60틱): 틱당 30주로 거래량 급감 (총 1800주)
    res = None
    for _ in range(60):
        total_vol += 30
        res = tracker.process_tick(
            stock_code=stock_code, strength=100.0, current_price=10000.0, 
            vwap=10000.0, atr_percent=0.5, vol_ratio=1.0, rsi=50.0, 
            tot_sel_req=1000, tot_buy_req=1000, total_volume=total_vol,
            market_cap=50_000_000_000.0
        )

    # ratio = 1800 / 6000 = 0.3 이 나와야 정상
    calculated_ratio = res["forces"].get("volume_drop_ratio", 0.0)
    assert 0.29 < calculated_ratio < 0.31, f"비율 연산 오류: {calculated_ratio}"