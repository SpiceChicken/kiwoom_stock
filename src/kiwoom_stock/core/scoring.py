"""
[Core] Scoring Logic Module
- Features: Uses dynamically loaded config.SCORING_CONFIG
- Logic: Log-Geometric Mean, Input Safety, Rational Penalty
"""

import math
import logging
from typing import Dict, Any

# [핵심] 동적 로더가 포함된 config 모듈 import
from kiwoom_stock.core import config 
from kiwoom_stock.core import indicators as ind
from kiwoom_stock.core.schema import SupplyData

logger = logging.getLogger(__name__)

# =========================================================
# Helper Functions (Safety & Math)
# =========================================================

def _sigmoid(x: float, k: float = 1.0) -> float:
    """[Helper] 안전한 시그모이드 (Overflow 방지)"""
    x = max(-50.0, min(50.0, x))
    return 1.0 / (1.0 + math.exp(-x * k))

def _rational_penalty(excess: float, hardness: float = 2.0) -> float:
    """[Helper] 완만한 페널티 함수 (Rational Decay)"""
    if excess <= 0:
        return 1.0
    return 1.0 / (1.0 + (excess / hardness) ** 2)

def _safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """[Helper] 0으로 나누기 방지"""
    if abs(denominator) < 1e-9:
        return default
    return numerator / denominator

# =========================================================
# Scoring Functions
# =========================================================

def calculate_alpha_score(data: SupplyData) -> float:
    """
    [Alpha] 적응형 가속도 평가 (Physics-based Net Force)
    * Logic: Thrust(추진력) - Gravity(중력) - Drag(매물저항) - [New] Overheat(과열저항)
    * Update: RSI 과열을 scoring 단계에서 '내부 저항'으로 처리하여 점수를 스스로 깎음
    """
    cfg = config.SCORING_CONFIG['alpha']

    # 1. 추진력(Thrust) 계산
    # ---------------------------------------------------
    raw_vol_ratio = data.vol_ratio
    vol_r = (raw_vol_ratio / 100.0) if raw_vol_ratio > 50.0 else raw_vol_ratio
    vol_r = max(0.1, vol_r)

    rsi_v = getattr(data, 'trend_rsi', 50.0)
    pwr_v = data.strength

    # [Normalization]
    norm_vol = math.log(vol_r) / cfg['std_vol']
    norm_mom = (rsi_v - 50.0) / cfg['std_rsi']
    norm_pow = (pwr_v - 100.0) / cfg['std_pwr']

    # [Combination]
    vol_influence = math.tanh(norm_vol) * cfg['w_vol_scale']
    w_vol = max(0.05, min(0.8, cfg['w_vol_base'] + vol_influence))
    remain = 1.0 - w_vol
    
    # thrust: 순수 상승 에너지
    thrust = (w_vol * norm_vol) + (remain * 0.6 * norm_mom) + (remain * 0.4 * norm_pow)


    # 2. 저항력(Resistance) 계산: 중력 + 공기저항 + [New] 과열
    # ---------------------------------------------------
    resistance_force = 0.0
    atr = max(0.5, getattr(data, 'atr_percent', 1.5))
    
    # A. 중력 (Gravity): EMA60 역배열 저항
    if data.ema60 > 0 and data.cur_prc < data.ema60:
        dist = data.ema60 - data.cur_prc
        gap_sigma = dist / (data.cur_prc * atr / 100.0)
        gravity = math.log(1.0 + gap_sigma) * cfg['w_resistance']
        resistance_force += gravity

    # B. 공기저항 (Drag): VWAP 붕괴 저항
    if data.vwap > 0 and data.cur_prc < data.vwap:
        dist = data.vwap - data.cur_prc
        gap_sigma = dist / (data.cur_prc * atr / 100.0)
        drag = math.sqrt(gap_sigma) * cfg['w_support']
        resistance_force += drag

    # C. [New] 과열 저항 (Overheat Resistance)
    # RSI가 80을 넘어가면 '엔진 과열'로 보아 저항이 급격히 발생
    # RSI 80: 저항 0
    # RSI 85: 저항 발생
    # RSI 90: 추진력을 상쇄할 만큼 강력한 저항
    if rsi_v > 80.0:
        excess_heat = rsi_v - 80.0
        # 제곱으로 페널티를 주어 90 넘으면 점수 급락 유도
        thermal_drag = (excess_heat / 5.0) ** 2.0  
        resistance_force += thermal_drag
        
        # 로깅 (필요시)
        # logger.debug(f"[{data.stock_name}] RSI Overheat({rsi_v}): Drag={thermal_drag:.2f}")


    # 3. 알짜 힘 (Net Force) 및 점수 산출
    # ---------------------------------------------------
    net_force = thrust - resistance_force
    
    # Adaptive Slope
    raw_k = 12.0 / (0.5 + (atr * 0.5))
    adaptive_k = max(cfg['k_min'], min(cfg['k_max'], raw_k))
    
    final_score = _sigmoid(net_force, k=adaptive_k) * 100.0
    
    return float(round(final_score, 2))


