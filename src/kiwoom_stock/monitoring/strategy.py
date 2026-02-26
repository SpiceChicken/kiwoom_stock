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
        [청산 판단] 물리적 힘(forces)과 가격 동역학을 결합한 헤비 엑시트(Heavy Exit) 룰 적용
        """
        now_time = datetime.now().time()
        stock_code = pos.stock_code
        
        # 이미 오버나잇으로 확정된 종목은 당일 청산 로직 무시
        if pos.status == "OVERNIGHT":
            return None

        # -------------------------------------------------------------------
        # 0. 전략 내부 상태(Kinetic State) 초기화 및 갱신 (max_price 기반)
        # -------------------------------------------------------------------
        # max_v 삭제 및 max_price 추가 (초기값: 매수가)
        buy_price = getattr(pos, 'buy_price', current_price)
        if buy_price <= 0: buy_price = current_price
        
        state = self._kinetic_state.setdefault(stock_code, {'max_price': buy_price, 'forces': []})
        
        current_velocity = forces.get('current_velocity', 0.0)
        net_force = forces.get('net_force', 0.0)
        thrust = forces.get('thrust', 0.0)
        magnetic = forces.get('magnetic', 0.0)

        # 고점 가격 트래킹 업데이트
        if current_price > state['max_price']:
            state['max_price'] = current_price
            
        # 최근 6회의 합력(Net Force)을 리스트에 저장 (Sliding Window)
        state['forces'].append(net_force)
        if len(state['forces']) > 6:
            state['forces'].pop(0)  # 최대 6틱(60초)의 물리량 보존

        # -------------------------------------------------------------------
        # 1. Selective Swing (15:20 이후 찐 주도주 홀딩 예외 룰)
        # -------------------------------------------------------------------
        if now_time >= time(15, 20):
            if current_velocity >= 3.0 and thrust > 2.0 and magnetic > 0:
                pos.status = "OVERNIGHT" 
                return None 

        # -------------------------------------------------------------------
        # 2. Time-based Exit (장 마감 강제 청산 - 예외 룰 통과 못한 종목)
        # -------------------------------------------------------------------
        if not getattr(self, 'debug_mode', False) and now_time >= getattr(self, 'forced_exit_time', time(15, 27)):
            return "Day Trade Close"

        # -------------------------------------------------------------------
        # 3. 🛡️ Heavy Exit Rules (동적 청산 룰 전면 개편)
        # -------------------------------------------------------------------
        
        # [Rule 1] 엔진 완전 소진 (Engine Dead): 속도/관성이 마이너스로 추락
        if current_velocity < 0.0:
            return "Kinetic Exit (Engine Dead: Velocity < 0)"
        
        # Track A: 플래시 덤프 (Flash Dump) 방어
        # 최대 30초(최근 3틱) 이내의 범위에서, 틱 개수(1~3)와 무관하게 누적 합이 -3.0 이하면 즉각 로스컷!
        recent_3 = state['forces'][-3:] 
        
        # 💡 [수정] len == 3 조건을 완전히 삭제! 
        if all(f <= 0 for f in recent_3) and sum(recent_3) <= -3.0:
            return f"Kinetic Exit (Flash Dump: {sum(recent_3):.2f})"

        # Track B: 블리딩 (Bleeding) 방어
        # 서서히 가라앉는 배는 최소 60초(6틱)의 누적된 흐름을 확인한 후 판단 (이곳은 len == 6 유지)
        if len(state['forces']) == 6 and sum(state['forces']) <= -4.5:
            return f"Kinetic Exit (Bleeding: {sum(state['forces']):.2f})"
            
        # [Rule 3] 가격 기반 트레일링 스탑 (Price-based Trailing Stop): 고점 대비 1.5% 하락
        if state['max_price'] > 0:
            drawdown = (current_price / state['max_price'] - 1) * 100
            if drawdown <= -1.5:
                return f"Trailing Stop ({drawdown:.2f}%)"

        # -------------------------------------------------------------------
        # 4. 기존 ATR 기반 Dynamic Stop-Loss 로직 유지
        # -------------------------------------------------------------------
        profit_rate = (current_price / buy_price - 1) * 100
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
        """[진입 판단] 동적 탈출 속도(Dynamic Escape Velocity) 기반 순수 물리 진입"""
        
        stock_code, total_score = metrics.stock_code, metrics.total_score
        forces = getattr(metrics, 'forces', {})
        
        # 물리 엔진 파라미터 추출
        thrust = forces.get('thrust', 0.0)
        current_velocity = forces.get('current_velocity', 0.0)
        impulse = forces.get('impulse', 0.0)
        magnetic = forces.get('magnetic', 0.0)
        jerk = forces.get('jerk', 0.0)
        gravity = forces.get('gravity', 0.0)
        
        momentum = self._get_momentum(stock_code, total_score)
        
        is_buy_signal = False
        status = "관망"

        # -------------------------------------------------------------------
        # 🛡️ [Trap Shield] 진입 방어 규칙 (세력의 덫 원천 차단)
        # -------------------------------------------------------------------
        if thrust >= 1.5 and gravity <= -0.9:
            status = "🌋고점과열 차단 (Climax Shield)"
            
        elif jerk <= -0.1:
            status = "🧊가속도 둔화 차단 (Jerk Filter)"

        # -------------------------------------------------------------------
        # 🚀 투트랙(Two-Track) 순수 동역학 진입 로직
        # -------------------------------------------------------------------
        # 대전제: 엔진이 켜져 있고(thrust > 0) 위로 가속이 붙기 시작할 것(momentum > 0)
        elif thrust > 0.0 and momentum > 0.0:
            
            # [Track 1] 우주 공간 (정배열 추세): 
            # 이미 물 위로 올라와 상승 관성(velocity > 0)을 탔다면 가벼운 수급만으로도 돌파 매수!
            if current_velocity > 0.0:
                is_buy_signal = True
                status = "🔥추세돌파 (Uptrend)"
                
            # [Track 2] 심해 탈출 (역배열 V자 반등):
            # 아직 물 속(velocity <= 0)이지만, 방금 우리가 고쳐놓은 '1.6억 이상 대포알(Impulse)'이나
            # '매도호가 진공 흡입(Magnetic)' 센서가 켜졌다면 세력의 찐타점으로 인정하고 바닥 매수!
            elif impulse > 0.0 or magnetic > 0.0:
                is_buy_signal = True
                status = "🚀바닥반등 (Reversal Boost)"
                
            else:
                status = "👀예열중 (Warming Up)"
                
        elif thrust > 0.0:
            status = "⚠️엔진점화 (Ignition only)"

        return {
            "score": total_score,
            "momentum": momentum,
            "status": status,
            "regime": self._current_regime,
            "is_buy_signal": is_buy_signal,
            "price": metrics.cur_prc,
            "stock_code": stock_code,
            "atr_percent": getattr(metrics, 'atr_percent', 0.5),
            "forces": forces 
        }