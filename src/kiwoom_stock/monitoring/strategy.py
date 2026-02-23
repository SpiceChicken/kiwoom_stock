import logging
from datetime import datetime, time, timedelta
from typing import Dict, Optional, List, Any

from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.types import MarketRegime

logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    [Strategy] 물리적 모멘텀 기반 트레이딩 전략
    - 특징: 물리 엔진이 산출한 가속도(모멘텀)와 속도(스코어)를 활용하여 가장 순수한 동역학 기반 판단을 내립니다.
    """
    
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 5.0) # 스코어 변화량 임계치
        self.debug_mode = strategy_config.get("debug_mode", False)

        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config: Dict[str, Any] = {}
        
        # [Thresholds] 진입 임계값 초기화
        self.curr_strict_th = 85.0
        self.curr_alert_th = 75.0
        self.curr_interest_th = 65.0

        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.03)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.03)

        self.history: Dict[str, List[float]] = {}
        self.total_loss_limit: float = float(strategy_config.get("total_loss_limit", -5))
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

    def update_context(self, regime: MarketRegime):
        """[Context Update] 시장 레짐에 따라 임계값 동적 조정"""
        regime_val = regime.value if hasattr(regime, 'value') else str(regime)

        if self.debug_mode: 
            self._current_regime = regime_val
            return

        if self._current_regime != regime_val:
            self._current_regime = regime_val
            regimes = self.settings.get("regimes", {})
            self._cached_config = regimes.get(regime_val, regimes.get("default", {}))
            
            config_th = self._cached_config.get("thresholds", {})
            self.curr_strict_th = config_th.get('strong', 85.0)
            self.curr_alert_th = config_th.get('alert', 75.0)
            
            logger.info(f"Strategy Updated: {regime_val} | Strict: {self.curr_strict_th}")

    def is_monitoring_time(self) -> bool:
        if self.debug_mode: return True
        now = datetime.now()
        if now.weekday() >= 5: return False
        return time(9, 0) <= now.time() <= self.exit_time_obj

    def is_trading_window(self) -> bool:
        if self.debug_mode: return True
        return time(9, 0) <= datetime.now().time() < self.deadline_time

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        return total_pnl <= self.total_loss_limit

    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        """[Exit Logic] 동역학 기반 청산 조건 판별"""
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        current_atr = getattr(pos, 'atr_percent', 0.5)
        
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        dynamic_stop = -(current_atr * 3.0) / 100
        final_stop = max(min(dynamic_stop, -0.015), self.stop_loss_rate * 1.5)

        if profit_rate <= final_stop:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        dynamic_target = (current_atr * 3.0) / 100
        final_target = max(dynamic_target, self.target_profit_rate)

        if profit_rate >= final_target:
            if pos.current_score >= strong_threshold:
                return None 
            return f"Take Profit (+{profit_rate*100:.1f}%)"
        
        current_decay = self.decay_rate
        if profit_rate >= 0.01:
            current_decay *= 0.5 
            
        relative_threshold = pos.buy_score * (1 - current_decay)
        absolute_threshold = self.curr_interest_th
        final_sell_threshold = min(relative_threshold, absolute_threshold)

        if pos.current_score < final_sell_threshold:
            return f"Score Decay (-{current_decay*100:.1f}%)"
            
        return None
    
    def _get_momentum(self, stock_code: str, current_score: float) -> float:
        """[Physics] 속도의 변화량 즉, 가속도(Momentum)를 측정합니다."""
        scores = self.history.setdefault(stock_code, [])
        if not scores:
            scores.append(current_score)
            return 0.0
            
        avg_prev_score = sum(scores) / len(scores)
        momentum = round(current_score - avg_prev_score, 1)
        scores.append(current_score)
        self.history[stock_code] = scores[-5:] 
        return momentum

    def evaluate(self, metrics: SupplyData) -> Dict:
        """
        [Final Verdict] 물리적 모멘텀 기반 진입 판독기
        """
        stock_code = metrics.stock_code
        total_score = metrics.total_score
        
        momentum = self._get_momentum(stock_code, total_score)
        
        status = "관망"
        is_buy_signal = False

        # 총점(물리적 속도)이 임계값을 돌파했는가?
        if total_score >= self.curr_strict_th:
            if momentum < 0:
                # 관성으로 인해 속도는 높지만 저항(Gravity/Drag)에 부딪혀 감속 중인 상태
                status = "⚠️고점경계 (감속 중)" 
            else:
                status = "🔥강력추천 (가속 돌파)"
                is_buy_signal = True
                
        elif total_score >= self.curr_alert_th:
            if momentum >= self.momentum_threshold:
                status = "🚀수급폭발 (초기 추진력 확보)" 
            else:
                status = "👀관심"

        return {
                "score": total_score,
                "momentum": momentum,
                "status": status,
                "regime": self._current_regime,
                "is_buy_signal": is_buy_signal,
                "price": metrics.cur_prc,
                "stock_code": stock_code,
                "atr_percent": getattr(metrics, 'atr_percent', 0.5)
            }