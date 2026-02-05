"""
[Core] Scoring Logic Module
- 전략적 판단(Policy)이 아닌 순수 점수 계산(Calculation) 로직을 담당
- Strategy 클래스로부터 분리되어 테스트 용이성 확보
"""

import math
from typing import Dict, Tuple
from kiwoom_stock.core import indicators as ind
from kiwoom_stock.core.schema import SupplyData

def calculate_alpha_score(data: SupplyData, prev_score: float, decay: float) -> float:
    """[Alpha] 가격 가속도 및 탄력성 평가"""
    if len(data.price_series) < 6:
        return 0.0

    roc_1m = ind.calculate_roc(data.price_series[0], data.price_series[1])
    roc_5m = ind.calculate_roc(data.price_series[0], data.price_series[5])
    acceleration = roc_1m - (roc_5m / 10)

    # Vol Factor는 이미 Analyzer에서 계산됨 (data.vol_factor)
    # 하지만 로직 일관성을 위해 여기서 재확인하거나 data 값을 사용
    # 여기서는 data.vol_factor가 이미 계산되어 있다고 가정하거나 직접 계산 가능
    # *리팩토링 시 data.vol_factor 활용 추천*
    
    k = 25
    combined_input = acceleration * data.vol_factor
    
    try:
        current_alpha = 100 / (1 + math.exp(-combined_input * k))
    except OverflowError:
        current_alpha = 100.0 if combined_input > 0 else 0.0

    # 잔상 효과 적용 (이전 점수 활용)
    final_alpha = max(current_alpha, prev_score * decay)
    return float(round(final_alpha, 2))

def calculate_supply_score(data: SupplyData, prev_score: float, decay: float) -> float:
    """[Supply] 수급 주체 개입 강도"""
    base_score = max(0, min(100, 50 + (data.strength - 100) * 0.5))

    market_total_million = data.market_total_amount / 1000000
    if market_total_million < 10.0:
        pgm_adj, frgn_adj = 0, 0
    else:
        pgm_adj = max(-0.5, min(0.5, data.pgm_data.net_amt / market_total_million))
        frgn_adj = max(-0.5, min(0.5, data.foreign_data.netprps_prica / market_total_million))

    trust_factor = 1.0 if data.vol_ratio >= 5.0 else 0.5
    supply_impact = (pgm_adj + frgn_adj) * 5.0
    multiplier = 1.0 + (supply_impact * trust_factor)

    current_supply_score = min(100.0, base_score * multiplier)
    
    final_supply = max(current_supply_score, prev_score * decay)
    return float(round(final_supply, 2))

def calculate_vwap_score(data: SupplyData) -> float:
    """[VWAP] 가격 위치 및 기울기"""
    if data.vwap <= 0: return 0.0

    deviation = ind.calculate_disparity(data.price, data.vwap)
    overheat_limit = max(3.0, data.atr_percent * 1.5) 
    
    if deviation >= 0:
        ratio = deviation / overheat_limit
        pos_score = max(30.0, 100 * math.exp(-ratio))
    else:
        breakout_range = data.atr_percent * 0.2 
        ratio = max(-1.0, deviation / breakout_range)
        pos_score = 100 * (1 + ratio) * data.vol_factor

    if data.prev_vwap > 0 and data.vwap != data.prev_vwap:
        slope_intensity = max(-1.0, min(1.0, ind.calculate_slope(data.vwap, data.prev_vwap)))
        slope_factor = 1.0 + (slope_intensity * 0.4)
    else:
        slope_factor = 1.0

    return float(round(max(0, min(100, pos_score * slope_factor)), 2))

def calculate_trend_score(data: SupplyData) -> float:
    """[Trend] 이평선 정렬 및 과열 감지"""
    if data.ema60 <= 0: return 0.0

    gap_short = ind.calculate_disparity(data.ema5, data.ema20)
    gap_long = ind.calculate_disparity(data.ema20, data.ema60)
    
    energy_density = (gap_short + gap_long) / data.atr_percent 
    trend_ratio = math.tanh(energy_density)
    base_score = 50 + (trend_ratio * 50)

    total_dispersal = ind.calculate_disparity(data.ema5, data.ema60)
    dispersal_ratio = total_dispersal / data.atr_percent 
    
    overheat_factor = max(0.0, dispersal_ratio - 2.0)
    decay_penalty = math.exp(-overheat_factor * 0.5) 
    alignment_score = max(30.0, base_score * decay_penalty)

    if data.ema60 > 0:
        slope_intensity = max(-1.0, min(1.0, ind.calculate_slope(data.ema60, data.prev_ema60)))
        slope_factor = 1.0 + (slope_intensity * 0.2)
    else:
        slope_factor = 1.0

    return float(round(max(0, min(100, alignment_score * slope_factor)), 2))

def calculate_dynamic_weights(data: SupplyData) -> Dict[str, float]:
    """[Weights] 동적 가중치 계산"""
    imp_alpha = 1.0 * data.vol_factor
    imp_supply = 1.0 * data.vol_factor
    
    deviation = abs(ind.calculate_disparity(data.price, data.vwap)) if data.vwap > 0 else 0
    imp_vwap = 1.5 / (1 + (deviation / max(0.1, data.atr_percent)))

    gap1 = data.ema5 - data.ema20
    gap2 = data.ema20 - data.ema60
    denom = (abs(gap1) + abs(gap2))
    alignment_ratio = abs(gap1 + gap2) / denom if denom > 0 else 0.5
    is_ordered = 0.6 + (0.4 * alignment_ratio)

    raw_gap = abs(ind.calculate_disparity(data.ema5, data.ema60)) if data.ema60 > 0 else 0
    vol_multiple = ind.calculate_volatility_ratio(raw_gap, data.atr_percent) if data.atr_percent > 0 else 0

    if vol_multiple <= 1.5:
        expansion_factor = 1.0 + (vol_multiple * 0.1)
    elif vol_multiple <= 2.5:
        expansion_factor = 1.15
    else:
        expansion_factor = max(0.4, 1.15 - ((vol_multiple - 2.5) * 0.4))

    imp_trend = is_ordered * expansion_factor
    total_imp = imp_alpha + imp_supply + imp_vwap + imp_trend
    
    return {
        'alpha': imp_alpha / total_imp,
        'supply': imp_supply / total_imp,
        'vwap': imp_vwap / total_imp,
        'trend': imp_trend / total_imp
    }