import pytest
import asyncio
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from kiwoom_stock.core.state_manager import PhysicalStateTracker

def test_time_decay_crash_recovery():
    """[상태 타격] DB에서 복구 시 Time-Decay(지수 감쇠) 적용 검증"""
    mock_db = MagicMock()
    # 2시간 전에 10.0의 속도로 종료되었다고 가정
    past_time = datetime.now() - timedelta(hours=2)
    mock_db.get_last_physical_state.return_value = {'velocity': 10.0, 'timestamp': past_time}
    
    tracker = PhysicalStateTracker(mock_db)
    tracker.recover_state_from_crash("005930", decay_constant=0.5)
    
    # V = 10.0 * exp(-0.5 * 2) = 10.0 * exp(-1) ≈ 3.678
    recovered_velocity = tracker._l1_cache["005930"]
    assert round(recovered_velocity, 2) == 3.68

def test_time_freeze_defense_volume_unchanged():
    """[병목/로직 타격] 거래량 동결 시 가속도 중첩 방지 검증"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    
    # 첫 틱 (거래량 100)
    res1 = tracker.process_tick(
        stock_code="005930", strength=110, current_price=50000, vwap=50000, atr_percent=1.5,
        vol_ratio=1.2, rsi=60, tot_sel_req=10000, tot_buy_req=5000, max_instant_amt_100m=10,
        current_volume=100.0 # 패치된 변수
    )
    
    # 두 번째 틱 (시간은 흘렀으나 거래량 100으로 동일)
    res2 = tracker.process_tick(
        stock_code="005930", strength=110, current_price=50000, vwap=50000, atr_percent=1.5,
        vol_ratio=1.2, rsi=60, tot_sel_req=10000, tot_buy_req=5000, max_instant_amt_100m=10,
        current_volume=100.0
    )
    
    # Assert: 거래량이 변하지 않았으므로 물리 엔진을 건너뛰고 이전 점수 유지, Forces 빈 딕셔너리 반환
    assert res1["total_score"] == res2["total_score"]
    assert res2["forces"] == {}

def test_jerk_5min_delay_queue():
    """[상태 타격] 5분(300초) 전 체결강도 큐 정상 배출 검증"""
    mock_db = MagicMock()
    tracker = PhysicalStateTracker(mock_db)
    
    now = datetime.now()
    # 과거 데이터 강제 주입 (350초 전, 250초 전, 현재)
    tracker._strength_history["005930"] = [
        (now - timedelta(seconds=350), 90.0), # 삭제되어야 함
        (now - timedelta(seconds=250), 105.0) # 5분 전 강도로 선택되어야 함
    ]
    
    prev_str = tracker._get_and_update_prev_strength("005930", 120.0)
    
    assert prev_str == 105.0
    # 350초 전 데이터는 Pop 되었어야 함
    history_times = [item[1] for item in tracker._strength_history["005930"]]
    assert 90.0 not in history_times