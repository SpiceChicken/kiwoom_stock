import pytest
import math
from kiwoom_stock.core.physics_engine import (
    _calculate_gravity_force,
    _calculate_impulse,
    calculate_physical_score,
    calculate_net_velocity
)

def test_gravity_zero_division_and_scaling_defense():
    """[수학적 타격] ZeroDivision 및 단위 스케일링 안전성 검증"""
    current_price = 10150
    # 엣지 케이스: VWAP이나 ATR이 0으로 수렴할 때
    vwap_extreme = 0.0
    atr_percent_extreme = 0.0
    
    force_extreme = _calculate_gravity_force(
        current_price_krw=current_price, 
        vwap_krw=vwap_extreme, 
        atr_percent=atr_percent_extreme
    )
    
    # Assert: 발산(NaN, Inf)하지 않고 안전하게 계산되어야 함
    assert isinstance(force_extreme, float)
    assert not math.isnan(force_extreme)
    assert not math.isinf(force_extreme)

    # 스케일링 검증 (1.5% -> 0.015)
    force_normal = _calculate_gravity_force(current_price_krw=10150, vwap_krw=10000, atr_percent=1.5)
    # sigma = 10000 * 0.015 = 150. gap = 150. tanh(1) = 0.7615... 방향은 아래로(-) -> -0.7615
    assert -0.8 < force_normal < -0.7

def test_impulse_boost_limit():
    """[수학적 타격] 극한의 순간 거래대금에도 부스트가 Clamping 되는지 검증"""
    extreme_amount = 999999.0 # 억 단위 폭발
    impulse = _calculate_impulse(extreme_amount)
    
    # Assert: 아무리 커도 최대 5.0을 초과해선 안 됨
    assert 4.9 < impulse <= 5.0

def test_sigmoid_score_clamping():
    """[수학적 타격] Net Force가 오버플로우 급일 때 최종 점수가 0~100에 갇히는지 검증"""
    extreme_velocity_positive = 1000.0
    extreme_velocity_negative = -1000.0
    
    score_pos = calculate_physical_score(extreme_velocity_positive)
    score_neg = calculate_physical_score(extreme_velocity_negative)
    
    assert score_pos == 100.0
    assert score_neg == 0.0