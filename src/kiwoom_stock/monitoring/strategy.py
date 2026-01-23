import logging
from typing import Dict, Tuple
from .analyzer import MarketRegime

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class TradingStrategy:
    """[Strategy] 트레이딩 전략 및 점수 산출: 하드코딩된 가중치/임계값 제거"""
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)

        # 캐싱을 위한 내부 상태 변수
        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config = {}

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