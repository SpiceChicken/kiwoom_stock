# src/kiwoom_stock/core/physics_engine.py
import math
from typing import Dict, Any

# =========================================================
# Mathematical Dampers (기존 자산 재활용)
# =========================================================

def _sigmoid(x: float, k: float = 1.0) -> float:
    """[Helper] 안전한 시그모이드. 무한 발산을 막고 0~100 사이로 속도를 가둡니다."""
    x = max(-50.0, min(50.0, x))
    return 1.0 / (1.0 + math.exp(-x * k))

def _rational_penalty(excess: float, hardness: float = 2.0) -> float:
    """[Helper] 완만한 페널티 함수. 지수 감쇠보다 부드러운 브레이크 역할로 활용 가능합니다."""
    if excess <= 0: return 1.0
    return 1.0 / (1.0 + (excess / hardness) ** 2)

# =========================================================
# Physical Forces (단위 힘 계산)
# =========================================================

def _calculate_thrust_force(execution_strength_pct: float, vol_ratio: float, interval_amount_krw: float, reference_mass: float, norm_constant: float = 50.0) -> float:
    """
    [물리적 의도: Thrust & Mass]
    - 체결강도가 100%를 초과하는 잉여 에너지를 기본 추진력(Base Thrust)으로 삼습니다.
    - 거래대비(vol_ratio)를 물체의 '질량(Mass) 혹은 엔진 부스터'로 취급하여 추진력을 증폭시킵니다.
    - 틱 거래량이 극도로 적은 허위 체결강도 필터링
    """
    if execution_strength_pct <= 100.0:
        return 0.0
    
    # [Mass 검증] 10초간 거래대금이 1천만 원 미만인 경우, 질량($m$)이 부족한 빈 껍데기 가속도로 취급하여 페널티 부과
    mass_penalty = 1.0
    min_required_mass = reference_mass * 0.10
    if interval_amount_krw < min_required_mass:
        mass_penalty = interval_amount_krw / min_required_mass if min_required_mass > 0 else 0.0
    
    base_thrust = math.tanh((execution_strength_pct - 100.0) / norm_constant)
    vol_multiplier = min(2.0, max(1.0, math.log10(max(1.0, vol_ratio)) + 1.0))
    
    return base_thrust * vol_multiplier * mass_penalty

import math

def _calculate_gravity_force(current_price_krw: float, vwap_krw: float, atr_percent: float) -> float:
    """
    [물리적 의도: Absolute Gravity (이격도 기반 절대 중력)]
    - 주가가 당일 평균가(VWAP)에서 멀어질수록 진입을 방해하는 하방 압력(저항)이 연속적으로 강해진다.
    - 위로 멀어질 때: 고점 과열에 의한 중력 (Overheating)
    - 아래로 멀어질 때: 하락 추세 관성에 의한 늪 저항 (Falling Knife / Dead-cat Bounce 필터링)
    - 상수와 if-else 분기를 완벽히 제거한 순수 동역학 함수.
    """
    safe_vwap = max(vwap_krw, 1e-9)
    actual_atr_ratio = atr_percent / 100.0 
    safe_atr = max(actual_atr_ratio, 1e-5)
    
    sigma_price_krw = safe_vwap * safe_atr
    gap_krw = current_price_krw - safe_vwap
    
    # [V2.3] 양방향 이격에 대해 동일한 형태의 Tanh 저항력(0.0 ~ -1.0)을 연속적으로 산출
    return -math.tanh(abs(gap_krw) / sigma_price_krw)

def _calculate_drag_force(previous_velocity: float, rsi: float, friction_coefficient: float = 0.1) -> float:
    """
    [물리적 의도: Drag (Friction)] 공기 저항 및 RSI 과열 마찰.
    - RSI가 70을 초과하면 차익 실현 매물이 나오는 '마찰열' 현상을 수식에 반영하여 항력을 제곱비례로 키웁니다.
    - 이전 속도가 0 이하(하락 추세)일 경우, 저항력을 0으로 강제하여 역방향 가속을 차단합니다.
    """
    if previous_velocity <= 0.0:
        return 0.0
        
    overheat_factor = max(0.0, (rsi - 70.0) / 30.0)
    drag_multiplier = 1.0 + (overheat_factor ** 2)
    
    return -friction_coefficient * previous_velocity * drag_multiplier

