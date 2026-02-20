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
    * Logic: Thrust(추진력) - Gravity(중력) - Drag(매물저항) - Overheat(과열저항)
    * Update: RSI 과열을 scoring 단계에서 '내부 저항'으로 처리하여 점수를 스스로 깎음
    """
    cfg = config.SCORING_CONFIG['alpha']

    # 1. 데이터 전처리 (단위 통일)
    # ---------------------------------------------------
    raw_vol = data.vol_ratio # 거래대비
    rsi_v = data.trend_rsi # RSI
    pwr_v = data.strength # 체결강도

    # 2. 핵심 지표 계산 (Tanh Scaling)
    # ---------------------------------------------------
    
    # A. 거래량 (Volume): 거래비율 - cfg['alpha_volumn'](100)
    # Scale: 200% 차이를 1.0으로 봄 (즉, 300% 터지면 tanh(1.0)=0.76점)
    # 100% -> 0점, 300% -> 0.76점, 500% -> 0.96점 (포화)
    norm_vol = math.tanh((raw_vol - cfg['alpha_volumn']) / 100.0)

    # B. RSI (Momentum): cfg['alpha_rsi'](75) - RSI (75 이하는 점수 상승, 75 이상은 하락)
    # RSI > 75 -> 과열 페널티
    norm_mom = math.tanh((cfg['alpha_rsi'] - rsi_v) / 15.0)

    # C. 체결강도 (Power): 체결강도 - cfg['alpha_power'](100)
    # Scale: 50% 차이를 1.0으로 봄
    # 100% -> 0점, 150% -> (150-100) / 50 = 1.0 -> tanh(1.0) = 0.76점
    norm_pow = math.tanh((pwr_v - cfg['alpha_power']) / 50)
    
    # thrust: 단순 평균 (Average)
    thrust = (norm_vol + norm_mom + norm_pow) / 3.0

    # 3. 저항력(Resistance) 계산: 중력 + 공기저항 + 과열
    # ---------------------------------------------------
    resistance_force = 0.0
    atr_ratio = getattr(data, 'atr_percent', 0.015)
    atr_ratio = max(0.005, atr_ratio) # 0.5% 미만은 0.5%로 보정 (Divide by Zero 방지)
    
    # A. 중력 (Gravity): EMA60 역배열
    if data.ema60 > 0 and data.cur_prc < data.ema60:
        dist = data.ema60 - data.cur_prc
        # Sigma = 주가 * 변동성비율(ATR)
        sigma = data.cur_prc * atr_ratio 
        gap_sigma = dist / sigma
        
        # 가중치 2.0 (Config 대체)
        gravity = math.sqrt(gap_sigma)
        resistance_force += gravity

    # B. 공기저항 (Drag): VWAP 하회
    if data.vwap > 0 and data.cur_prc < data.vwap:
        dist = data.vwap - data.cur_prc
        sigma = data.cur_prc * atr_ratio
        gap_sigma = dist / sigma
        
        drag = math.sqrt(gap_sigma)
        resistance_force += drag

    # 5. 알짜 힘 (Net Force) 및 점수 산출
    # ---------------------------------------------------
    net_force = thrust - resistance_force
    
    final_score = _sigmoid(net_force, k=atr_ratio * cfg['atr_leverage']) * 100.0
    # print(f"stock: {data.stock_code}, norm_vol: {norm_vol:.2f}, norm_mom: {norm_mom:.2f}, norm_pow {norm_pow:.2f}, net_force: {net_force:.2f}, final_score: {final_score:.2f}")
    
    return float(round(final_score, 2))

def calculate_supply_score(data: SupplyData) -> float:
    """
    [Supply] 수급 폭발력 (Aggressive Buying Pressure)
    - 목적: 실질적인 단기 시세 분출을 일으키는 '공격적 매수세' 포착
    - 변경사항: 프로그램/외인 가중치 제거 -> 단순 합산 (Smart Money Sum)
    """
    cfg = config.SCORING_CONFIG['supply']
    
    # 1. 체결강도 (Execution Strength) - 단기 시세의 핵심 엔진
    # ------------------------------------------------------------------
    # Physics: 100% 이하는 '정지/후퇴'로 간주하여 과감히 0점 처리.
    # 100%를 초과하는 '잉여 힘(Excess Power)'에 대해서만 점수 부여.
    
    raw_strength = data.strength
    
    if raw_strength <= 100.0:
        strength_score = 0.0
    else:
        # Tanh Scale: 100을 기점으로 50단위 증가 시마다 점수 포화
        # 150% -> tanh(1.0) ≈ 76점
        strength_score = math.tanh((raw_strength - 100.0) / 50.0) * 100.0


    # 2. 스마트 머니 침투율 (Smart Money Penetration)
    # ------------------------------------------------------------------
    # Physics: '누가 샀는가'에 가중치를 두지 않고, '얼마나 관여했는가(Density)'만 측정.
    # 거래대금 대비 주포(기관/외인)의 순매수 비중 계산.
    
    # 거래대금 추정 (백만 원 단위)
    amt_mil = (data.trde_qty * data.vwap) / 1_000_000.0
    
    # 데이터 방어: 거래대금 1억 미만은 신뢰도 부족으로 패스
    if amt_mil < cfg['min_amt_mil']: 
        smart_score = 0.0
    else:
        pgm_net = getattr(data.pgm_data, 'netprps_prica', 0.0)
        frgn_net = getattr(data.foreign_data, 'netprps_prica', 0.0)
        
        # [Refactored] 가중치 제거 (Unbiased Sum)
        # 프로그램이든 외국인이든 '돈의 힘'은 평등하다고 가정.
        smart_net = pgm_net + frgn_net
        
        # 침투율 (Penetration Ratio)
        # 전체 거래대금 중 메이저 자금이 차지하는 비중
        penetration = smart_net / amt_mil
        
        # Tanh Scale: 거래대금의 5%(0.05) 이상을 주포가 샀다면 강력한 신호
        # 0.05 -> tanh(1.0) ≈ 76점 / 음수(순매도)면 마이너스 점수
        smart_score = math.tanh(penetration * 20.0) * 100.0


    # 3. 최종 결합 (Combination with Quality Filter)
    # ------------------------------------------------------------------
    
    # Base: 체결강도 (즉각적인 시세 반응)
    final_score = strength_score
    
    # Boost: 스마트 머니가 받쳐주면 점수 증폭
    # 상승 국면(점수>0)일 때만 반영 (하락 중에 외인이 사봤자 물 타기일 수 있음)
    if final_score > 0:
        # 가중치 없이 스마트 머니 점수를 30% 비중으로 혼합
        # 예: 체결강도 만점(100) + 외인 매수 만점(100) -> 100점
        # 예: 체결강도 만점(100) + 외인 폭풍 매도(-100) -> 40점 (신뢰도 하락)
        final_score = (final_score * 0.7) + (max(-50.0, smart_score) * 0.3)
    
    # Filter: 거래량 퀄리티 (Volume Quality)
    # 거래량이 전일 동시간 대비 50% 미만이면 가짜 상승(허매수) 가능성 -> 점수 반토막
    vol_ratio = data.vol_ratio
    if vol_ratio < 50.0:
        final_score *= 0.5
    elif vol_ratio > 200.0:
        # 거래량 200% 이상 폭발 시 가산점 (최대 10점)
        bonus = math.log10(vol_ratio / 100.0) * 10.0 
        final_score += min(10.0, bonus)

    return float(round(max(0.0, min(100.0, final_score)), 2))

def calculate_vwap_score(data: SupplyData) -> float:
    """
    [VWAP] 동적 탄성 도약 (Dynamic Trampoline Effect)
    - 목적: VWAP를 발판 삼아 튀어 오르는 '위치(Sweet Spot)'와 '힘(Strength)'의 결합
    - 특징: 체결강도와 가우스 곡선을 이용해 모멘텀 보너스를 동적으로 스케일링
    """
    if getattr(data, 'vwap', 0) <= 0: return 50.0
    
    disparity_pct = (data.cur_prc - data.vwap) / data.vwap * 100.0
    
    # ------------------------------------------------------------------
    # 1. 위치 에너지 (Position Score)
    # ------------------------------------------------------------------
    if disparity_pct < -0.5:
        # VWAP 아래로 완전히 가라앉음 (익사)
        return 0.0
    elif disparity_pct < 0:
        # 수면 아래 돌파 직전 (30~50점)
        pos_score = 30.0 + (disparity_pct + 0.5) * 40.0
    else:
        # 상승 구간: Tanh 기울기를 완화(0.33)하여 너무 쉽게 점수가 오르지 않게 제어
        # 이격도 3% 수준에서 약 88점 도달
        pos_score = 50.0 + math.tanh(disparity_pct * 0.33) * 50.0

    
    # ------------------------------------------------------------------
    # 2. 동적 운동 에너지 보너스 (Dynamic Momentum Bonus) (수정됨)
    # ------------------------------------------------------------------
    momentum_bonus = 0.0
    if disparity_pct > 0:
        # A. 가우스 최적점 (Gaussian Peak): 이격도 1.0% 부근에서 탄성 극대화 (초입 잡기)
        # 1.0%에서 1.0(Max)을 주고, 멀어질수록 부드럽게 감소하도록 조정
        gaussian_peak = math.exp(-0.5 * ((disparity_pct - 1.0) / 1.0) ** 2)
        
        # B. 힘 승수 (Strength Multiplier)
        raw_strength = getattr(data, 'strength', 100.0)
        strength_multiplier = max(0.0, math.tanh((raw_strength - 100.0) / 50.0))
        
        momentum_bonus = 15.0 * gaussian_peak * strength_multiplier

    
    # ------------------------------------------------------------------
    # 3. 과열 제어 (Overheat Control)
    # ------------------------------------------------------------------
    penalty = 1.0
    if disparity_pct > 5.0:
        # 5% 초과분 1%당 10% 감점 (추격매수 방지)
        excess = disparity_pct - 5.0
        penalty = max(0.0, 1.0 - (excess * 0.1))


    final_score = (pos_score + momentum_bonus) * penalty
    
    return float(round(max(0.0, min(100.0, final_score)), 2))

def calculate_trend_score(data: SupplyData) -> float:
    """
    [Trend] 발사각 및 가속도 (Launch Angle & Divergence)
    - 목적: 단기 이평선이 중기 이평선을 뚫고 폭발적으로 확산하는 '각도' 측정
    - 변경사항: 효율성/클라이맥스 페널티 제거 -> 단기 이격 확산도 중심의 공격적 로직
    """
    
    # 데이터 유효성 검사
    if data.ema20 <= 0 or data.ema5 <= 0: return 50.0

    # 1. 단기 추세 방향 (Direction)
    # ------------------------------------------------------------------
    # Physics: 단기(5)가 중기(20) 아래에 있다면 하락 파동이 진행 중인 것.
    # 단타 엔진에서는 떨어지는 칼날의 반등을 노리지 않음. 가차 없이 0점.
    if data.ema5 < data.ema20:
        return 0.0

    # 2. 발사각 측정 (Divergence = Launch Angle)
    # ------------------------------------------------------------------
    # EMA5와 EMA20이 얼마나 가파르게 벌어지고 있는가? (%)
    divergence_pct = (data.ema5 - data.ema20) / data.ema20 * 100.0

    # Tanh Scale 적용 (S-Curve)
    # 이격도가 0% ~ 3%로 벌어지는 구간을 집중적으로 점수화
    # 1.5% 벌어졌을 때 -> tanh(1.0) ≈ 76점 (본격적인 슈팅)
    # 3.0% 벌어졌을 때 -> tanh(2.0) ≈ 96점 (폭주 상태)
    angle_score = math.tanh(divergence_pct / 1.5) * 100.0


    # 3. 주가 위치 보정 (Riding the Trend)
    # ------------------------------------------------------------------
    # 이평선(평균)은 오르고 있어도, 현재 주가가 꺾이면 선행 지표로서 위험함.
    price_to_ema5 = (data.cur_prc - data.ema5) / data.ema5 * 100.0

    if price_to_ema5 < 0:
        # 단기 생명선(EMA5)을 아래로 깼다면, 추세가 꺾이기 시작한 것 (조정 진입)
        # 발사각 점수를 즉시 50% 삭감 (급브레이크)
        angle_score *= 0.5
        
    elif price_to_ema5 > 3.0:
        # 주가가 EMA5보다 3% 이상 높으면 순간적인 갭(음봉 꽂힐 확률) 리스크가 있음.
        # 기존 로직처럼 '클라이맥스 페널티'를 주어 점수를 폭락시키진 않지만,
        # 약간의 마찰력(Drag)을 주어 최고점 도달을 살짝 지연시킴 (90%만 반영)
        angle_score *= 0.9

    return float(round(max(0.0, min(100.0, angle_score)), 2))

def calculate_dynamic_weights(data: SupplyData) -> Dict[str, float]:
    """
    [Weights] 동적 가중치 (3-Factor 전술 엔진)
    - 특징: Alpha 더미 데이터 삭제. 순수 Trigger(S, V, T) 비율만 반환
    """
    vol_multiplier = 1.0 + math.log10(max(1.0, data.vol_factor))
    imp_supply = 1.2 * vol_multiplier

    imp_vwap = 1.0
    if data.vwap > 0:
        disparity_pct = (data.cur_prc - data.vwap) / data.vwap * 100.0
        if disparity_pct > 3.0:
            imp_vwap = max(0.5, 1.0 - (disparity_pct - 3.0) * 0.1)

    imp_trend = 1.0
    if data.ema20 > 0 and data.ema5 > data.ema20:
        divergence_pct = (data.ema5 - data.ema20) / data.ema20 * 100.0
        imp_trend += math.tanh(divergence_pct / 2.0) * 0.5

    total_imp = max(imp_supply + imp_vwap + imp_trend, 1e-6)
    
    # 딱 3가지만 반환 (alpha 제거)
    return {
        'supply': float(round(imp_supply / total_imp, 3)),
        'vwap': float(round(imp_vwap / total_imp, 3)),
        'trend': float(round(imp_trend / total_imp, 3))
    }


def calculate_total_score(scores: Dict[str, float], weights: Dict[str, float]) -> dict:
    """
    [Total] 전술 종합 점수 (Log-Geometric Mean / 3-Factor Model)
    - 특징: 개별 변수 대신 scores 딕셔너리를 받아 내부에서 필요한 키만 추출.
    """
    # Alpha는 여기서 아예 제외하고 3개만 타겟팅합니다.
    expected_keys = ['supply', 'vwap', 'trend'] 
    default_w = 1.0 / 3.0
    
    # 1. 가중치 정규화 (Normalization)
    sum_w = sum(weights.get(k, 0.0) for k in expected_keys)
    if sum_w <= 1e-9:
        norm_w = {k: default_w for k in expected_keys}
    else:
        norm_w = {k: weights.get(k, 0.0) / sum_w for k in expected_keys}

    # 2. 로그-기하 평균 산출 (scores 딕셔너리에서 바로 추출)
    log_sum = 0.0
    for k in expected_keys:
        w = norm_w[k]
        # scores 딕셔너리에서 값을 꺼내되, 없으면 기본값 1.0 (Log(0) 방지)
        s = max(1.0, scores.get(k, 1.0))
        log_sum += w * math.log(s)
        
    final_score = math.exp(log_sum)
    
    return {
        "total_score": round(final_score, 1),
        "weights": norm_w
    }
def apply_deep_analysis_bonus(total_score: float, deep_data: Dict) -> float:
    """
    [Deep Dive] 정밀 분석 (호가/고래) 결과 반영 (Proportional Logic)
    * Update: 상수(if-else) 제거 -> 연속 함수(Log/Tanh) 적용
    * Logic:
        1. 호가 잔량: Log2 스케일 적용 (2배 차이마다 ±5% 변동)
        2. 고래 체결: Tanh 스케일 적용 (체결액 비례 가산, 자연스러운 포화)
    """
    final_score = total_score
    
    sell_total = deep_data.get('sell_total', 0)
    buy_total = deep_data.get('buy_total', 0)
    whale_found = deep_data.get('whale_found', False)
    whale_vol = deep_data.get('whale_vol', 0.0)

    # 1. 호가 잔량 분석 (Order Book Imbalance)
    # Logarithmic Scale: 비율이 기하급수적으로 커져도 점수는 선형적으로 반영
    if buy_total > 0 and sell_total > 0:
        # Ratio = 매도잔량 / 매수잔량
        # 1.0 (균형) -> log2(1) = 0 -> 변동 없음
        # 2.0 (매도우위) -> log2(2) = +1 -> +5% Boost
        # 0.5 (매수우위) -> log2(0.5) = -1 -> -5% Penalty
        ratio = sell_total / buy_total
        
        # log2 적용 (비율의 대칭성 확보)
        log_ratio = math.log2(ratio)
        
        # 계수 설정: 2배 차이당 5% 변동 (0.05)
        # 상/하한 캡: 최대 ±15% (변동폭 제한)
        imbalance_factor = max(-0.15, min(0.15, log_ratio * 0.05))
        
        final_score *= (1.0 + imbalance_factor)

    # 2. 고래 체결 분석 (Whale Impact)
    # Hyperbolic Tangent Scale: 금액이 커질수록 가산폭이 체감(Diminishing Return)
    if whale_found and whale_vol > 0:
        # whale_vol 단위: 억 원
        # tanh(x * k): k는 민감도. 
        # 10억 체결 시 -> tanh(10 * 0.1) = tanh(1.0) ≈ 0.76 (76% 반영)
        # 50억 체결 시 -> tanh(50 * 0.1) = tanh(5.0) ≈ 0.99 (99% 반영)
        
        # Max Boost: 15% (0.15)
        # 1억 체결: 0.1 * 0.15 = 1.5% 가산
        # 10억 체결: 0.76 * 0.15 = 11.4% 가산
        # 30억 이상: 거의 15% 가산 (포화)
        
        whale_boost = math.tanh(whale_vol * 0.1) * 0.15
        final_score *= (1.0 + whale_boost)
        
    return float(round(min(100.0, final_score), 2))