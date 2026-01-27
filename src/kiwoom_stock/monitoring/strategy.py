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
        """총점과 상세 지표 점수를 함께 반환합니다."""
        w = self.weights
        
        # 1. Alpha 점수: 민감도 하향 (2.5 -> 1.5) 
        # 시장 지수 대비 초과 수익률이 더 높아야 고득점이 가능하도록 변경
        alpha_raw = max(0, min(100, 50 + (metrics['alpha'] * 1.5)))
        
        # 2. Supply 점수: 기존 로직 유지 (고정)
        total_vol = max(1, metrics.get('volume', 1))
        s_ratio = (metrics.get('net_buy', 0) / total_vol) * 100
        synergy = 20 if (metrics['f_buy'] > 0 and metrics['i_buy'] > 0) else (-20 if (metrics['f_buy'] < 0 and metrics['i_buy'] < 0) else 0)
        supply_raw = max(0, min(100, (s_ratio * 50) + synergy))
        
        # 3. VWAP 점수: 민감도 추가 하향 (10 -> 8)
        # 이제 가격 이격도가 약 6.25% 이상일 때만 100점에 도달합니다. (기존 5%)
        dev = (metrics['price'] / metrics['vwap'] - 1) * 100 if metrics['vwap'] > 0 else 0
        vwap_raw = max(0, min(100, 50 + (dev * 8)))
        
        # 4. Trend 점수: 민감도 하향 (2.5 -> 1.5) 
        # 단순히 RSI가 70을 넘는 것만으로는 부족하며, 80 이상의 강력한 과매수 구간에 
        # 진입해야 100점에 근접하도록 문턱을 높임
        t_rsi = metrics['trend_rsi']
        trend_raw = max(0, min(100, 50 + ((t_rsi - 50) * 1.5) if t_rsi >= 50 else t_rsi))
        
        # 최종 가중치 합산
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