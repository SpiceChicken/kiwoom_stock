import logging
import math
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional

from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.core import indicators as ind # [Refactor] 계산 로직 모듈 임포트
from .analyzer import MarketRegime

# utils에서 설정한 로깅 핸들러 사용
logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    [Strategy] 트레이딩 전략 및 점수 산출 엔진 (v2.3 Refactored)
    
    [개선 사항]
    1. SoC 적용: 수학적 지표 계산을 core.indicators 모듈로 위임
    2. 차등 진입 전략: Primary Driver에 따라 진입 임계값 이원화 (Supply 우대)
    3. 과열 방지: Trend Score 90점 이상 시 진입 차단 (수익률 역상관 반영)
    4. Config 기반: 하드코딩 제거 및 설정 파일 동적 로드
    """
    
    def __init__(self, strategy_config: Dict):
        """전략 객체 초기화 및 기본 설정 로드"""
        self.settings = strategy_config

        # [Test Support] 디버그 모드 설정 로드 (기본값 False)
        self.debug_mode = strategy_config.get("debug_mode", False)
        
        if self.debug_mode:
            logger.warning("🚨 [DEBUG MODE] TIME CHECKS ARE BYPASSED! (장외 시간 테스트 모드 작동)")

        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)

        # [최적화] 시간 문자열 파싱 캐싱
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        
        # [Rule] 장 마감 3분 전 강제 청산 시간 설정
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        # 내부 상태 변수
        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config = {}
        
        # [Thresholds] 초기 안전장치 (Config 로드 전 Default 값)
        # 분석 결과에 따라 Strict(87.0)와 Supply(82.0)로 이원화
        self.curr_strict_th = 87.0  # Trend/Alpha 주도 시 엄격 기준
        self.curr_supply_th = 82.0  # Supply 주도 시 완화 기준
        self.curr_alert_th = 75.0   # 모니터링 시작 기준

        # 매매 규칙 로드 (익절, 손절, 감쇠율)
        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

        # 모멘텀 계산용 이력 저장소
        self.history = {} 

        # 리스크 관리 (Kill Switch & Deadline)
        self.total_loss_limit = strategy_config.get("total_loss_limit", -5)
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        # [Memory] 지표 잔상 효과 저장소 (호가 공백 보정)
        self._alpha_memory: Dict[str, float] = {}
        self.alpha_decay = strategy_config.get("alpha_decay", 0.8)
        self._supply_memory: Dict[str, float] = {}
        self.supply_decay = strategy_config.get("supply_decay", 0.8)

    def update_context(self, regime: MarketRegime):
        """
        [Context] 시장 레짐 변경에 따라 임계값 설정을 동적으로 업데이트
        """

        # [Test Support] 디버그 모드일 경우: 시장 레짐을 무시하고 테스트용 임계값 강제 적용
        if self.debug_mode:
            # 중복 로깅 방지: 이미 디버그 모드로 설정되어 있다면 패스
            if self._current_regime != "DEBUG_MODE":
                self._current_regime = "DEBUG_MODE"
                
                # 설정 파일에서 debug_thresholds 로드 (없으면 기본값 50.0)
                debug_th = self.settings.get("debug_thresholds", {})
                self.curr_strict_th = debug_th.get("strong", 50.0)
                self.curr_supply_th = debug_th.get("strong_supply", 50.0)
                self.curr_alert_th = debug_th.get("alert", 40.0)
                
                logger.warning(f"🚨 [DEBUG MODE] Thresholds Fixed for TEST: Strict={self.curr_strict_th} / Supply={self.curr_supply_th}")
            return # 이후 로직(레짐별 설정 로드) 수행 안 함

        regime_val = regime.value if hasattr(regime, 'value') else str(regime)
        
        if self._current_regime != regime_val:
            self._current_regime = regime_val
            regimes = self.settings.get("regimes", {})
            self._cached_config = regimes.get(regime_val, regimes.get("default", {}))
            
            # [Config Load] 설정 파일에서 임계값 로드 (하드코딩 방지)
            config_th = self._cached_config.get("thresholds", {})
            
            # 설정값 우선 적용, 없으면 __init__의 기본값(Safe Default) 유지
            self.curr_strict_th = config_th.get('strong', 87.0)
            self.curr_supply_th = config_th.get('strong_supply', 82.0)
            self.curr_alert_th = config_th.get('alert', 75.0)
            
            logger.info(f"Strategy Context Updated: {regime_val} | Strict: {self.curr_strict_th}, Supply: {self.curr_supply_th}")

    @property
    def entry_thresholds(self) -> Dict[str, float]:
        """현재 적용 중인 임계값 정보 반환"""
        return {
            "strong": self.curr_strict_th,
            "strong_supply": self.curr_supply_th,
            "alert": self.curr_alert_th
        }
    
    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        """
        [Exit Logic] 청산 조건 판별
        1. Time Cut: 15:27 강제 청산
        2. Stop Loss: 손절선 도달
        3. Take Profit: 목표가 도달 (단, 점수 유지 시 홀딩)
        4. Score Decay: 점수 급락 시 이탈 (수익권에선 더 민감하게 반응)
        """
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        
        # 1. 시간 청산
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        # 2. 손절매
        if profit_rate <= self.stop_loss_rate:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        # 3. 익절매 (Trailing Logic)
        if profit_rate >= self.target_profit_rate:
            # 점수가 여전히 높다면(현재 기준) 홀딩하여 수익 극대화
            if pos.current_score >= strong_threshold:
                return None 
            return f"Take Profit (+{profit_rate*100:.1f}%)"

        # 4. 점수 하락 감지 (Score Decay)
        current_decay = self.decay_rate
        # 수익 중일 때는 이익 보전을 위해 민감도 1.5배 증가
        if profit_rate >= 0.01:
            current_decay *= 1.5
            
        relative_threshold = pos.buy_score * (1 - current_decay)
        absolute_threshold = self.curr_alert_th

        final_sell_threshold = min(relative_threshold, absolute_threshold)

        if pos.current_score < final_sell_threshold:
            return f"Score Decay (-{current_decay*100:.0f}%)"
        
        return None

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        """계좌 전체 손실 한도 체크"""
        return total_pnl <= self.total_loss_limit

    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (주말 제외)"""
        # [Test Support] 디버그 모드면 무조건 True 반환
        if self.debug_mode:
            return True

        now = datetime.now()
        if now.weekday() >= 5: return False
        return time(8, 30) <= now.time() <= self.exit_time_obj

    def is_trading_window(self) -> bool:
        """신규 진입 가능 시간 체크 (15:00 마감)"""
        # [Test Support] 디버그 모드면 무조건 True 반환
        if self.debug_mode:
            return True
            
        now = datetime.now().time()
        return now < self.deadline_time
        
    def _calculate_conviction_score(self, metrics: Dict) -> Tuple[float, Dict]:
        """
        [Scoring Engine] 승산형(Multiplicative) 모델 점수 산출
        """
        # 1. 4대 지표 산출 (indicators 모듈 활용)
        a_score = max(1.0, self._calculate_alpha_score(metrics))
        s_score = max(1.0, self._calculate_supply_score(metrics))
        v_score = max(1.0, self._calculate_vwap_score(metrics))
        t_score = max(1.0, self._calculate_trend_score(metrics))

        # 2. 동적 가중치 계산
        w = self._calculate_dynamic_weights(metrics)

        # 3. 가중 기하평균 적용 (하나라도 0점에 가까우면 총점 급락)
        final_score = (
            math.pow(a_score, w.get('alpha', 0.25)) *
            math.pow(s_score, w.get('supply', 0.25)) *
            math.pow(v_score, w.get('vwap', 0.25)) *
            math.pow(t_score, w.get('trend', 0.25))
        )

        details = {
            "alpha": round(a_score, 1),
            "supply": round(s_score, 1),
            "vwap": round(v_score, 1),
            "trend": round(t_score, 1),
        }
        
        return round(final_score, 1), details

    def _calculate_alpha_score(self, metrics: Dict) -> float:
        """[Alpha] 가격 가속도 및 거래량 파워 (indicators 활용)"""
        stock_code = metrics['stock_code']
        price_series = metrics.get('price_series', [])
        vol_series = metrics.get('volume_series', [])

        if len(price_series) < 6:
            return 0.0

        # [Refactor] indicators 모듈을 사용하여 변화율 계산
        roc_1m = ind.calculate_roc(price_series[0], price_series[1])
        roc_5m = ind.calculate_roc(price_series[0], price_series[5])
        
        # 가속도: 최근 1분 상승분이 5분 추세 대비 얼마나 강력한가
        acceleration = roc_1m - (roc_5m / 10)

        # [Refactor] indicators 모듈을 사용하여 거래량 비율 계산
        # 현재 거래량이 5분 평균 대비 2배 이상이면 최대 가점
        vol_factor = min(2.0, ind.calculate_volume_ratio(vol_series[0], vol_series[1:6]))

        # 점수화 (Sigmoid)
        k = 25
        combined_input = acceleration * vol_factor
        try:
            current_alpha = 100 / (1 + math.exp(-combined_input * k))
        except OverflowError:
            current_alpha = 100.0 if combined_input > 0 else 0.0

        # 잔상 효과 (Memory) 적용
        prev_alpha = self._alpha_memory.get(stock_code, 0.0)
        final_alpha = max(current_alpha, prev_alpha * self.alpha_decay)
        self._alpha_memory[stock_code] = final_alpha

        return float(round(final_alpha, 2))

    def _calculate_supply_score(self, metrics: Dict) -> float:
        """[Supply] 수급 주체 개입 강도 (기존 로직 유지 + Memory)"""
        stock_code = metrics['stock_code']
        strength = metrics.get('strength', 100.0)
        pgm_net = metrics.get('pgm_net', 0)
        frgn_net = metrics.get('frgn_net', 0)
        market_total = metrics.get('market_total_amount', 1)
        vol_ratio = metrics.get('vol_ratio', 0)

        # 체결강도 기반 베이스 점수
        base_score = max(0, min(100, 50 + (strength - 100) * 0.5))

        # 대형주 수급 보정
        market_total_million = market_total / 1000000
        if market_total_million < 10.0:
            pgm_adj, frgn_adj = 0, 0
        else:
            pgm_adj = max(-0.5, min(0.5, pgm_net / market_total_million))
            frgn_adj = max(-0.5, min(0.5, frgn_net / market_total_million))

        # 거래량 신뢰도 가중
        trust_factor = 1.0 if vol_ratio >= 5.0 else 0.5
        supply_impact = (pgm_adj + frgn_adj) * 5.0
        multiplier = 1.0 + (supply_impact * trust_factor)

        current_supply_score = min(100.0, base_score * multiplier)

        # 잔상 효과 적용
        prev_supply = self._supply_memory.get(stock_code, 0.0)
        final_supply = max(current_supply_score, prev_supply * 0.85)
        self._supply_memory[stock_code] = final_supply

        return float(round(final_supply, 2))

    def _calculate_vwap_score(self, metrics: Dict) -> float:
        """[VWAP] 가격 위치 및 기울기 (indicators 활용)"""
        vwap = metrics.get('vwap', 0)
        price = metrics.get('price', 0)
        vol_factor = metrics.get('vol_factor', 1.0)
        prev_vwap = metrics.get('prev_vwap', 0)
        atr_p = metrics.get('atr_percent', 3.0)

        if vwap <= 0: return 0.0

        # [Refactor] 이격도 계산 위임
        deviation = ind.calculate_disparity(price, vwap)
        overheat_limit = max(3.0, atr_p * 1.5) 
        
        if deviation >= 0:
            # 상단 과열 체크
            ratio = deviation / overheat_limit
            pos_score = max(30.0, 100 * math.exp(-ratio))
        else:
            # 하단 돌파 체크
            breakout_range = atr_p * 0.2 
            ratio = max(-1.0, deviation / breakout_range)
            pos_score = 100 * (1 + ratio) * vol_factor

        # [Refactor] 기울기 계산 위임
        if prev_vwap > 0 and vwap != prev_vwap:
            slope_intensity = max(-1.0, min(1.0, ind.calculate_slope(vwap, prev_vwap)))
            slope_factor = 1.0 + (slope_intensity * 0.4)
        else:
            slope_factor = 1.0

        final_vwap_score = pos_score * slope_factor
        return float(round(max(0, min(100, final_vwap_score)), 2))

    def _calculate_trend_score(self, metrics: Dict) -> float:
        """[Trend] 이평선 정렬 및 과열 감지 (indicators 활용)"""
        e5 = metrics.get('ema5', 0)
        e20 = metrics.get('ema20', 0)
        e60 = metrics.get('ema60', 0)
        prev_e60 = metrics.get('prev_ema60', e60)
        atr_p = metrics.get('atr_percent', 3.0) 

        if e60 <= 0: return 0.0

        # [Refactor] 이평선 간 이격도 계산 위임
        gap_short = ind.calculate_disparity(e5, e20)
        gap_long = ind.calculate_disparity(e20, e60)
        
        # 에너지 밀도 (정배열 강도)
        energy_density = (gap_short + gap_long) / atr_p 
        trend_ratio = math.tanh(energy_density)
        base_score = 50 + (trend_ratio * 50)

        # [Refactor] 과열 필터링을 위한 총 이격도
        total_dispersal = ind.calculate_disparity(e5, e60)
        dispersal_ratio = total_dispersal / atr_p 
        
        # 감쇄 로직: 이격이 너무 벌어지면 점수 삭감
        overheat_factor = max(0.0, dispersal_ratio - 2.0)
        decay_penalty = math.exp(-overheat_factor * 0.5) 
        alignment_score = max(30.0, base_score * decay_penalty)

        # [Refactor] 장기 이평선 기울기 계산 위임
        if e60 > 0:
            slope_intensity = max(-1.0, min(1.0, ind.calculate_slope(e60, prev_e60)))
            slope_factor = 1.0 + (slope_intensity * 0.2)
        else:
            slope_factor = 1.0

        return float(round(max(0, min(100, alignment_score * slope_factor)), 2))

    def _calculate_dynamic_weights(self, metrics: Dict) -> Dict[str, float]:
        """[Weights] 시장 상황에 따른 지표별 가중치 조절"""
        vol_f = metrics.get('vol_factor', 1.0)
        atr_p = metrics.get('atr_percent', 3.0)
        price = metrics.get('price', 0)
        vwap = metrics.get('vwap', 0)
        
        imp_alpha = 1.0 * vol_f
        imp_supply = 1.0 * vol_f
        
        # [Refactor] VWAP 이격도 활용
        deviation = abs(ind.calculate_disparity(price, vwap)) if vwap > 0 else 0
        imp_vwap = 1.5 / (1 + (deviation / max(0.1, atr_p)))

        e5 = metrics.get('ema5', 0)
        e20 = metrics.get('ema20', 0)
        e60 = metrics.get('ema60', 0)

        gap1 = e5 - e20
        gap2 = e20 - e60
        denom = (abs(gap1) + abs(gap2))
        alignment_ratio = abs(gap1 + gap2) / denom if denom > 0 else 0.5
        is_ordered = 0.6 + (0.4 * alignment_ratio)

        # 이평선 확장 국면 평가
        raw_gap = abs(ind.calculate_disparity(e5, e60)) if e60 > 0 else 0
        vol_multiple = calculate_volatility_ratio(raw_gap, atr_p) if atr_p > 0 else 0

        # [Logic] 과열 구간 진입 시 Trend 가중치 축소
        if vol_multiple <= 1.5:
            expansion_factor = 1.0 + (vol_multiple * 0.1)
        elif vol_multiple <= 2.5:
            expansion_factor = 1.15
        else:
            expansion_factor = 1.15 - ((vol_multiple - 2.5) * 0.4)
            expansion_factor = max(0.4, expansion_factor)

        imp_trend = is_ordered * expansion_factor

        total_imp = imp_alpha + imp_supply + imp_vwap + imp_trend
        
        return {
            'alpha': imp_alpha / total_imp,
            'supply': imp_supply / total_imp,
            'vwap': imp_vwap / total_imp,
            'trend': imp_trend / total_imp
        }
    
    def _get_momentum(self, stock_code: str, current_score: float) -> float:
        """[Momentum] 점수의 변화량(미분값) 측정"""
        scores = self.history.get(stock_code, [])
        
        if not scores:
            self.history[stock_code] = [current_score]
            return 0.0
        
        avg_prev_score = sum(scores) / len(scores)
        momentum = round(current_score - avg_prev_score, 1)
        
        scores.append(current_score)
        self.history[stock_code] = scores[-5:] # 최근 5개만 유지
        
        return momentum
        
    def evaluate(self, metrics: Dict) -> Dict:
        """
        [Final Evaluation] 통합 평가 프로세스 (v2.3)
        1. 점수 계산 (Indicators & Multiplicative Model)
        2. 모멘텀 확인
        3. 전략적 판정 (차등 진입 & 과열 필터 적용)
        """
        stock_code = metrics['stock_code']
        
        # 1. 점수 산출
        score, score_detail = self._calculate_conviction_score(metrics)
        
        # 2. 모멘텀 산출
        momentum = self._get_momentum(stock_code, score)
        
        # 3. 전략적 판정
        status = "관망"
        is_buy_signal = False

        # [Analysis Logic] 주 동인 식별
        primary_driver = max(score_detail, key=score_detail.get)

        # [Analysis Logic] 차등 진입 기준 적용
        # Supply(수급)가 주도하는 경우 -> 기준 완화 (기회 포착)
        # Trend/Alpha가 주도하는 경우 -> 기준 강화 (보합 방지)
        if primary_driver == 'supply':
            effective_threshold = self.curr_supply_th
        else:
            effective_threshold = self.curr_strict_th

        if score >= effective_threshold:
            # [Filter 1] 기세 확인 (꺾인 놈은 안 산다)
            if momentum < 0:
                status = "⚠️고점경계"
                is_buy_signal = False
            # [Filter 2] Trend 과열 방지 (분석 결과: Trend 점수 너무 높으면 수익률 하락)
            elif score_detail['trend'] >= 90.0:
                status = "⚠️추세과열"
                is_buy_signal = False
            else:
                status = "🔥강력추천"
                is_buy_signal = True
                
        elif score >= self.curr_alert_th:
            # 점수는 부족하지만 기세가 폭발적일 때
            if momentum >= self.momentum_threshold:
                status = "🚀수급폭발"
                is_buy_signal = False
            else:
                status = "👀관심"
                is_buy_signal = False

        return {
                "score": score,
                "momentum": momentum,
                "status": status,
                'score_detail': score_detail,
                "regime": self._current_regime,
                "is_buy_signal": is_buy_signal,
                "primary_driver": primary_driver
            }

# Helper function for calculate_dynamic_weights context
def calculate_volatility_ratio(val, atr):
    return ind.calculate_volatility_ratio(val, atr)