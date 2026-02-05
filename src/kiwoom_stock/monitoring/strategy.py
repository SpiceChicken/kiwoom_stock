import logging
import math
from datetime import datetime, time, timedelta
from typing import Dict, Tuple, Optional

from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core import scoring
from kiwoom_stock.core.types import MarketRegime

logger = logging.getLogger(__name__)

class TradingStrategy:
    """
    [Strategy] 트레이딩 전략 (v2.5 Modularized)
    - 점수 계산 로직을 scoring 모듈로 위임
    - SupplyData 데이터 클래스 활용
    """
    
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)
        self.debug_mode = strategy_config.get("debug_mode", False)

        # 시간 설정
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config = {}
        
        # 임계값 초기화
        self.curr_strict_th = 87.0
        self.curr_supply_th = 82.0
        self.curr_alert_th = 75.0

        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

        self.history = {} 
        self.total_loss_limit = strategy_config.get("total_loss_limit", -5)
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        # 메모리(잔상 효과) 관리 - Strategy의 상태(State)로 유지
        self._alpha_memory: Dict[str, float] = {}
        self.alpha_decay = strategy_config.get("alpha_decay", 0.8)
        self._supply_memory: Dict[str, float] = {}
        self.supply_decay = strategy_config.get("supply_decay", 0.8)

        if self.debug_mode:
            logger.warning("🚨 [DEBUG MODE] Strategy initialized in TEST mode.")

    def update_context(self, regime: MarketRegime):
        """시장 레짐 변경 시 설정 업데이트"""
        if self.debug_mode:
            if self._current_regime != "DEBUG_MODE":
                self._current_regime = "DEBUG_MODE"
                debug_th = self.settings.get("debug_thresholds", {})
                self.curr_strict_th = debug_th.get("strong", 50.0)
                self.curr_supply_th = debug_th.get("strong_supply", 50.0)
                self.curr_alert_th = debug_th.get("alert", 40.0)
                logger.warning(f"🚨 [DEBUG] Thresholds Fixed: {self.curr_strict_th}/{self.curr_supply_th}")
            return

        regime_val = regime.value if hasattr(regime, 'value') else str(regime)
        
        if self._current_regime != regime_val:
            self._current_regime = regime_val
            regimes = self.settings.get("regimes", {})
            self._cached_config = regimes.get(regime_val, regimes.get("default", {}))
            
            config_th = self._cached_config.get("thresholds", {})
            self.curr_strict_th = config_th.get('strong', 87.0)
            self.curr_supply_th = config_th.get('strong_supply', 82.0)
            self.curr_alert_th = config_th.get('alert', 75.0)
            
            logger.info(f"Strategy Updated: {regime_val}")

    # ... (Time Check Methods 생략: is_monitoring_time 등 기존과 동일) ...
    def is_monitoring_time(self) -> bool:
        if self.debug_mode: return True
        now = datetime.now()
        if now.weekday() >= 5: return False
        return time(8, 30) <= now.time() <= self.exit_time_obj

    def is_trading_window(self) -> bool:
        if self.debug_mode: return True
        return datetime.now().time() < self.deadline_time

    def is_kill_switch_activated(self, total_pnl: float) -> bool:
        return total_pnl <= self.total_loss_limit

    def get_exit_reason(self, pos: Position, strong_threshold: float) -> Optional[str]:
        # (기존 로직 유지)
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
        if profit_rate <= self.stop_loss_rate:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
        if profit_rate >= self.target_profit_rate:
            if pos.current_score >= strong_threshold:
                return None 
            return f"Take Profit (+{profit_rate*100:.1f}%)"
        
        current_decay = self.decay_rate
        if profit_rate >= 0.01: current_decay *= 1.5
        relative_threshold = pos.buy_score * (1 - current_decay)
        final_sell_threshold = min(relative_threshold, self.curr_alert_th)

        if pos.current_score < final_sell_threshold:
            return f"Score Decay (-{current_decay*100:.0f}%)"
        return None

    def _calculate_conviction_score(self, data: SupplyData) -> Tuple[float, Dict]:
        """[Delegation] 점수 계산을 scoring 모듈에 위임"""
        # 메모리 값 조회
        prev_alpha = self._alpha_memory.get(data.stock_code, 0.0)
        prev_supply = self._supply_memory.get(data.stock_code, 0.0)

        # Scoring 모듈 호출 (순수 함수)
        a_score = scoring.calculate_alpha_score(data, prev_alpha, self.alpha_decay)
        s_score = scoring.calculate_supply_score(data, prev_supply, self.supply_decay)
        v_score = scoring.calculate_vwap_score(data)
        t_score = scoring.calculate_trend_score(data)
        
        # 메모리 업데이트 (State Update)
        self._alpha_memory[data.stock_code] = a_score
        self._supply_memory[data.stock_code] = s_score

        # 동적 가중치 및 최종 점수 계산
        w = scoring.calculate_dynamic_weights(data)
        
        final_score = (
            math.pow(max(1.0, a_score), w.get('alpha', 0.25)) *
            math.pow(max(1.0, s_score), w.get('supply', 0.25)) *
            math.pow(max(1.0, v_score), w.get('vwap', 0.25)) *
            math.pow(max(1.0, t_score), w.get('trend', 0.25))
        )

        details = {
            "alpha": a_score, "supply": s_score, "vwap": v_score, "trend": t_score
        }
        return round(final_score, 1), details
    
    def _get_momentum(self, stock_code: str, current_score: float) -> float:
        scores = self.history.get(stock_code, [])
        if not scores:
            self.history[stock_code] = [current_score]
            return 0.0
        avg_prev_score = sum(scores) / len(scores)
        momentum = round(current_score - avg_prev_score, 1)
        scores.append(current_score)
        self.history[stock_code] = scores[-5:]
        return momentum
        
    def evaluate(self, metrics: SupplyData) -> Dict:
        """통합 평가 (Input type changed to SupplyData)"""
        stock_code = metrics.stock_code
        score, score_detail = self._calculate_conviction_score(metrics)
        momentum = self._get_momentum(stock_code, score)
        
        status = "관망"
        is_buy_signal = False
        primary_driver = max(score_detail, key=score_detail.get)

        if primary_driver == 'supply':
            effective_threshold = self.curr_supply_th
        else:
            effective_threshold = self.curr_strict_th

        if score >= effective_threshold:
            if momentum < 0:
                status = "⚠️고점경계"
                is_buy_signal = False
            elif score_detail['trend'] >= 90.0:
                status = "⚠️추세과열"
                is_buy_signal = False
            else:
                status = "🔥강력추천"
                is_buy_signal = True
        elif score >= self.curr_alert_th:
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
                "primary_driver": primary_driver,
                # Engine 호환성을 위해 필요한 필드들 전달
                "price": metrics.price,
                "stock_code": stock_code
            }