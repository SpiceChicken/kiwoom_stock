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
        self.debug_mode = strategy_config.get("debug_mode", False)

        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config: Dict[str, Any] = {}

        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.03)
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.03)

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
        [청산 판단] 초민감도 제거 및 묵직한 가격/관성 기반 헤비 엑시트(Heavy Exit) 룰 적용
        """
        now_time = datetime.now().time()
        stock_code = pos.stock_code
        
        # 이미 오버나잇으로 확정된 종목은 당일 청산 로직 무시
        if pos.status == "OVERNIGHT":
            return None

        # -------------------------------------------------------------------
        # 0. 전략 내부 상태(Kinetic State) 초기화 및 갱신 (max_price만 추적)
        # -------------------------------------------------------------------
        buy_price = getattr(pos, 'buy_price', current_price)
        if buy_price <= 0: buy_price = current_price
        
        state = self._kinetic_state.setdefault(stock_code, {'max_price': buy_price})
        
        current_velocity = forces.get('current_velocity', 0.0)
        thrust = forces.get('thrust', 0.0)
        magnetic = forces.get('magnetic', 0.0)

        # 고점 가격 트래킹 업데이트
        if current_price > state['max_price']:
            state['max_price'] = current_price

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
        # 3. 🛡️ Heavy Exit Rules (초민감도 제거 완료)
        # -------------------------------------------------------------------
        
        # [Rule 1] 엔진 완전 소진 (Engine Dead 조건 완화)
        # 💡 [수정 2] 속도가 0 미만으로 살짝 빠졌다고 팔지 않고, -2.0 이하의 진짜 심해로 처박힐 때만 매도
        if current_velocity <= -2.0:
            return "Kinetic Exit (Engine Dead: V <= -2.0)"
            
        # [Rule 3] 가격 기반 트레일링 스탑 (Price-based Trailing Stop): 고점 대비 1.5% 하락
        if state.get('max_price', 0) > 0:
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

    def evaluate(self, metrics: SupplyData) -> Dict:
        """[진입 판단] 동적 탈출 속도(Dynamic Escape Velocity) 기반 순수 물리 진입"""
        
        stock_code = metrics.stock_code
        forces = getattr(metrics, 'forces', {})
        
        # 물리 엔진 파라미터 추출
        thrust = forces.get('thrust', 0.0)
        current_velocity = forces.get('current_velocity', 0.0)
        impulse = forces.get('impulse', 0.0)
        magnetic = forces.get('magnetic', 0.0)
        jerk = forces.get('jerk', 0.0)
        gravity = forces.get('gravity', 0.0)
                
        is_buy_signal = False
        status = "관망"

        # -------------------------------------------------------------------
        # 🛡️ [Trap Shield] 진입 방어 규칙
        # -------------------------------------------------------------------
        if thrust >= 1.5 and gravity <= -0.9:
            status = "🌋고점과열 차단 (Climax Shield)"
            
        # -------------------------------------------------------------------
        # 🚀 순수 동역학 진입 로직
        # -------------------------------------------------------------------
        # 💡 대전제 변경: thrust > 0.0 이고 가속도(Jerk)가 위로 향할 것!
        elif thrust > 0.0 and jerk > 0.0:
            if current_velocity > 0.0:
                is_buy_signal = True
                status = "🔥추세돌파 (Uptrend)"
            elif impulse > 0.0 or magnetic > 0.0:
                is_buy_signal = True
                status = "🚀바닥반등 (Reversal Boost)"
            else:
                status = "👀예열중 (Warming Up)"
                
        elif thrust > 0.0:
            status = "⚠️엔진점화 (Ignition only)"

        return {
            "status": status,
            "regime": getattr(self._current_regime, 'name', str(self._current_regime)),
            "is_buy_signal": is_buy_signal,
            "price": metrics.cur_prc,
            "stock_code": stock_code,
            "atr_percent": getattr(metrics, 'atr_percent', 0.5),
            "forces": forces 
        }