def calculate_supply_score(data: SupplyData) -> float:
    """
    [Supply] 수급 강도 (Unit Fix)
    * Fix: 순매수 금액(Won)을 백만원 단위로 변환하여 거래대금(Million)과 비율 매칭
    * Logic: 체결강도(Base) * 수급 임팩트(Multiplier)
    """
    cfg = config.SCORING_CONFIG['supply']

    # 1. Base Score (체결강도 기반)
    # 100% -> 50점, 150% -> 75점, 200% -> 100점
    base_score = max(0.0, min(100.0, 50.0 + (data.strength - 100.0) * 0.5))

    # 2. Market Cap Reliability (거래대금 규모)
    # amt_mil: 백만 원 단위 (예: 10,000 = 100억원)
    amt_mil = data.trde_qty * data.vwap / 1000000.0
    
    if amt_mil <= cfg['min_amt_mil']:
        return 0.0

    # 3. Supply Impact (Net Buy Ratio)
    net_buy_ratio = 0.0
    if amt_mil > 0:
        if not data.pgm_data: pgm_net = 0.0
        else: pgm_net = getattr(data.pgm_data, 'netprps_prica', 0.0)

        if not data.foreign_data: frgn_net = 0.0
        else: frgn_net = getattr(data.foreign_data, 'netprps_prica', 0.0)
        
        # 거래대금 대비 순매수 비중 계산
        pgm_ratio = pgm_net / amt_mil
        frgn_ratio = frgn_net / amt_mil
        
        # [Smart Money] 외국인 수급에 1.1배 가중치 부여
        net_buy_ratio = pgm_ratio + (frgn_ratio * 1.1)
    
    # 4. Final Multiplier
    # 부정적 수급: 최소 0.5배 (점수 하락)
    multiplier = max(cfg['multiplier_floor'], 1.0 + net_buy_ratio)
    
    current_supply_score = min(100.0, base_score * multiplier)
    return float(round(current_supply_score, 2))


def calculate_vwap_score(data: SupplyData) -> float:
    """[VWAP] 지지력 + 오버슈팅 방어"""
    cfg = config.SCORING_CONFIG['vwap']

    if data.vwap <= 0: return 50.0
    
    # 1. Z-Score
    raw_gap_pct = (data.cur_prc - data.vwap) / data.vwap * 100.0
    
    if not hasattr(data, 'atr_percent') or data.atr_percent is None:
        atr = cfg['atr_default']
    else:
        atr = max(0.5, data.atr_percent)

    adaptive_scale = atr * 2.0
    z_score = _safe_div(raw_gap_pct, adaptive_scale)
    
    # 2. Base Score
    damped_z = z_score * 0.5
    base_score = 50.0 + (math.tanh(damped_z) * 50.0)
    
    # 3. Overheat Penalty
    threshold = 3.0
    if z_score > threshold:
        overheat = z_score - threshold
        overheat = min(overheat, cfg['overheat_cap'])
        penalty = _rational_penalty(overheat, hardness=cfg['penalty_hardness'])
    else:
        penalty = 1.0
        
    final_score = base_score * penalty
    return float(round(max(0.0, min(100.0, final_score)), 2))


