import logging
import math
from datetime import datetime, time, timedelta
from numbers import Real
from typing import Any, Callable, Dict, Optional, cast

from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.domain.models import (
    PositionDecision,
    PositionDecisionResult,
    PositionStatus,
)
from kiwoom_stock.domain.strategy import (
    StrategySemanticsValidationError,
    TargetStopPolicy,
    calculate_position_return_percentage_points,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


def _system_now() -> datetime:
    return cast(datetime, getattr(datetime, "now")())


class TradingStrategy:
    """
    [Strategy] 물리적 모멘텀 기반 트레이딩 전략
    - 특징: 물리 엔진이 산출한 가속도(모멘텀)와 속도(스코어)를 활용하여 가장 순수한 동역학 기반 판단을 내립니다.
    """
    
    def __init__(
        self,
        strategy_config: Dict,
        clock: Optional[Clock] = None,
        *,
        target_stop_policy: Optional[TargetStopPolicy] = None,
    ):
        if "total_loss_limit" in strategy_config:
            raise StrategySemanticsValidationError(
                "total_loss_limit dict input is unsupported; use "
                "cumulative_trade_return_score_floor"
            )
        target_stop_keys = {
            "target_profit_rate",
            "stop_loss_rate",
            "target_stop_unit_version",
            "target_profit_percentage_points",
            "stop_loss_percentage_points",
        }
        ambiguous = sorted(set(strategy_config).intersection(target_stop_keys))
        if ambiguous:
            raise StrategySemanticsValidationError(
                "target/stop dict keys are unsupported; pass TargetStopPolicy: "
                + ", ".join(ambiguous)
            )
        if target_stop_policy is not None and not isinstance(
            target_stop_policy,
            TargetStopPolicy,
        ):
            raise TypeError("target_stop_policy must be a TargetStopPolicy")
        self.settings = strategy_config
        self.debug_mode = strategy_config.get("debug_mode", False)
        self._clock = clock or _system_now

        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        dummy_dt = datetime.combine(datetime.min.date(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()

        self._current_regime: Any = MarketRegime.UNKNOWN
        self._cached_config: Dict[str, Any] = {}

        self.target_stop_policy = target_stop_policy or TargetStopPolicy()

        raw_score_floor = strategy_config.get(
            "cumulative_trade_return_score_floor",
            -5.0,
        )
        if (
            isinstance(raw_score_floor, bool)
            or not isinstance(raw_score_floor, Real)
            or not math.isfinite(float(raw_score_floor))
            or float(raw_score_floor) > 0.0
        ):
            raise StrategySemanticsValidationError(
                "cumulative_trade_return_score_floor must be a finite "
                "non-boolean number at or below zero"
            )
        self.cumulative_trade_return_score_floor = float(raw_score_floor)
        deadline_time_str = strategy_config.get("entry_deadline", "15:00")
        self.deadline_time = time.fromisoformat(deadline_time_str)

        # 청산 동력을 추적
        self._kinetic_state: Dict[str, Dict[str, Any]] = {}

    def _resolve_now(self, now: Optional[datetime] = None) -> datetime:
        return now if now is not None else self._clock()

    @property
    def target_stop_unit_version(self) -> str:
        return self.target_stop_policy.unit_version

    @property
    def target_profit_percentage_points(self) -> float:
        return self.target_stop_policy.target_profit_percentage_points

    @property
    def stop_loss_percentage_points(self) -> float:
        return self.target_stop_policy.stop_loss_percentage_points

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

    def is_monitoring_time(self, now: Optional[datetime] = None) -> bool:
        if self.debug_mode:
            return True
        current = self._resolve_now(now)
        if current.weekday() >= 5:
            return False
        return time(9, 0) <= current.time() <= self.exit_time_obj

    def is_trading_window(self, now: Optional[datetime] = None) -> bool:
        if self.debug_mode:
            return True
        current = self._resolve_now(now)
        return time(9, 0) <= current.time() < self.deadline_time

    def is_kill_switch_activated(self, cumulative_trade_return_score: float) -> bool:
        if (
            isinstance(cumulative_trade_return_score, bool)
            or not isinstance(cumulative_trade_return_score, Real)
            or not math.isfinite(float(cumulative_trade_return_score))
        ):
            raise StrategySemanticsValidationError(
                "cumulative_trade_return_score must be a finite "
                "non-boolean number"
            )
        return (
            float(cumulative_trade_return_score)
            <= self.cumulative_trade_return_score_floor
        )

    def get_exit_reason(
        self,
        pos,
        current_price: float,
        forces: Dict,
        now: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        [청산 판단] V2.5 비대칭 스마트 방어망 & Dual-Trigger Sniper (Zero-Time)
        """
        if current_price <= 0.0:
            # 주가가 0원이면 VI(단일가 매매) 발동 중이므로, 
            # 어떠한 청산도 집행하지 않고 관성을 유지함 (손익 -100% 오작동 방지).
            return None
        
        now_time = self._resolve_now(now).time()
        stock_code = pos.stock_code
        buy_price = getattr(pos, "buy_price", None)
        profit_rate = calculate_position_return_percentage_points(
            buy_price,
            current_price,
        )
        buy_price = cast(float, buy_price)

        if pos.status == PositionStatus.OVERNIGHT:
            return None

        # -------------------------------------------------------------------
        # 0. 전략 내부 상태(Kinetic State) 초기화 및 갱신
        # -------------------------------------------------------------------
        if profit_rate >= self.target_profit_percentage_points:
            return (
                "Fixed Target "
                f"({self.target_profit_percentage_points:g} %p; "
                f"{self.target_stop_unit_version})"
            )
        if profit_rate <= -self.stop_loss_percentage_points:
            return (
                "Fixed Stop "
                f"(-{self.stop_loss_percentage_points:g} %p; "
                f"{self.target_stop_unit_version})"
            )

        state = self._kinetic_state.get(stock_code, {})
        if state.get('buy_price') != buy_price:
            state = {
                'buy_price': buy_price,
                'max_price': buy_price
            }
            self._kinetic_state[stock_code] = state
            
        current_velocity = forces.get('current_velocity', 0.0)
        thrust = forces.get('thrust', 0.0)
        magnetic = forces.get('magnetic', 0.0)
        jerk = forces.get('jerk', 0.0)

        # 고점 가격 트래킹 업데이트
        if current_price > state['max_price']:
            state['max_price'] = current_price

        # -------------------------------------------------------------------
        # 1. 예외 룰 및 강제 청산 룰
        # -------------------------------------------------------------------
        if now_time >= time(15, 20):
            if current_velocity >= 3.0 and thrust > 2.0 and magnetic > 0:
                return None

        if not getattr(self, 'debug_mode', False) and now_time >= getattr(self, 'forced_exit_time', time(15, 27)):
            return "Day Trade Close"

        # -------------------------------------------------------------------
        # 2. 비대칭 스마트 방어망 (Universal Shield) 변수 산출
        # -------------------------------------------------------------------
        current_atr = getattr(pos, 'atr_percent', 0.5)
        current_down_atr = getattr(pos, 'down_atr_percent', 0.5)
        max_price = state.get('max_price', buy_price)
        
        max_profit_rate = (max_price / buy_price - 1) * 100
        drawdown_from_max = (current_price / max_price - 1) * 100      

        up_atr = max(0.1, current_atr - current_down_atr)
        drop_limit = -(up_atr * 3.0)

        # -------------------------------------------------------------------
        # 3. 🏃‍♂️ 조기 탈출 (Bail-out) 순수 동역학 규칙
        # -------------------------------------------------------------------
        # Flash Crash (폭포수 붕괴) 감지
        if profit_rate <= -1.5 and jerk <= -1.0:
            return f"Bail-out (Flash Crash Detected: {profit_rate:.2f}%)"
            
        # Negative Jerk (일반 역분사) 감지
        elif jerk <= -0.5 and thrust < 1.0:
            return f"Bail-out (Negative Jerk: {profit_rate:.2f}%)"

        # -------------------------------------------------------------------
        # 4. 🔥 [V2.5 Rule] 고고도(수익 2.0% 이상) 잠금장치 및 에너지 보존
        # -------------------------------------------------------------------
        if max_profit_rate >= 2.0:
            # [Rule 1] 이중 잠금 스나이퍼 (초고도 예민 반응)
            if jerk < 0.0 and drawdown_from_max <= -(up_atr * 1.0):
                return f"Bail-out (Sniper Exit: Jerk Drop at High Altitude)"
                
            # [Rule 2] 에너지 보존 법칙 (켈리 베팅식 바닥 보장)
            dynamic_drop_limit = max(-(up_atr * 3.0), -(max_profit_rate * 0.5))
            if drawdown_from_max <= dynamic_drop_limit:
                return f"Trailing Stop (Profit Retention)"
                
        # -------------------------------------------------------------------
        # 5. 🛡️ 초기 손절 및 일반 이익 보존 대통합 (Universal Shield)
        # -------------------------------------------------------------------
        else:
            if drawdown_from_max <= drop_limit:
                if current_price > buy_price:
                    return f"Trailing Stop (Peak: +{max_profit_rate:.2f}%, Drop: {drawdown_from_max:.2f}%)"
                else:
                    return f"Stop Loss (Universal: {drawdown_from_max:.2f}%)"

        return None

    def decide_position(
        self,
        pos,
        current_price: float,
        forces: Dict,
        now: Optional[datetime] = None,
    ) -> PositionDecisionResult:
        """Return typed intent without mutating the position."""

        reason = self.get_exit_reason(pos, current_price, forces, now)
        if reason is not None:
            return PositionDecisionResult(PositionDecision.SELL, reason)
        resolved_now = self._resolve_now(now)
        if (
            current_price > 0.0
            and pos.status == PositionStatus.OPEN
            and resolved_now.time() >= time(15, 20)
            and forces.get("current_velocity", 0.0) >= 3.0
            and forces.get("thrust", 0.0) > 2.0
            and forces.get("magnetic", 0.0) > 0.0
        ):
            return PositionDecisionResult(PositionDecision.MARK_OVERNIGHT)
        return PositionDecisionResult(PositionDecision.HOLD)

    def evaluate(self, metrics: SupplyData) -> Dict:
        """[진입 판단] 동적 탈출 속도 기반 순수 물리 진입 (V2.5 Thrust Lock)"""
        
        stock_code = metrics.stock_code
        forces = getattr(metrics, 'forces', {})

        if metrics.cur_prc <= 0.0:
            return {
                "status": "⏸️VI 발동 대기 (0원 호가 무시)",
                "regime": getattr(self._current_regime, 'name', str(self._current_regime)),
                "is_buy_signal": False,
                "price": metrics.cur_prc,
                "stock_code": stock_code,
                "atr_percent": 0.0,
                "down_atr_percent": 0.0,
                "forces": forces 
            }
        
        thrust = forces.get('thrust', 0.0)
        current_velocity = forces.get('current_velocity', 0.0)
        impulse = forces.get('impulse', 0.0)
        magnetic = forces.get('magnetic', 0.0)
        jerk = forces.get('jerk', 0.0)
        gravity = forces.get('gravity', 0.0)
        net_force = forces.get('net_force', 0.0)
        volume_drop_ratio = forces.get('volume_drop_ratio', 1.0)
        
        atr_percent = getattr(metrics, 'atr_percent', 0.5)
        down_atr_percent = getattr(metrics, 'down_atr_percent', 0.5)
        up_atr = max(0.1, atr_percent - down_atr_percent)
                
        # 기본값 초기화
        is_buy_signal = False
        status = "대기"

        # ===================================================================
        # 🚀 순수 동역학 진입 통제 센터 (Kinematic Entry Control)
        # ===================================================================

        # [Stage 0] 절대 진입 금지 (Absolute Blacklist)
        # ➔ 절대 살 수 없는 세력의 피날레 구간
        if thrust >= 1.5 and gravity <= -0.9:
            status = "🌋고점과열 차단 (Climax Shield)"
            is_buy_signal = False

        # [Stage 1] 진성 돌파 하이패스 (Breakout Override)
        # ➔ Climax가 아닌 일반적인 저항(Net Force < 0)을 자본으로 찢고 가는 주도주
        elif impulse >= 3.0 and jerk >= 0.5 and thrust >= 1.0:
            status = "🚀진성 돌파 (Breakout Override)"
            is_buy_signal = True

        # [Stage 2] 물리적 하드 록 (Hard Locks)
        elif thrust < 0.8:
            status = "🛑수급 빈곤 (Thrust Low)"
            is_buy_signal = False
            
        elif net_force < 0.0:
            status = "🛑합력 역전 (Net Force < 0)"
            is_buy_signal = False
            
        elif gravity <= -0.9 and thrust < 1.0:
            status = "🛬고공 실속 차단 (Stall Shield)"
            is_buy_signal = False
            
        elif down_atr_percent > 0 and (up_atr / down_atr_percent) < 1.5:
            status = "💨더러운 추세 (Low Quality)"
            is_buy_signal = False

        elif volume_drop_ratio < 0.5 and thrust < 1.5:
            status = "💨연료 고갈 차단 (Volume Exhausted)"
            is_buy_signal = False

        # [Stage 3] 정상 궤도 가동 (Standard Entry Triggers)
        # ➔ 위의 락을 모두 무사히 통과한 종목 중, 가속 페달(Jerk)을 밟은 경우 탑승
        elif jerk > 0.0:
            if current_velocity > 0.0:
                status = "🔥추세돌파 (Uptrend)"
                is_buy_signal = True
            elif impulse > 0.0 or magnetic > 0.0:
                status = "🚀바닥반등 (Reversal Boost)"
                is_buy_signal = True
            else:
                status = "👀예열중 (Warming Up)"
                is_buy_signal = False
                
        else:
            status = "📉가속도 감소 (Jerk <= 0)"
            is_buy_signal = False

        return {
            "status": status,
            "regime": getattr(self._current_regime, 'name', str(self._current_regime)),
            "is_buy_signal": is_buy_signal,
            "price": metrics.cur_prc,
            "stock_code": stock_code,
            "atr_percent": atr_percent,
            "down_atr_percent": down_atr_percent,
            "forces": forces 
        }
