# [PATCH] tests/test_physics_engine.py 전면 덮어쓰기

import pytest
import math
from kiwoom_stock.core.physics_engine import calculate_net_velocity

def get_neutral_physics_params():
    """다른 물리적 힘을 0에 가깝게 통제한 Zero-Constant 기본 파라미터 셋"""
    return {
        "strength": 100.0,
        "current_price": 50000,
        "previous_price": 50000, # 💥 방향성 판독을 위해 직전가 추가
        "vwap": 50000,
        "atr_percent": 1.0,
        "previous_velocity": 0.0,
        "vol_ratio": 1.0,
        "rsi": 50.0,
        "tot_sel_req": 1000,
        "tot_buy_req": 1000,
        "prev_strength_5m": 100.0,
        "interval_impulse": 0.0,
        "interval_amount_krw": 0.0,
        "reference_mass": 10_000_000.0 
    }

class TestPhysicsZeroConstant:
    """[동역학 타격] Zero-Constant 아키텍처 질량(Mass) 필터링 검증"""

    def test_spoofing_mass_penalty_thrust(self):
        """[질량 타격] 거래대금이 컷오프의 10% 미만일 때 Thrust 페널티가 부여되는가?"""
        params = get_neutral_physics_params()
        params["strength"] = 120.0
        params["reference_mass"] = 100_000_000.0
        
        params["interval_amount_krw"] = 5_000_000.0
        res_penalty = calculate_net_velocity(**params)
        
        params["interval_amount_krw"] = 20_000_000.0
        res_normal = calculate_net_velocity(**params)
        
        assert res_penalty["thrust"] < res_normal["thrust"], "가짜 수급 시 Thrust는 강력한 페널티를 받아 깎여야 합니다."

    def test_bull_trap_filter_jerk(self):
        """[덫 타격] 거래대금이 컷오프의 5% 미만이면 Jerk(Bull Trap)가 0.0으로 차단되는가?"""
        params = get_neutral_physics_params()
        params["strength"] = 150.0
        params["prev_strength_5m"] = 100.0
        params["reference_mass"] = 100_000_000.0
        
        params["interval_amount_krw"] = 3_000_000.0
        res_trap = calculate_net_velocity(**params)
        assert res_trap["jerk"] == 0.0
        
        params["interval_amount_krw"] = 10_000_000.0
        res_real = calculate_net_velocity(**params)
        assert res_real["jerk"] > 0.0

    def test_directional_impulse_blocking(self):
        """[방향성 타격] 거래대금이 터져도 가격이 오르지 못했다면(음봉/보합) Impulse가 차단되는가?"""
        params = get_neutral_physics_params()
        params["interval_impulse"] = 20.0 # 엄청난 충격량 주입
        
        # Case 1: 가격 상승 (정상 반영)
        params["current_price"] = 51000
        params["previous_price"] = 50000
        res_up = calculate_net_velocity(**params)
        assert res_up["impulse"] > 0.0
        
        # Case 2: 가격 하락 (음봉 폭탄 -> Impulse 0)
        params["current_price"] = 49000
        params["previous_price"] = 50000
        res_down = calculate_net_velocity(**params)
        assert res_down["impulse"] == 0.0