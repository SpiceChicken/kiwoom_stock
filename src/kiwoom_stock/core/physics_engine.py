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

def _calculate_thrust_force(execution_strength_pct: float, vol_ratio: float, norm_constant: float = 50.0) -> float:
    """
    [물리적 의도: Thrust & Mass]
    - 체결강도가 100%를 초과하는 잉여 에너지를 기본 추진력(Base Thrust)으로 삼습니다.
    - 거래대비(vol_ratio)를 물체의 '질량(Mass) 혹은 엔진 부스터'로 취급하여 추진력을 증폭시킵니다.
    """
    if execution_strength_pct <= 100.0:
        return 0.0
    
    base_thrust = math.tanh((execution_strength_pct - 100.0) / norm_constant)
    
    # 거래대비가 높을수록 가속도 증폭 (단, 극단적 이상치 방지를 위해 Log 스케일 적용)
    vol_multiplier = max(1.0, math.log10(max(1.0, vol_ratio)) + 1.0)

    # [Fix] 최대 증폭 2배 제한 (과도한 갭상승/폭발 제어)
    vol_multiplier = min(2.0, vol_multiplier)  
    
    return base_thrust * vol_multiplier

def _calculate_gravity_force(current_price_krw: float, vwap_krw: float, atr_percent: float) -> float:
    """
    [물리적 의도: Gravity] 
    - atr_percent: 1.5 등 퍼센트 단위로 입력받음.
    """
    safe_vwap = max(vwap_krw, 1e-9)
    # [수정] 백분율을 실제 비율로 변환 (1.5 -> 0.015)
    actual_atr_ratio = atr_percent / 100.0 
    safe_atr = max(actual_atr_ratio, 1e-5)
    
    sigma_price_krw = safe_vwap * safe_atr
    gap_krw = current_price_krw - safe_vwap
    
    return -math.tanh(gap_krw / sigma_price_krw)

def _calculate_drag_force(previous_velocity: float, rsi: float, friction_coefficient: float = 0.1) -> float:
    """
    [물리적 의도: Drag (Friction)] 공기 저항 및 RSI 과열 마찰.
    - RSI가 70을 초과하면 차익 실현 매물이 나오는 '마찰열' 현상을 수식에 반영하여 항력을 제곱비례로 키웁니다.
    """
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

def _calculate_jerk_force(current_strength: float, prev_strength_5m: float, norm_constant: float = 20.0) -> float:
    """
    [물리적 의도: Jerk (가속도의 미분)]
    - 체결강도(가속도) 자체가 증가하고 있는지(양의 Jerk) 감소하고 있는지(음의 Jerk) 판별.
    """
    if prev_strength_5m <= 1e-9: 
        return 0.0
    
    jerk_val = current_strength - prev_strength_5m
    return math.tanh(jerk_val / norm_constant)

def _calculate_impulse(max_amount: float, norm_constant: float = 10.0) -> float:
    """
    [물리적 의도: Impulse] 순간적인 대량 거래(충격량).
    - J = F * dt 로, 속도 벡터에 직접적인 스칼라 합산을 부여합니다.
    """
    if max_amount <= 0: return 0.0
    # 최대 5.0 단위의 순간 속도 부스트
    return math.tanh(max_amount / norm_constant) * 5.0

# =========================================================
# Integration (합력 및 속도 산출)
# =========================================================

def calculate_net_velocity(
    strength: float,
    current_price: float,
    vwap: float,
    atr_percent: float,
    previous_velocity: float,
    vol_ratio: float,
    rsi: float,
    tot_sel_req: float,
    tot_buy_req: float,
    prev_strength_5m: float,
    max_amount: float,
    friction_coefficient: float = 0.1
) -> Dict[str, float]:
    """
    [물리적 의도: V_t = V_{t-1} + F_net + J]
    """
    # 1. 벡터 힘 (Forces) 계산
    thrust = _calculate_thrust_force(strength, vol_ratio)
    gravity = _calculate_gravity_force(current_price, vwap, atr_percent)
    
    # [Fix] 추진력(Thrust)이 없을 때는 중력이 위로 끌어올리지 못하게(양수 불가) 차단 (한화생명 억지 진입 방지)
    if thrust <= 0.0:
        gravity = min(0.0, gravity)
        
    drag = _calculate_drag_force(previous_velocity, rsi, friction_coefficient)
    magnetic = _calculate_magnetic_force(tot_sel_req, tot_buy_req)
    jerk = _calculate_jerk_force(strength, prev_strength_5m)
    
    # 2. 합력 및 속도 산출
    net_force = thrust + gravity + drag + magnetic + jerk
    
    impulse = _calculate_impulse(max_amount)
    current_velocity = previous_velocity + net_force + impulse
    
    # 3. 상세 지표 반환
    return {
        "thrust": float(round(thrust, 4)),
        "gravity": float(round(gravity, 4)),
        "drag": float(round(drag, 4)),
        "magnetic": float(round(magnetic, 4)),
        "jerk": float(round(jerk, 4)),
        "impulse": float(round(impulse, 4)),
        "net_force": float(round(net_force, 4)),
        "current_velocity": float(round(current_velocity, 4))
    }

def calculate_physical_score(current_velocity: float) -> float:
    """
    [물리적 의도: Score 산출] 
    재활용한 시그모이드 함수를 통해 속도를 0~100 사이의 Score로 변환합니다.
    """
    return float(round(_sigmoid(current_velocity) * 100.0, 2))