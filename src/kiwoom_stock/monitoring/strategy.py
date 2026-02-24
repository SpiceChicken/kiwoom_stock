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

        self.decay_rate = strategy_config.get("score_decay_rate", 0.25)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.03)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.03)

        self.history: Dict[str, List[float]] = {}
        self.total_loss_limit: float = float(strategy_config.get("total_loss_limit", -5))
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        # 청산 동력을 추적
        self._kinetic_state: Dict[str, Dict[str, Any]] = {}

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
            
            logger.info(f"Strategy Updated: {regime_val}")

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

    def get_exit_reason(self, pos, current_price: float, forces: Dict) -> Optional[str]:
        """
        물리적 힘(forces)을 통째로 받아 동적 상태를 업데이트하고 청산을 판단합니다.
        """
        now_time = datetime.now().time()
        stock_code = pos.stock_code
        
        # 이미 오버나잇으로 확정된 종목은 당일 청산 로직 무시
        if pos.status == "OVERNIGHT":
            return None

        # -------------------------------------------------------------------
        # 0. 전략 내부 상태(Kinetic State) 초기화 및 갱신
        # -------------------------------------------------------------------
        state = self._kinetic_state.setdefault(stock_code, {'max_v': 0.0, 'neg_count': 0})
        
        current_velocity = forces.get('current_velocity', 0.0)
        net_force = forces.get('net_force', 0.0)
        thrust = forces.get('thrust', 0.0)
        magnetic = forces.get('magnetic', 0.0)

        # 상태 업데이트
        if current_velocity > state['max_v']:
            state['max_v'] = current_velocity
            
        # 최근 3회의 합력(Net Force)을 리스트에 저장 (Sliding Window)
        state['forces'].append(net_force)
        if len(state['forces']) > 3:
            state['forces'].pop(0)

        # -------------------------------------------------------------------
        # 1. Selective Swing (15:20 이후 찐 주도주 홀딩 예외 룰)
        # -------------------------------------------------------------------
        if now_time >= time(15, 20):
            # 엔진 점수(현재 속도의 시그모이드) 추정 또는 current_velocity 직접 사용
            if current_velocity >= 3.0 and thrust > 2.0 and magnetic > 0:
                pos.status = "OVERNIGHT"  # 상태만 변경하고 보유
                return None 

        # -------------------------------------------------------------------
        # 2. Time-based Exit (장 마감 강제 청산 - 예외 룰 통과 못한 종목)
        # -------------------------------------------------------------------
        if not getattr(self, 'debug_mode', False) and now_time >= getattr(self, 'forced_exit_time', time(15, 27)):
            return "Day Trade Close"

        # -------------------------------------------------------------------
        # 3. Kinetic Trailing Stop (물리적 트레일링 스탑)
        # -------------------------------------------------------------------
        # A. 속도 감쇠: 최고 속도 대비 15% 이상 추진력이 꺾이면 즉시 차익 실현
        if state['max_v'] > 0 and current_velocity < (state['max_v'] * 0.85):
            return "Kinetic Exit (Velocity Drop)"
            
        # B. 누적 합력 평가: 최근 3분간의 합력 총합이 확실한 음수(-3.0 이하)일 때 이탈
        if len(state['forces']) == 3 and sum(state['forces']) <= -3.0:
            return f"Kinetic Exit (Cum. Force: {sum(state['forces']):.2f})"

        # -------------------------------------------------------------------
        # 4. 기존 ATR 기반 Dynamic Stop-Loss 로직
        # -------------------------------------------------------------------
        profit_rate = (current_price / pos.buy_price - 1) * 100
        current_atr = getattr(pos, 'atr_percent', 0.5)
        dynamic_stop = -(current_atr * 3.0) 
        
        if profit_rate <= dynamic_stop:
            return f"Stop Loss ({profit_rate:.2f}%)"
            
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
        stock_code, total_score = metrics.stock_code, metrics.total_score
        forces = getattr(metrics, 'forces', {})

        # 물리 엔진 파라미터 추출
        thrust = forces.get('thrust', 0.0)
        net_force = forces.get('net_force', 0.0)
        
        momentum = self._get_momentum(stock_code, total_score)
        
        status = "관망"
        is_buy_signal = False

        # -------------------------------------------------------------------
        # 조건 1: thrust > 0 
        #   -> 체결강도가 100%를 초과하여 매수세가 엔진을 점화했는가?
        # 조건 2: net_force > 0 
        #   -> 추진력이 중력(VWAP)과 마찰(RSI)을 완벽히 이겨내고 위로 향하는가?
        # 조건 3: momentum > 0 
        #   -> 최근 5주기 평균 대비 가속이 붙고 있는가?
        # -------------------------------------------------------------------
        if thrust > 0.0 and net_force > 0.0:
            if momentum > 0.0:
                is_buy_signal = True
                status = "🔥로켓발진 (Ignition)"
            else:
                status = "⚠️저항돌파중 (Struggling)"
        elif thrust > 0.0:
            status = "👀수급유입 (Engine On)"

        return {
                "score": total_score,
                "momentum": momentum,
                "status": status,
                "regime": self._current_regime,
                "is_buy_signal": is_buy_signal,
                "price": metrics.cur_prc,
                "stock_code": stock_code,
                "atr_percent": getattr(metrics, 'atr_percent', 0.5),
                "forces": getattr(metrics, 'forces', {})
            }