def calculate_trend_score(data: SupplyData) -> float:
    """[Trend] 효율성 + 클라이맥스 방지"""
    cfg = config.SCORING_CONFIG['trend']
    eps = cfg['eps']

    if data.ema60 <= eps: return 50.0

    # 1. Vectors & Path
    vec_p_s = data.cur_prc - data.ema5
    vec_s_m = data.ema5 - data.ema20
    vec_m_l = data.ema20 - data.ema60
    
    net_move = (data.cur_prc - data.ema60)
    total_path = abs(vec_p_s) + abs(vec_s_m) + abs(vec_m_l)
    
    # 2. Efficiency
    efficiency = _safe_div(abs(net_move), max(total_path, eps), default=0.0)
    
    # 3. Strength Z-Score
    atr = max(0.5, getattr(data, 'atr_percent', 1.5))
    safe_ema60 = max(data.ema60, eps)
    net_move_pct = (net_move / safe_ema60) * 100.0
    z_score = _safe_div(net_move_pct, atr * 2.0)
    
    # 4. Base Score
    adjusted_strength = z_score * efficiency
    base_score = _sigmoid(adjusted_strength, k=1.0) * 100.0

    # 5. Climax Penalty
    threshold = cfg['climax_threshold']
    if z_score > threshold:
        climax = z_score - threshold
        penalty = _rational_penalty(climax, hardness=cfg['penalty_hardness'])
    else:
        penalty = 1.0
        
    final_score = base_score * penalty
    return float(round(final_score, 2))


def calculate_dynamic_weights(data: SupplyData) -> Dict[str, float]:
    """[Weights] 동적 가중치"""
    # 1. 거래량 가중치
    safe_vol = max(0.0, min(data.vol_factor, 10.0))
    imp_alpha = 1.0 * safe_vol
    imp_supply = 1.0 * safe_vol
    
    # 2. VWAP 이격도 가중치
    if data.vwap > 0:
        deviation = abs(ind.calculate_disparity(data.cur_prc, data.vwap))
        denom = max(0.1, getattr(data, 'atr_percent', 1.0))
        imp_vwap = 1.5 / (1.0 + (deviation / denom))
    else:
        imp_vwap = 1.0

    # 3. 추세 정배열 가중치
    gap1 = data.ema5 - data.ema20
    gap2 = data.ema20 - data.ema60
    denom = abs(gap1) + abs(gap2)
    
    alignment_ratio = _safe_div(abs(gap1 + gap2), denom, default=0.5)
    imp_trend = math.log(1.0 + math.exp(alignment_ratio)) * 1.5

    # 4. Total Sum Safety
    total_imp = imp_alpha + imp_supply + imp_vwap + imp_trend
    total_imp = max(total_imp, 1e-6)
    
    return {
        'alpha': imp_alpha / total_imp,
        'supply': imp_supply / total_imp,
        'vwap': imp_vwap / total_imp,
        'trend': imp_trend / total_imp
    }


def calculate_total_score(
    alpha: float, 
    supply: float, 
    vwap: float, 
    trend: float,
    weights: Dict[str, float]
) -> dict:
    """[Total] 종합 점수 (Log-Geometric Mean)"""
    expected_keys = ['alpha', 'supply', 'vwap', 'trend']
    for k in expected_keys:
        if k not in weights:
            # 기본 가중치로 보정
            weights[k] = 0.25

    sum_w = sum(weights.values())
    if sum_w <= 1e-9:
        norm_w = {k: 0.25 for k in expected_keys}
    else:
        norm_w = {k: v / sum_w for k, v in weights.items()}

    scores = {
        'alpha': max(1.0, alpha),
        'supply': max(1.0, supply),
        'vwap': max(1.0, vwap),
        'trend': max(1.0, trend)
    }
    
    log_sum = 0.0
    for k in expected_keys:
        w = norm_w.get(k, 0.25)
        s = scores[k]
        log_sum += w * math.log(s)
        
    final_score = math.exp(log_sum)
    
    return {
        "total_score": round(final_score, 1),
        "weights": norm_w
    }