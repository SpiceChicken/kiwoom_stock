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

    def calculate_conviction_score(self, metrics: Dict):
        """[모듈화] 지표별 점수를 산출하고 가중 합산한 최종 점수를 반환합니다."""
        
        # 1. 각 지표별 독립 메서드 호출
        alpha_raw = self._calculate_alpha_score(metrics)
        supply_raw = self._calculate_supply_score(metrics)
        vwap_raw = self._calculate_vwap_score(metrics)
        trend_raw = self._calculate_trend_score(metrics)

        # 2. 최종 가중치 합산
        w = self.weights
        total_score = round(
            (alpha_raw * w.get('alpha', 0.25)) + 
            (supply_raw * w.get('supply', 0.25)) + 
            (vwap_raw * w.get('vwap', 0.25)) + 
            (trend_raw * w.get('trend', 0.25)), 1
        )
        
        # 상세 점수 딕셔너리 생성
        details = {
            "alpha": round(alpha_raw, 1),
            "supply": round(supply_raw, 1),
            "vwap": round(vwap_raw, 1),
            "trend": round(trend_raw, 1)
        }
        
        return total_score, details

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

        try:
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

        except (ZeroDivisionError, ValueError) as e:
            logger.error(f"Alpha Score 연산 오류: {e}")
            return 0.0

    def _calculate_supply_score(self, metrics: Dict) -> float:
        """
        실질 비중 직접 승수 모델 (상수 가산점 제거)
        체결강도 Base에 프로그램/외국계의 실제 시장 점유 비중을 직접 곱합니다.

        """
        # 1. 기초 데이터 확보 (Analyzer에서 정제된 데이터 주입)
        strength = metrics.get('strength', 100.0)
        pgm = metrics.get('pgm_data', {})      # {'netprps_prica', 'all_trde_rt', 'buy_cntr_amt', 'sel_cntr_amt'}
        frgn = metrics.get('foreign_data', {}) # {'netprps_prica', 'trde_prica'}

        # 전일 대비 거래량 비율 (%) - Engine에서 주입
        # 예: 전일 거래량 대비 5% 수준이면 5.0
        vol_ratio = metrics.get('vol_ratio', 100.0)

        # 2. Base Score: 체결강도 (200% -> 100점 매핑)
        # 100%일 때 50점 기준, 기세가 없으면(0%) 0점
        base_score = max(0, min(100, 50 + (strength - 100) * 0.5))

        # 3. 프로그램 실질 참여 비중 계산 (Logic Value)
        pgm_net = pgm.get('netprps_prica', 0)
        pgm_total = pgm.get('buy_cntr_amt', 0) + pgm.get('sel_cntr_amt', 0)
        pgm_ratio = pgm.get('all_trde_rt', 0) / 100
        
        # 프로그램 내 순매수 강도 * 시장 점유율
        pgm_adj = (pgm_net / pgm_total * pgm_ratio) if pgm_total > 0 else 0

        # 4. 외국계 실질 참여 비중 계산 (Logic Value)
        frgn_net = frgn.get('netprps_prica', 0)
        frgn_total = max(1, frgn.get('trde_prica', 1)) # 분모 0 방지

        # 5. 거래량 기반 안전핀 (Safety Pin) 적용
        # 전일 거래량의 5%도 안 되는 시점에서는 수급 데이터의 신뢰도를 50%로 강제 감쇄
        # 이는 장 초반 소액 매매로 인한 '가중치 튐' 현상을 방어함
        trust_factor = 1.0
        if vol_ratio < 5.0:
            trust_factor = 0.5
        
        # 전체 거래대금 대비 외국계 순매수 비중
        frgn_adj = (frgn_net / frgn_total)

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
        """Trend 점수: 민감도 1.5 적용 (RSI 80 이상 시 100점 근접)"""
        t_rsi = metrics.get('trend_rsi', 50)
        if t_rsi >= 50:
            val = 50 + ((t_rsi - 50) * 1.5)
        else:
            val = t_rsi  # 하락 구간은 RSI 값 그대로 유지 (보수적)
        return max(0, min(100, val))

    def calculate_buy_score(self, code: str, analyzer, metrics: Dict) -> float:
        """
        전체 지표 합산 로직 (시간별 가중치 적용)
        """
        now = datetime.now().time()
        
        # 실시간 수급 점수 계산
        supply_score = self.calculate_supply_score(code, analyzer)
        
        # 기존 알파/가격 지표
        alpha = metrics.get('alpha', 50)
        vwap = metrics.get('vwap', 50)
        trend = metrics.get('trend', 50)

        # [핵심] 장 초반(09:30 이전) 수급 데이터 신뢰도 가중치 조절
        # 데이터가 쌓이기 전인 장 초반에는 수급 비중을 낮추고 기세(Alpha)에 집중
        w = self.weights.copy()
        if now < time(9, 30):
            w['supply'] *= 0.5  # 수급 가중치 50% 감소
            # 감소된 만큼의 가중치를 alpha(기세)로 전이하여 장 초반 공격성 유지
            w['alpha'] += (self.weights['supply'] * 0.5)

        # 최종 가중치 합산 점수 산출
        score = (
            (alpha * w['alpha']) +
            (supply_score * w['supply']) +
            (vwap * w['vwap']) +
            (trend * w['trend'])
        )
        
        return round(score, 2)