def _calculate_magnetic_force(tot_sel_req: float, tot_buy_req: float, c_constant: float = 0.5) -> float:
    """
    [물리적 의도: Magnetic Force (Vacuum)] 
    - 매도잔량(매도벽)이 매수잔량보다 압도적으로 많을 때 발생하는 위쪽 호가의 진공 흡입력.
    """
    total_req = tot_sel_req + tot_buy_req
    if total_req <= 1e-9:
        return 0.0
        
    # 매도잔량이 많을수록 양수(+), 매수잔량이 많을수록 음수(-) 반환
    return c_constant * math.tanh((tot_sel_req - tot_buy_req) / total_req)

def _calculate_jerk_force(current_strength: float, prev_strength_5m: float, interval_amount_krw: float, reference_mass: float, norm_constant: float = 20.0) -> float:
    """
    [물리적 의도: Jerk (가속도의 미분)]
    - 체결강도(가속도) 자체가 증가하고 있는지(양의 Jerk) 감소하고 있는지(음의 Jerk) 판별.
    - 가짜 숏커버링(Bull Trap) 판별
    """
    if prev_strength_5m <= 1e-9: 
        return 0.0
    
    jerk_val = current_strength - prev_strength_5m
    
    # [Bull Trap 검증] 가속도가 증가(Positive Jerk)하는데 거래대금이 5백만 원 미만으로 메말랐다면 세력의 덫으로 간주
    if jerk_val > 0 and interval_amount_krw < (reference_mass * 0.05):
        return 0.0
        
    return math.tanh(jerk_val / norm_constant)

def _calculate_impulse(interval_impulse: float, current_price: float, previous_price: float, norm_constant: float = 10.0) -> float:
    """
    [물리적 의도: Impulse] 순간적인 대량 거래(충격량).
    - J = F * dt 로, 속도 벡터에 직접적인 스칼라 합산을 부여합니다.
    """
    if interval_impulse <= 0: return 0.0

    # 방향성 충격량: 틱 대금이 크더라도 가격이 오르지 못했다면 매도 폭탄!
    if current_price <= previous_price:
        return 0.0

    # 최대 5.0 단위의 순간 속도 부스트
    return math.tanh(interval_impulse / norm_constant) * 5.0

# =========================================================
# Integration (합력 및 속도 산출)
# =========================================================

def calculate_net_velocity(
    strength: float, current_price: float, vwap: float, atr_percent: float, previous_velocity: float,
    vol_ratio: float, rsi: float, tot_sel_req: float, tot_buy_req: float, prev_strength_5m: float,
    previous_price: float = 0.0,
    interval_impulse: float = 0.0, 
    interval_amount_krw: float = 0.0, 
    reference_mass: float = 10_000_000.0,
    friction_coefficient: float = 0.1
) -> Dict[str, float]:
    """
    [물리적 의도: V_t = V_{t-1} + F_net + J]
    """
    # 1. 벡터 힘 (Forces) 계산
    thrust = _calculate_thrust_force(strength, vol_ratio, interval_amount_krw, reference_mass)
    gravity = _calculate_gravity_force(current_price, vwap, atr_percent)
        
    drag = _calculate_drag_force(previous_velocity, rsi, friction_coefficient)
    magnetic = _calculate_magnetic_force(tot_sel_req, tot_buy_req)
    jerk = _calculate_jerk_force(strength, prev_strength_5m, interval_amount_krw, reference_mass)
    
    # 2. 합력 및 속도 산출
    net_force = thrust + gravity + drag + magnetic + jerk

    # Impulse 계산 시 방향성 판별을 위해 현재가와 직전가 전달
    impulse = _calculate_impulse(interval_impulse, current_price, previous_price)
    current_velocity = previous_velocity + net_force + impulse
    
    # 3. 상세 지표 반환
    return {
        "thrust": float(round(thrust, 4)), "gravity": float(round(gravity, 4)),
        "drag": float(round(drag, 4)), "magnetic": float(round(magnetic, 4)),
        "jerk": float(round(jerk, 4)), "impulse": float(round(impulse, 4)),
        "net_force": float(round(net_force, 4)), "current_velocity": float(round(current_velocity, 4))
    }