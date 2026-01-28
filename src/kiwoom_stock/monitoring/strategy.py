import logging
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional

from kiwoom_stock.monitoring.manager import Position
from .analyzer import MarketRegime

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class TradingStrategy:
    """[Strategy] 트레이딩 전략 및 점수 산출: 하드코딩된 가중치/임계값 제거"""
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
        self.decay_rate = strategy_config.get("score_decay_rate", 0.15)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025) # 기본 2.5%
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

        # [안전장치] 계좌 전체 손실 제한 (예: -5%)
        self.total_loss_limit = strategy_config.get("total_loss_limit", -5)

    def update_context(self, regime: MarketRegime):
        """
        레짐이 변경될 때만 호출하여 관련 설정을 내부 메모리에 캐싱합니다.
       
        """
        if self._current_regime == regime and self._cached_config:
            return # 변경 사항이 없으면 유지

        self._current_regime = regime
        regimes = self.settings.get("regimes", {})
        # 해당 레짐 설정 로드, 없으면 default 로드
        self._cached_config = regimes.get(regime.value, regimes.get("default", {}))
        logger.info(f"Strategy context updated to: {regime.value}")

    @property
    def weights(self) -> Dict[str, float]:
        """현재 레짐의 가중치를 반환합니다. (누락 시 균등 가중치)"""
        return self._cached_config.get("weights", {
            "alpha": 0.25, "supply": 0.25, "vwap": 0.25, "trend": 0.25
        })

    @property
    def entry_thresholds(self) -> Dict[str, float]:
        """현재 레짐의 진입 임계값을 반환합니다. (누락 시 보수적 기준)"""
        return self._cached_config.get("thresholds", {
            "strong": 85.0, "interest": 75.0, "alert": 70.0
        })

    @property
    def min_thresholds(self) -> Dict[str, float]:
        """
        현재 레짐의 개별 지표 하한선을 반환합니다.
        레짐별 설정 -> 공통 루트 설정 순으로 참조합니다.
        """
        return self._cached_config.get("min_thresholds", self.settings.get("min_thresholds", {}))
    
    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        """
        설정된 익절/손절/시간/점수 조건을 검사하여 매도 사유를 반환합니다.
   
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

        # 4. 상대적 점수 하락 (Score Decay)
        sell_threshold = pos.buy_score * (1 - self.decay_rate)
        if pos.current_score < sell_threshold:
            return f"Score Decay (-{self.decay_rate*100:.0f}%)"

        return None

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        """[Strategy] 전체 손익이 허용치를 초과했는지 판단합니다."""
        return total_pnl <= self.total_loss_limit

    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (에러 수정 버전)"""
        now = datetime.now()
        if now.weekday() >= 5: return False
        
        # 시작 시간(09:00 권장)과 종료 시간(exit_time) 사이인지 비교
        return time(8, 30) <= now.time() <= self.exit_time_obj

    def calculate_conviction_score(self, metrics: Dict) -> float:
        # 1. 각 지표별 점수 산출 (v3.0 모델들)
        a_score = self._calculate_alpha_score(metrics)
        s_score = self._calculate_supply_score(metrics)
        v_score = self._calculate_vwap_score(metrics)
        t_score = self._calculate_trend_score(metrics)

        # 2. 실시간 동적 가중치 획득
        w = self._calculate_dynamic_weights(metrics)
        
        # 3. 가중치 합산
        total = (a_score * w['alpha']) + (s_score * w['supply']) + \
                (v_score * w['vwap']) + (t_score * w['trend'])

        # 상세 점수 딕셔너리 생성
        details = {
            "alpha": round(a_score * w['alpha'], 1),
            "supply": round(s_score * w['supply'], 1),
            "vwap": round(v_score * w['vwap'], 1),
            "trend": round(t_score * w['trend'], 1)
        }
        
        return round(total, 1), details

    def _calculate_alpha_score(self, metrics) -> float:
        """
        [Alpha Score] 가격 가속도 및 탄력성 평가
        - 가속도: 최근 1분 수익률 - (지난 5분 평균 수익률)
        - 신뢰도: 최근 1분 거래량 / (직전 4분 평균 거래량)
        """

        price_series = metrics.get('price_series')
        volume_series = metrics.get('volume_series')

        # 최소 6개의 데이터 포인트가 필요함 (현재 포함 5분전까지 비교)
        if len(price_series) < 6 or len(volume_series) < 6:
            return 0.0
        # 데이터 인덱싱 (리스트 끝이 현재 데이터)
        # [-1]: 현재, [-2]: 1분전, [-6]: 5분전
        curr_p = price_series[-1]
        p_1m_ago = price_series[-2]
        p_5m_ago = price_series[-6]

        if p_1m_ago <= 0 or p_5m_ago <= 0: return 0.0

        # 1. 가속도(Acceleration) 산출
        roc_1m = (curr_p - p_1m_ago) / p_1m_ago * 100
        roc_5m = (curr_p - p_5m_ago) / p_5m_ago * 100
            
        # 현재 기세가 평균 기세보다 얼마나 높은지 (Momentum Acceleration)
        acceleration = roc_1m - (roc_5m / 5)

        # 2. 거래량 신뢰도(Volume Surge)
        # 최근 1분 거래량 vs 직전 4분 평균 거래량 ([-5]부터 [-2]까지)
        avg_prev_vol = max(1.0, sum(volume_series[-5:-1]) / 4)
        curr_vol = volume_series[-1]
        vol_factor = min(2.0, curr_vol / avg_prev_vol)

        # 3. 최종 Alpha 점수화 (0~100점)
        # 가속도 1%당 100점 기준 가중치 부여 및 거래량 증폭
        raw_alpha = acceleration * 100 * vol_factor
            
        return round(max(0, min(100, raw_alpha)), 2)

    def _calculate_supply_score(self, metrics: Dict) -> float:
        """
        실질 비중 직접 승수 모델 (상수 가산점 제거)
        체결강도 Base에 프로그램/외국계의 실제 시장 점유 비중을 직접 곱합니다.

        """
        # 1. 기초 데이터 확보 (Analyzer에서 정제된 데이터 주입)
        strength = metrics.get('strength', 100.0)
        pgm = metrics.get('pgm_data', {})      # {'netprps_prica', 'all_trde_rt', 'buy_cntr_amt', 'sel_cntr_amt'}
        frgn = metrics.get('foreign_data', {}) # {'netprps_prica', 'trde_prica'}
        trde_qty = metrics.get('trde_qty', 0)
        cur_prc = metrics.get('cur_prc', 0)

        # 실시간 누적 거래대금 산출 (단위: 100만 원)
        market_total_amount = max(1, trde_qty * cur_prc) / 1000000

        # 전일 대비 거래량 비율 (%) - Engine에서 주입
        # 예: 전일 거래량 대비 5% 수준이면 5.0
        vol_ratio = metrics.get('vol_ratio', 100.0)

        # 2. Base Score: 체결강도 (200% -> 100점 매핑)
        # 100%일 때 50점 기준, 기세가 없으면(0%) 0점
        base_score = max(0, min(100, 50 + (strength - 100) * 0.5))

        # 3. 프로그램/외국인 실질 참여 비중 계산 (Logic Value)
        pgm_net = pgm.get('netprps_prica', 0)
        frgn_net = frgn.get('netprps_prica', 0)
        
        if market_total_amount < 10.0:  # 누적 거래대금이 1,000만 원 미만일 때
            pgm_adj = 0
            frgn_adj = 0
        else:
            # 0.5(50%)를 상한선으로 두어 데이터 오염 방어
            # 전체 거래대금 대비 프로그램 순매수 비중
            pgm_adj = max(-0.5, min(0.5, pgm_net / market_total_amount))
            # 전체 거래대금 대비 외국계 순매수 비중
            frgn_adj = max(-0.5, min(0.5, frgn_net / market_total_amount))
        
        # 5. 거래량 기반 안전핀 (Safety Pin) 적용
        # 전일 거래량의 5%도 안 되는 시점에서는 수급 데이터의 신뢰도를 50%로 강제 감쇄
        # 이는 장 초반 소액 매매로 인한 '가중치 튐' 현상을 방어함
        trust_factor = 1.0
        if vol_ratio < 5.0:
            trust_factor = 0.5
        
        # 6. 최종 점수 산출 (Multiplicative Model)
        # Multiplier = 1.0 + (프로그램 비중 + 외국계 비중)
        # 예: 체결강도 140%(70점) 종목에 프로그램 3%, 외국계 2% 순매수 비중 발생 시
        # 70 * (1 + 0.03 + 0.02) = 70 * 1.05 = 73.5점
        multiplier = 1.0 + (pgm_adj + frgn_adj) * trust_factor
        final_score = base_score * multiplier

        return round(max(0, min(100, final_score)), 2)

    def _calculate_vwap_score(self, metrics: Dict) -> float:
        """
        수급 평단가 기반 위치 및 과열 평가

        """
        vwap = metrics.get('vwap', 0)
        price = metrics.get('price', 0)
        vol_factor = metrics.get('vol_factor', 1.0)
        prev_vwap = metrics.get('prev_vwap', 0)
        atr_p = metrics.get('atr_percent', 3.0)

        if vwap <= 0: return 0.0

        # 1. 기준 거리 설정 (상대적 잣대)
        # 모든 계산의 분모가 되는 '단위 거리'를 종목 변동성(ATR)에 동기화
        deviation = (price - vwap) / vwap * 100
        overheat_limit = max(3.0, atr_p * 1.5) 
        
        # 2. 선형 감쇄 함수 (Linear Decay Function) 적용
        if deviation >= 0:
            # [정방향] VWAP(0%)일 때 100점, overheat_limit일 때 0점
            # 공식: 100 * (1 - 현재이격/한계이격)
            ratio = min(1.0, deviation / overheat_limit)
            pos_score = 100 * (1 - ratio)
        else:
            # [역방향/돌파] VWAP에 가까워질수록 점수 상승
            # 돌파 가용 범위를 ATR의 일정 비율(예: 0.2배)로 동적 설정
            breakout_range = atr_p * 0.2 
            ratio = max(-1.0, deviation / breakout_range)
            # VWAP에 붙을수록 100점에 수렴하며, 거래량 가속도(vol_factor)를 가중치로 사용
            pos_score = 100 * (1 + ratio) * vol_factor

        # 3. 수급 추세(Slope) 반영: 기울기를 정규화하여 가중치로 변환
        # 상수가 아닌 기울기의 강도에 따라 0.8 ~ 1.2 사이를 유동적으로 움직임
        if prev_vwap > 0 and vwap != prev_vwap:
            raw_slope = (vwap - prev_vwap) / vwap * 1000
            # 기울기 강도를 -1 ~ 1 사이로 압축한 뒤 가중치화 (Sigmoid 형태 혹은 Clamp)
            slope_intensity = max(-1.0, min(1.0, raw_slope)) 
            slope_factor = 1.0 + (slope_intensity * 0.2) # 0.8 ~ 1.2
        else:
            slope_factor = 1.0

        return round(max(0, min(100, pos_score * slope_factor)), 2)

    def _calculate_trend_score(self, metrics: Dict) -> float:
        """
        완전 비례 및 동적 감쇄 모델 (상수 배제)
        모든 기준점은 ATR%(종목 변동성)를 '단위 잣대'로 사용하여 동적으로 결정됩니다.

        """
        e5 = metrics.get('ema5', 0)
        e20 = metrics.get('ema20', 0)
        e60 = metrics.get('ema60', 0)
        prev_e60 = metrics.get('prev_ema60', e60)
        atr_p = metrics.get('atr_percent', 3.0) # 종목의 체급(단위 잣대)

        if e60 <= 0: return 0.0

        # 1. 추세 에너지(Trend Energy) 산출
        # 이평선 간의 간격을 ATR%로 정규화하여 '추세의 질'을 측정합니다.
        # 정배열일 때 양수, 역배열일 때 음수가 나옵니다.
        gap_short = (e5 - e20) / e20 * 100
        gap_long = (e20 - e60) / e60 * 100
        
        # 에너지 밀도: ATR% 대비 이평선들이 얼마나 건강하게 벌어져 있는가?
        energy_density = (gap_short + gap_long) / atr_p
        
        # 2. 정배열 점수화 (Energy to Score)
        # 에너지가 0(완전 수렴)일 때 50점, 양수로 발산할수록 100점에 수렴, 음수면 0점에 수렴
        # 상수를 쓰지 않고 하이퍼볼릭 탄젠트(tanh)와 유사한 비율 함수를 적용합니다.
        trend_ratio = max(-1.0, min(1.0, energy_density)) # -1 ~ 1 사이로 압축
        base_score = 50 + (trend_ratio * 50) # 역배열이면 0~50, 정배열이면 50~100

        # 3. 동적 이격 감쇄 (Dynamic Dispersal Penalty)
        # 단기 이평(e5)이 장기 이평(e60)으로부터 평소 변동성(ATR)보다 얼마나 과하게 벌어졌는가?
        total_dispersal = (e5 - e60) / e60 * 100
        dispersal_ratio = total_dispersal / atr_p  # 평소 변동성 대비 현재 벌어진 배수
        
        # [핵심] 이격 비율이 커질수록(보통 2배~3배 이상) 점수를 선형적으로 깎음
        # 특정 임계치 상수가 아닌, 분모(atr_p)에 비례하는 감쇄 로직
        overheat_factor = max(0.0, dispersal_ratio - 1.0) # 1배(ATR)까지는 정상 추세로 인정
        penalty = min(1.0, overheat_factor / 2.0) # ATR의 3배 지점에서 최대 페널티(0점) 도달
        
        alignment_score = base_score * (1 - penalty)

        # 4. 장기 추세 수렴 가중치 (Slope Intensity)
        # EMA 60선의 기울기를 정규화하여 가중치로 변환 (0.8 ~ 1.2)
        slope_60 = (e60 - prev_e60) / e60 * 1000
        slope_intensity = max(-1.0, min(1.0, slope_60))
        slope_factor = 1.0 + (slope_intensity * 0.2)

        return round(max(0, min(100, alignment_score * slope_factor)), 2)

    def _calculate_dynamic_weights(self, metrics: Dict) -> Dict[str, float]:
        """
        지표 신뢰도 기반 동적 가중치
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