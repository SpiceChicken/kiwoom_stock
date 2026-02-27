import pytest
import math
from kiwoom_stock.core.physics_engine import calculate_net_velocity

def get_neutral_physics_params():
    """다른 물리적 힘을 0에 가깝게 통제한 Zero-Constant 기본 파라미터 셋"""
    return {
        "strength": 100.0,
        "current_price": 50000,
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
        "reference_mass": 10_000_000.0 # 기본 컷오프 1천만 원
    }

class TestPhysicsZeroConstant:
    """[동역학 타격] Zero-Constant 아키텍처 질량(Mass) 필터링 검증"""

    def test_spoofing_mass_penalty_thrust(self):
        """[질량 타격] 거래대금이 컷오프의 10% 미만일 때 Thrust 페널티가 부여되는가?"""
        params = get_neutral_physics_params()
        params["strength"] = 120.0
        params["reference_mass"] = 100_000_000.0 # 초대형주 컷오프 1억 가정
        
        # Case 1: 강도는 치솟았으나 거래대금이 고작 500만 원 (10%인 1천만 원 미만) -> 페널티 부과 대상
        params["interval_amount_krw"] = 5_000_000.0
        res_penalty = calculate_net_velocity(**params)
        
        # Case 2: 동일 강도에 거래대금 2,000만 원 (10% 이상) -> 정상 반영
        params["interval_amount_krw"] = 20_000_000.0
        res_normal = calculate_net_velocity(**params)
        
        assert res_penalty["thrust"] < res_normal["thrust"], "가짜 수급(Spoofing) 시 Thrust는 강력한 페널티를 받아 깎여야 합니다."

    def test_bull_trap_filter_jerk(self):
        """[덫 타격] 가속도는 올랐으나 거래대금이 컷오프의 5% 미만이면 Jerk(Bull Trap)가 0.0으로 차단되는가?"""
        params = get_neutral_physics_params()
        params["strength"] = 150.0
        params["prev_strength_5m"] = 100.0 # +50% 급가속
        params["reference_mass"] = 100_000_000.0 # 컷오프 1억
        
        # Case 1: 거래대금 300만 원 (5%인 500만 원 미만) -> 전형적인 세력의 호가창 장난(Bull Trap)
        params["interval_amount_krw"] = 3_000_000.0
        res_trap = calculate_net_velocity(**params)
        assert res_trap["jerk"] == 0.0, "거래대금 없는 억지 가속은 덫(Trap)으로 간주되어 Jerk가 0.0으로 차단되어야 합니다."
        
        # Case 2: 거래대금 1,000만 원 (5% 이상) -> 진짜 수급
        params["interval_amount_krw"] = 10_000_000.0
        res_real = calculate_net_velocity(**params)
        assert res_real["jerk"] > 0.0, "진짜 수급 동반 시 Jerk가 양수로 발산해야 합니다."