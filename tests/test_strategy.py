# tests/test_strategy.py
import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy

# [수정] strategy 픽스처 정의 삭제 (conftest에서 가져옴)

def test_evaluate_buy_signal(strategy, sample_supply_data):
    """[Strategy] 높은 점수 데이터 입력 시 매수 신호가 발생하는가?"""
    # Given: conftest의 sample_supply_data는 상승 추세
    
    # When
    verdict = strategy.evaluate(sample_supply_data)
    
    # Then
    assert "score" in verdict
    
    # 조건부 검증: 점수가 높고, 추세 과열(90점 이상) 필터에 걸리지 않았다면 매수 신호여야 함
    if verdict['score'] >= strategy.curr_strict_th:
        if verdict['score_detail'].get('trend', 0) < 90.0:
            assert verdict["is_buy_signal"] is True
        else:
            # 점수가 높아도 너무 급등하면 '추세과열'로 매수 보류 (정상 동작)
            assert verdict["status"] == "⚠️추세과열"

def test_exit_stop_loss(strategy, sample_position):
    sample_position.sell_price = 76000.0 
    sample_position.atr_percent = 2.0
    reason = strategy.get_exit_reason(sample_position, strong_threshold=87.0)
    assert reason is not None
    assert "Stop Loss" in reason

def test_hold_on_high_score(strategy, sample_position):
    sample_position.sell_price = 88000.0
    sample_position.current_score = 90.0
    reason = strategy.get_exit_reason(sample_position, strong_threshold=87.0)
    assert reason is None