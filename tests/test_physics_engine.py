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

def get_neutral_physics_params():
    """다른 물리적 힘(Thrust, Gravity 등)을 0에 가깝게 통제한 중립 파라미터 셋"""
    return {
        "strength": 100.0,
        "current_price": 50000,
        "vwap": 50000,         # Vwap과 현재가가 같아 Gravity 최소화
        "atr_percent": 1.0,
        "previous_velocity": 0.0,
        "vol_ratio": 1.0,
        "rsi": 50.0,           # RSI 50으로 Thrust 중립화
        "tot_sel_req": 1000,
        "tot_buy_req": 1000,
        "prev_strength_5m": 100.0,
        "max_amount": 0.0
    }

class TestPhysicsForcesLogic:
    """[동역학 타격] Magnetic, Jerk, Impulse 3대 코어 힘 수식 검증"""

    def test_magnetic_force_logic(self):
        """[Magnetic] 매도/매수 잔량 비율에 따른 흡입력 검증"""
        params = get_neutral_physics_params()
        
        # 1. 상승 흡입력 (매도 잔량이 매수 잔량보다 압도적으로 많을 때)
        params["tot_sel_req"] = 100000  # 위로 꽉 찬 매도벽
        params["tot_buy_req"] = 5000
        res_up = calculate_net_velocity(**params)
        assert res_up["magnetic"] > 0, "매도 잔량이 많을 때 Magnetic은 양수(상승 방향)여야 합니다."

        # 2. 하락 압력 (매수 잔량이 압도적으로 많을 때 -> 허매수/투매 전조)
        params["tot_sel_req"] = 5000
        params["tot_buy_req"] = 100000 # 아래로 꽉 찬 매수벽
        res_down = calculate_net_velocity(**params)
        assert res_down["magnetic"] < 0, "매수 잔량이 많을 때 Magnetic은 음수(하락 방향)여야 합니다."

        # 3. ZeroDivision 방어 (동시호가/VI 장외 시간 등 호가 0)
        params["tot_sel_req"] = 0
        params["tot_buy_req"] = 0
        res_zero = calculate_net_velocity(**params)
        assert res_zero["magnetic"] == 0.0, "잔량이 0일 때 발산하지 않고 0.0으로 방어해야 합니다."

    def test_jerk_force_logic(self):
        """[Jerk] 5분 전 대비 체결강도 변화에 따른 가속도 검증"""
        params = get_neutral_physics_params()

        # 1. 급가속 (5분 전 100% -> 현재 150%)
        params["strength"] = 150.0
        params["prev_strength_5m"] = 100.0
        res_accel = calculate_net_velocity(**params)
        assert res_accel["jerk"] > 0, "체결강도가 급증하면 Jerk는 양수여야 합니다."

        # 2. 급감속 (5분 전 150% -> 현재 90%)
        params["strength"] = 90.0
        params["prev_strength_5m"] = 150.0
        res_decel = calculate_net_velocity(**params)
        assert res_decel["jerk"] < 0, "체결강도가 급감하면 Jerk는 음수여야 합니다."

        # 3. 등속도 (변화 없음)
        params["strength"] = 120.0
        params["prev_strength_5m"] = 120.0
        res_constant = calculate_net_velocity(**params)
        assert res_constant["jerk"] == 0.0, "체결강도 변화가 없으면 Jerk는 0.0이어야 합니다."

    def test_impulse_force_logic(self):
        """[Impulse] 순간 체결 대금(억 단위)에 따른 충격량 검증"""
        params = get_neutral_physics_params()

        # 1. 노이즈 거래 (0.5억 = 5천만 원) -> 엔진에 따라 0이거나 매우 미미한 값이어야 함
        params["max_amount"] = 0.5
        res_noise = calculate_net_velocity(**params)
        # 2. 유의미한 고래 타격 (20억)
        params["max_amount"] = 20.0
        res_whale = calculate_net_velocity(**params)
        
        assert res_whale["impulse"] > res_noise["impulse"], "20억 체결이 5천만 원보다 큰 충격량을 발생시켜야 합니다."
        assert res_whale["impulse"] > 0, "유의미한 대금 체결 시 Impulse는 양수여야 합니다."

        # 3. 극한의 폭발 방어 (1000억 체결 시 Clamping이 걸리는지 확인)
        params["max_amount"] = 1000.0
        res_extreme = calculate_net_velocity(**params)
        
        # 엔진 내부 로직(tanh 등)에 의해 최대치가 제한되어 있어야 함 (보통 5.0 미만)
        assert res_extreme["impulse"] <= 5.0, "극한의 거래 대금이 들어와도 Impulse 점수가 Clamping(상한선 방어) 되어야 합니다."