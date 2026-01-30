import logging
import math
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional

from kiwoom_stock.monitoring.manager import Position
from .analyzer import MarketRegime

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    [Strategy] 트레이딩 전략 및 점수 산출: 하드코딩된 가중치/임계값 제거
    
    """
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)

        # [최적화] 문자열을 time 객체로 미리 변환 (루프 내 오버헤드 제거)
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        # [수정] 장 마감 3분 전 강제 청산 시간 계산 (오버헤드 방지를 위해 미리 계산)
        # datetime.combine을 사용하여 안전하게 시간 연산 수행
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        # 캐싱을 위한 내부 상태 변수
        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config = {}

        # [신규] 익절/손절/감쇠 설정 로드
        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025) # 기본 2.5%
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

        # [캡슐화] 종목별 점수 이력을 관리합니다.
        self.history = {} # { "종목코드": [점수1, 점수2, 점수3, 점수4, 점수5] }

        # [안전장치] 계좌 전체 손실 제한 (예: -5%)
        self.total_loss_limit = strategy_config.get("total_loss_limit", -5)
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        # [추가] 종목별 Alpha 점수 잔상 저장소
        self._alpha_memory: Dict[str, float] = {}
        self.alpha_decay = strategy_config.get("alpha_decay", 0.8) # 매 분 20%씩 감쇄

        # [추가] 종목별 Supply 점수 잔상 저장소
        self._supply_memory: Dict[str, float] = {}
        self.supply_decay = strategy_config.get("supply_decay", 0.8)

    def update_context(self, regime: MarketRegime):
        """
        [Strategy] 레짐이 변경될 때만 설정을 내부 캐싱하여 오버헤드를 줄입니다.
        
        """
        regime_val = regime.value if hasattr(regime, 'value') else str(regime)
        if self._current_regime == regime_val and self._cached_config:
            return

        self._current_regime = regime_val
        regimes = self.settings.get("regimes", {})
        self._cached_config = regimes.get(regime_val, regimes.get("default", {}))
        logger.info(f"Strategy context updated to: {regime_val}")

    @property
    def entry_thresholds(self) -> Dict[str, float]:
        """
        [Strategy] 현재 레짐의 진입 임계값을 반환합니다. (누락 시 보수적 기준)
        
        """
        return self._cached_config.get("thresholds", {
            "strong": 85.0, "interest": 75.0, "alert": 70.0
        })
    
    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        """
        [Strategy] 설정된 익절/손절/시간/점수 조건을 검사하여 매도 사유를 반환합니다.
   
        """
        # 현재 수익률 계산 (소수점 단위)
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        
        # 1. 시간 기반 당일 청산 (장 마감 3분 전부터 최우선 수행)
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        # 2. 하드 손절 (Stop Loss) - 설정값 이하로 하락 시 즉시 매도
        if profit_rate <= self.stop_loss_rate:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        # 3. 지능형 익절 (Take Profit)
        # 수익률이 목표치 이상이지만, 점수가 여전히 강하면(strong_threshold 이상) 매도를 미룹니다.
        if profit_rate >= self.target_profit_rate:
            if pos.current_score >= strong_threshold:
                return None # 기세가 좋으므로 익절 보류 (Let the winner run)
            return f"Take Profit (+{profit_rate*100:.1f}%)"

        # 4. 점수 하락 (Score Decay)
        # 상대적 점수 하락 (Score Decay)
        relative_threshold = pos.buy_score * (1 - self.decay_rate)
        # 절대적 지지선 (Score Floor)
        absolute_threshold = self.settings.get("thresholds", {}).get("alert", 70.0)

        # 두 값 중 더 낮은(관대한) 값을 기준으로 삼아 노이즈를 견디게 합니다.
        final_sell_threshold = min(relative_threshold, absolute_threshold)

        if pos.current_score < final_sell_threshold:
            return f"Score Decay (-{self.decay_rate*100:.0f}%)"

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        """
        [Strategy] 전체 손익이 허용치를 초과했는지 판단합니다.
        
        """
        return total_pnl <= self.total_loss_limit

    def is_monitoring_time(self) -> bool:
        """
        [Strategy] 장 운영 시간 체크 (에러 수정 버전)
        
        """
        now = datetime.now()
        if now.weekday() >= 5: return False
        
        # 시작 시간(09:00 권장)과 종료 시간(exit_time) 사이인지 비교
        return time(8, 30) <= now.time() <= self.exit_time_obj

    def is_trading_window(self) -> bool:
        """
        [Strategy] 현재 시간이 진입 가능한 시간대(entry_deadline)인지 확인
        
        """
        now = datetime.now().time()
        return now < self.deadline_time
        
    def _calculate_conviction_score(self, metrics: Dict) -> float:
        """
        [Strategy] 밸런스 시너지 모델 기반 최종 점수 산출
        - 4개 지표의 상호작용(Synergy)을 검증하여 '껍데기 상승' 차단
        - 모든 점수 체계를 100점 만점으로 표준화

        """
        # 1. 개정된 4대 지표 산출 (모두 0~100점 사이 반환)
        a_score = self._calculate_alpha_score(metrics)
        s_score = self._calculate_supply_score(metrics)
        v_score = self._calculate_vwap_score(metrics)
        t_score = self._calculate_trend_score(metrics)

        # 2. 실시간 동적 가중치 적용 (사용자님 기존 로직 유지)
        # weights['alpha'] + weights['supply'] + ... = 1.0 이 되도록 설정 권장
        w = self._calculate_dynamic_weights(metrics)
        
        # 가중 평균 점수 계산 (0~100점 사이)
        weighted_total = (a_score * w.get('alpha', 0.25)) + \
                        (s_score * w.get('supply', 0.25)) + \
                        (v_score * w.get('vwap', 0.25)) + \
                        (t_score * w.get('trend', 0.25))

        # 3. [핵심] 시너지 및 밸런스 체크
        score_list = [a_score, s_score, v_score, t_score]
        min_val = min(score_list)
        
        # 과락 페널티: 가장 낮은 지표가 20점 미만이면 불량 종목으로 간주
        # 시너지 배수 산출 로직: 최저점이 높을수록 1.0에 수렴, 낮을수록 급격히 하락
        if min_val < 20.0:
            # 하나라도 과락이면 전체 점수를 50% 이상 감점 (강제 관망)
            synergy_multiplier = 0.5 * (min_val / 20.0) 
        elif min_val < 40.0:
            # 밸런스가 약간 부족하면 20% 감점
            synergy_multiplier = 0.8
        else:
            # 모든 지표가 40점 이상으로 준수하면 시너지 100% 인정
            synergy_multiplier = 1.0

        # 4. 최종 점수 확정
        final_score = weighted_total * synergy_multiplier

        # 로그 기록을 위한 상세 데이터
        details = {
            "alpha": round(a_score, 1),
            "supply": round(s_score, 1),
            "vwap": round(v_score, 1),
            "trend": round(t_score, 1),
        }
        
        return round(final_score, 1), details

    def _calculate_alpha_score(self, metrics: Dict) -> float:
        """
        [Alpha Score] 가격 가속도 및 탄력성 평가
        - 가속도: 최근 1분 수익률 - (지난 5분 평균 수익률)
        - 신뢰도: 최근 1분 거래량 / (직전 4분 평균 거래량)
        - 기세가 꺾여도 이전의 강력했던 에너지를 메모리에서 불러와 유지함

        """
        stock_code = metrics['stock_code']
        price_series = metrics.get('price_series', [])
        vol_series = metrics.get('volume_series', [])

        if len(price_series) < 6:
            return 0.0

        # 1. 가속도 계산 (사용자님 제공 로직)
        roc_1m = (price_series[0] - price_series[1]) / price_series[1] * 100
        roc_5m = (price_series[0] - price_series[5]) / price_series[5] * 100
        acceleration = roc_1m - (roc_5m / 10)

        # 2. 거래량 신뢰도
        v_1m = vol_series[0]
        v_5m_avg = sum(vol_series[1:6]) / 5
        vol_factor = min(2.0, v_1m / v_5m_avg) if v_5m_avg > 0 else 1.0

        # 3. [개정] 시그모이드 점수 생성
        # k=25: 0.2% 가속도 시 만점 부근, 0% 시 50점
        k = 25
        combined_input = acceleration * vol_factor
        
        try:
            current_alpha = 100 / (1 + math.exp(-combined_input * k))
        except OverflowError:
            current_alpha = 100.0 if combined_input > 0 else 0.0

        # 4. [핵심] 잔상(Memory) 로직 적용
        # 이전에 저장된 이 종목의 Alpha 점수를 가져옴
        prev_alpha = self._alpha_memory.get(stock_code, 0.0)
        
        # "현재 점수"와 "이전 점수의 80%(감쇄)" 중 큰 값을 최종 점수로 선택
        # 이를 통해 점수가 0점으로 수직 낙하하는 것을 방지함
        final_alpha = max(current_alpha, prev_alpha * self.alpha_decay)
        
        # 다음 사이클을 위해 현재의 최종 점수를 메모리에 저장
        self._alpha_memory[stock_code] = final_alpha

        return float(round(final_alpha, 2))

    def _calculate_supply_score(self, metrics: Dict) -> float:
        """
        [supply_score] 실질 비중 직접 승수 모델 (상수 가산점 제거)
        체결강도 Base에 프로그램/외국계의 실제 시장 점유 비중을 직접 곱합니다.

        """
        stock_code = metrics['stock_code']
        strength = metrics.get('strength', 100.0)
        pgm_net = metrics.get('pgm_net', 0)
        frgn_net = metrics.get('frgn_net', 0)
        market_total = metrics.get('market_total_amount', 1)
        vol_ratio = metrics.get('vol_ratio', 0)

        # 1. Base Score: 체결강도 기반 (100% -> 50점)
        base_score = max(0, min(100, 50 + (strength - 100) * 0.5))

        # 2. 실질 지배력(Market Dominance) 및 안전 게이트
        market_total_million = market_total / 1000000
        if market_total_million < 10.0: # 1,000만원 미만 거래 시 수급 노이즈 차단
            pgm_adj, frgn_adj = 0, 0
        else:
            pgm_adj = max(-0.5, min(0.5, pgm_net / market_total_million))
            frgn_adj = max(-0.5, min(0.5, frgn_net / market_total_million))

        # 3. [핵심] 수급 영향력 5배 증폭 및 장 초반 신뢰도(trust_factor) 적용
        trust_factor = 1.0 if vol_ratio >= 5.0 else 0.5
        supply_impact = (pgm_adj + frgn_adj) * 5.0
        multiplier = 1.0 + (supply_impact * trust_factor)

        # 현재 시점의 실시간 점수 (100점 상한선 적용)
        current_supply_score = min(100.0, base_score * multiplier)

        # 4. [핵심] 잔상(Memory) 로직 적용
        prev_supply = self._supply_memory.get(stock_code, 0.0)
        
        # 현재 점수와 이전 점수의 85% 중 큰 값 선택
        final_supply = max(current_supply_score, prev_supply * 0.85)
        
        # 메모리 업데이트
        self._supply_memory[stock_code] = final_supply

        return float(round(final_supply, 2))

    def _calculate_vwap_score(self, metrics: Dict) -> float:
        """
        [vwap_score]수급 평단가 기반 위치 및 과열 평가
        지수 감쇄 모델 및 Floor Score 적용
        - 0점 속출 방지: 과열 구간에서도 최소 30점을 유지하여 밸런스 붕괴 방지
        - 표준화: 100점 만점 체계 준수

        """
        vwap = metrics.get('vwap', 0)
        price = metrics.get('price', 0)
        vol_factor = metrics.get('vol_factor', 1.0)
        prev_vwap = metrics.get('prev_vwap', 0)
        atr_p = metrics.get('atr_percent', 3.0)

        if vwap <= 0: return 0.0

        # 1. 기준 거리 설정 (상대적 잣대 유지)
        deviation = (price - vwap) / vwap * 100
        # overheat_limit: 이격도가 이 수치만큼 벌어지면 '주의' 구간
        overheat_limit = max(3.0, atr_p * 1.5) 
        
        # 2. [로직 개조] 지수적 감쇄 함수 (Exponential Decay)
        if deviation >= 0:
            # [정방향] VWAP 위에 있을 때
            # 기존: Linear (1 - ratio) -> 개정: Exponential (e^-ratio)
            ratio = deviation / overheat_limit
            
            # 이격이 overheat_limit과 같을 때 약 36.7점 산출
            # [핵심] 아무리 멀어져도 최소 30점은 유지하여 밸런스 확보
            pos_score = max(30.0, 100 * math.exp(-ratio))
        else:
            # [역방향/돌파] VWAP 아래에서 올라올 때 (기존 사용자님 로직 유지 및 보정)
            breakout_range = atr_p * 0.2 
            ratio = max(-1.0, deviation / breakout_range)
            # 돌파 기세 점수 (최대 100점)
            pos_score = 100 * (1 + ratio) * vol_factor

        # 3. 수급 추세(Slope) 가중치 반영 (사용자님 로직 유지)
        if prev_vwap > 0 and vwap != prev_vwap:
            raw_slope = (vwap - prev_vwap) / vwap * 1000
            slope_intensity = max(-1.0, min(1.0, raw_slope)) 
            slope_factor = 1.0 + (slope_intensity * 0.2) # 0.8 ~ 1.2
        else:
            slope_factor = 1.0

        # 최종 점수 산출 및 100점 캡핑
        final_vwap_score = pos_score * slope_factor
        
        return float(round(max(0, min(100, final_vwap_score)), 2))

    def _calculate_trend_score(self, metrics: Dict) -> float:
        """
        [trend_score] 완전 비례 및 동적 감쇄 모델 (상수 배제)
        모든 기준점은 ATR%(종목 변동성)를 '단위 잣대'로 사용하여 동적으로 결정됩니다.
        - 0점 속출 방지: 과열 시에도 최소 30점(Floor)을 유지하여 밸런스 붕괴 차단
        - 표준화: 100점 만점 체계 준수

        """
        e5 = metrics.get('ema5', 0)
        e20 = metrics.get('ema20', 0)
        e60 = metrics.get('ema60', 0)
        prev_e60 = metrics.get('prev_ema60', e60)
        atr_p = metrics.get('atr_percent', 3.0) 

        if e60 <= 0: return 0.0

        # 1. 추세 에너지 산출 (사용자님 로직 유지)
        gap_short = (e5 - e20) / e20 * 100
        gap_long = (e20 - e60) / e60 * 100
        energy_density = (gap_short + gap_long) / atr_p 
        
        # 2. [개정] 정배열 점수화 (Smooth Sigmoid)
        # 횡보(0)일 때 50점, 발산할수록 100점에 부드럽게 수렴
        trend_ratio = math.tanh(energy_density) # -1 ~ 1 사이로 부드럽게 압축
        base_score = 50 + (trend_ratio * 50)

        # 3. [개정] 동적 이격 감쇄 (Exponential Penalty)
        total_dispersal = (e5 - e60) / e60 * 100
        dispersal_ratio = total_dispersal / atr_p 
        
        # 이격 비율이 ATR의 2배 이상일 때 패널티 시작
        overheat_factor = max(0.0, dispersal_ratio - 2.0)
        
        # [핵심] 선형 penalty = overheat_factor / 2.0 대신 지수 감쇄 적용
        # 이격이 심해져도 최소 30점(Floor)은 보존하여 밸런스 파괴 방지
        decay_penalty = math.exp(-overheat_factor * 0.5) 
        alignment_score = max(30.0, base_score * decay_penalty)

        # 4. 장기 추세 수렴 가중치 (기울기 반영 로직 유지)
        if e60 > 0:
            slope_60 = (e60 - prev_e60) / e60 * 1000
            slope_intensity = max(-1.0, min(1.0, slope_60))
            slope_factor = 1.0 + (slope_intensity * 0.2)
        else:
            slope_factor = 1.0

        return float(round(max(0, min(100, alignment_score * slope_factor)), 2))

    def _calculate_dynamic_weights(self, metrics: Dict) -> Dict[str, float]:
        """
        [strategy] 지표 신뢰도 기반 동적 가중치
        각 지표의 현재 상태가 '얼마나 믿을만한가'를 계산하여 비중을 조절합니다.

        """
        vol_f = metrics.get('vol_factor', 1.0)
        atr_p = metrics.get('atr_percent', 3.0)
        price = metrics.get('price', 0)
        vwap = metrics.get('vwap', 0)
        
        # 1. 공격 지표 중요도 (Alpha & Supply)
        # 거래량이 터질수록(vol_f > 1) 공격 지표에 더 큰 확신을 가짐
        imp_alpha = 1.0 * vol_f
        imp_supply = 1.0 * vol_f
        
        # 2. 방어 지표 중요도 (VWAP)
        # 평단가에 바짝 붙어 있을수록(deviation -> 0) VWAP 지표의 결정권 강화
        deviation = abs(price - vwap) / vwap * 100 if vwap > 0 else 0
        imp_vwap = 1.5 / (1 + (deviation / max(0.1, atr_p))) # ATR 대비 이격 비례

        # 3. 추세 지표 중요도 (Trend - e20 활용)
        e5 = metrics.get('ema5', 0)
        e20 = metrics.get('ema20', 0)
        e60 = metrics.get('ema60', 0)

        # [핵심] 정렬 품질(Alignment Quality) 산출
        # e5 > e20 > e60 (정배열) 혹은 e5 < e20 < e60 (역배열) 처럼 '순서'가 맞아야 신뢰도 상승
        is_ordered = 1.2 if (e5 > e20 > e60) or (e5 < e20 < e60) else 0.7
        
        # 추세의 확장성 (ATR 대비 전체 폭)
        total_gap = abs(e5 - e60) / e60 * 100 if e60 > 0 else 0
        expansion_factor = min(0.5, total_gap / atr_p)
        
        # 최종 Trend 중요도: 질서정연하게(is_ordered) 터지고 있을 때(expansion) 신뢰
        imp_trend = 1.0 * is_ordered * (1 + expansion_factor)

        # 4. 가중치 정규화 (Normalization)
        total_imp = imp_alpha + imp_supply + imp_vwap + imp_trend
        
        return {
            'alpha': imp_alpha / total_imp,
            'supply': imp_supply / total_imp,
            'vwap': imp_vwap / total_imp,
            'trend': imp_trend / total_imp
        }
    
    def _get_momentum(self, stock_code: str, current_score: float) -> float:
        """
        [strategy] 내부 이력을 바탕으로 평균값 대비 모멘텀을 산출합니다.
        
        """
        # 1. 해당 종목의 이전 이력 가져오기
        scores = self.history.get(stock_code, [])
        
        if not scores:
            # 첫 데이터인 경우 이력에 추가만 하고 모멘텀 0 반환
            self.history[stock_code] = [current_score]
            return 0.0
        
        # 2. 평균 대비 모멘텀 계산 (사용자 제안 로직)
        avg_prev_score = sum(scores) / len(scores)
        momentum = round(current_score - avg_prev_score, 1)
        
        # 3. 이력 갱신 (최근 5개만 유지하여 메모리 효율화)
        scores.append(current_score)
        self.history[stock_code] = scores[-5:]
        
        return momentum
        
    def evaluate(self, metrics: Dict, regime) -> Dict:
        """
        [Strategy] 3단계 통합 평가: 컨텍스트 -> 계산/이력관리 -> 전략적 판정
        
        """
        # 1. 컨텍스트 업데이트 (레짐별 설정 로드)
        self.update_context(regime)
        stock_code = metrics['stock_code']
        
        # 2. 종합 점수 산출 (기존 로직)
        score, score_detail = self._calculate_conviction_score(metrics)
        
        # 3. 내부 이력 활용 모멘텀 산출
        momentum = self._get_momentum(stock_code, score)
        
        # 4. 전략적 판정 (모멘텀 필터 적용)
        th = self._cached_config.get("thresholds", {"strong": 80.0, "alert": 70.0})
        status = "관망"
        is_buy_signal = False

        if score >= th['strong']:
            # [전략 필터] 점수는 높으나 기세가 꺾였다면 진입 차단 (설거지 방지)
            if momentum >= 0:
                status = "🔥강력추천"
                is_buy_signal = True
            else:
                status = "⚠️고점경계"
                is_buy_signal = False
                
        elif score >= th['alert']:
            # [전략 포착] 점수가 부족해도 기세가 폭발하면 진입 (주도주 포착)
            if momentum >= self.momentum_threshold:
                status = "🚀수급폭발"
                is_buy_signal = True
            else:
                status = "👀관심"
                is_buy_signal = False

        return {
            "score": score,
            "momentum": momentum,
            "status": status,
            'score_detail': score_detail,
            "regime": self._current_regime,
            "is_buy_signal": is_buy_signal
        }