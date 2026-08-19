import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from numbers import Real
from typing import Callable, Dict, List, Mapping, Optional

from kiwoom_stock.application.ports import (
    MarketDataCollectionError,
    MarketDataFailureKind,
    MarketDataGateway,
    PaperTradeLedger,
    PaperTradePersistenceError,
    PositionTransitionReceipt,
)
from kiwoom_stock.application.session import CycleContext
from kiwoom_stock.domain.models import Position, PositionStatus
from kiwoom_stock.domain.strategy import (
    StrategySemanticsValidationError,
    calculate_position_return_percentage_points,
)
from kiwoom_stock.utils.market_cal import (
    KrxCalendarError,
    current_krx_session,
    next_krx_session,
    require_aware_kst,
    seoul_now,
)

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)


def _validated_atr(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or float(value) < 0.0
    ):
        raise StrategySemanticsValidationError(
            f"{name} must be a finite nonnegative number"
        )
    return float(value)


def _validated_tick_price(value: object) -> float:
    """Return the positive current-tick price used for every paper fill."""

    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise PaperTradePersistenceError(
            "paper fill requires a positive finite current-tick price"
        )
    return float(value)

class StockManager:
    """
    [Helper] 종목 및 인벤토리 관리자: 감시 종목 및 보유 종목 상태 관리
    
    """
    def __init__(
        self,
        market_gateway: MarketDataGateway,
        db: PaperTradeLedger,
        filter_config: Dict,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        current_session_resolver: Callable[[datetime], Optional[date]] = (
            current_krx_session
        ),
        next_session_resolver: Callable[[date], date] = next_krx_session,
        paper_transition_guard: Callable[[], None] = lambda: None,
        strict_paper_errors: bool = False,
    ):
        self.market_gateway = market_gateway
        self.db = db
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 50)
        self._clock = clock or seoul_now
        self._current_session_resolver = current_session_resolver
        self._next_session_resolver = next_session_resolver
        self._paper_transition_guard = paper_transition_guard
        self._strict_paper_errors = strict_paper_errors
        
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}

        raw_positions = self.db.load_active_positions()
        candidates: Dict[str, Position] = {}
        for code, data in raw_positions.items():
            candidates[code] = Position(**data)
        self.active_positions = candidates

    def _now(self) -> datetime:
        try:
            return require_aware_kst(self._clock(), "position lifecycle clock")
        except KrxCalendarError:
            raise
        except Exception as error:
            raise KrxCalendarError("position lifecycle clock failed") from error

    def _current_session(self, now: datetime) -> Optional[date]:
        try:
            return self._current_session_resolver(now)
        except KrxCalendarError:
            raise
        except Exception as error:
            raise KrxCalendarError("current XKRX session lookup failed") from error

    @staticmethod
    def _assert_transition_receipt(
        receipt: PositionTransitionReceipt,
        position: Position,
        *,
        previous_status: PositionStatus,
        status: PositionStatus,
        owning_session_date: date,
        state_changed_at: datetime,
    ) -> None:
        if not isinstance(receipt, PositionTransitionReceipt):
            raise PaperTradePersistenceError("invalid paper transition receipt")
        if (
            receipt.position_id != position.id
            or receipt.stock_code != position.stock_code
            or receipt.previous_status is not previous_status
            or receipt.status is not status
            or receipt.owning_session_date != owning_session_date
            or receipt.state_changed_at != state_changed_at
        ):
            raise PaperTradePersistenceError("paper transition receipt mismatch")

    def update_target_stocks(self):
        """
        [Manager] 보유 종목을 최우선으로 포함하여 감시 리스트를 갱신합니다.
        
        """
        upper_list = self.market_gateway.get_top_trading_value(market_tp="001")
        if not isinstance(upper_list, Sequence) or isinstance(
            upper_list,
            (str, bytes, bytearray),
        ):
            raise MarketDataCollectionError(
                MarketDataFailureKind.MALFORMED,
                "top_trading_value",
            )
        if not upper_list:
            raise MarketDataCollectionError(
                MarketDataFailureKind.EMPTY,
                "top_trading_value",
            )

        new_stocks = []
        new_names = dict(self.stock_names)
        seen_codes = set()
        for item in upper_list:
            if len(new_stocks) >= self.max_stocks:
                break
            if not isinstance(item, Mapping):
                raise MarketDataCollectionError(
                    MarketDataFailureKind.MALFORMED,
                    "top_trading_value",
                )
            code = item.get("stk_cd")
            name = item.get("stk_nm")
            if (
                not isinstance(code, str)
                or not code
                or not isinstance(name, str)
                or not name
            ):
                raise MarketDataCollectionError(
                    MarketDataFailureKind.MALFORMED,
                    "top_trading_value",
                )
            if any(keyword in name for keyword in self.etf_keywords):
                continue
            if code not in seen_codes:
                new_stocks.append(code)
                seen_codes.add(code)
                new_names[code] = name

        for code, position in self.active_positions.items():
            if code not in seen_codes:
                new_stocks.append(code)
                seen_codes.add(code)
                new_names[code] = position.stock_name

        self.stocks, self.stock_names = new_stocks, new_names
        logger.info(
            "감시 종목 갱신 (총 %d개 | 보유: %d개 포함)",
            len(self.stocks),
            len(self.active_positions),
        )

    def update_position_data(self, verdict: Dict):
        """
        [Manager] 보유 종목의 상태를 최신화하고 매도 사유(Exit Reason)가 있는지 평가합니다.
        
        """
        stock_code = verdict['stock_code']

        if stock_code not in self.active_positions:
            return None

        pos = self.active_positions[stock_code]
        current_price = verdict.get("price")
        calculate_position_return_percentage_points(
            getattr(pos, "buy_price", None),
            current_price,
        )
        atr_percent = _validated_atr(verdict.get("atr_percent"), "atr_percent")
        down_atr_percent = _validated_atr(
            verdict.get("down_atr_percent"),
            "down_atr_percent",
        )

        # [New] ATR 정보를 Position 객체에 임시 저장 (메모리 전용)
        # DB 스키마에 없어도 객체 속성으로는 동적 할당 가능
        pos.sell_price = current_price
        pos.atr_percent = atr_percent
        pos.down_atr_percent = down_atr_percent
        
        return pos

    def calculate_cumulative_trade_return_score(
        self,
        realized_trade_return_score: float,
    ) -> float:
        """Add realized and active per-trade percentage-point returns without weighting."""
        if (
            isinstance(realized_trade_return_score, bool)
            or not isinstance(realized_trade_return_score, Real)
            or not math.isfinite(float(realized_trade_return_score))
        ):
            raise StrategySemanticsValidationError(
                "realized_trade_return_score must be a finite non-boolean number"
            )
        active_score = sum(
            position.calc_profit_rate for position in self.active_positions.values()
        )
        score = float(realized_trade_return_score) + active_score
        if not math.isfinite(score):
            raise StrategySemanticsValidationError(
                "cumulative_trade_return_score must be finite"
            )
        return score

    def calculate_fresh_cumulative_trade_return_score(
        self,
        realized_trade_return_score: float,
        fresh_active_marks: Mapping[str, float],
    ) -> float:
        """Purely score every active position from this cycle's exact fresh mark."""

        active_codes = set(self.active_positions)
        if set(fresh_active_marks) != active_codes:
            raise StrategySemanticsValidationError(
                "fresh active marks must exactly cover all active positions"
            )
        active_score = sum(
            calculate_position_return_percentage_points(
                self.active_positions[code].buy_price,
                fresh_active_marks[code],
            )
            for code in sorted(active_codes)
        )
        if (
            isinstance(realized_trade_return_score, bool)
            or not isinstance(realized_trade_return_score, Real)
            or not math.isfinite(float(realized_trade_return_score))
        ):
            raise StrategySemanticsValidationError(
                "realized_trade_return_score must be a finite non-boolean number"
            )
        score = float(realized_trade_return_score) + active_score
        if not math.isfinite(score):
            raise StrategySemanticsValidationError(
                "cumulative_trade_return_score must be finite"
            )
        return score

    def get_total_pnl_status(self, realized_pnl: float) -> float:
        """Deprecated compatibility wrapper for one migration window."""
        import warnings

        warnings.warn(
            "get_total_pnl_status() is deprecated; use "
            "calculate_cumulative_trade_return_score().",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.calculate_cumulative_trade_return_score(realized_pnl)

    def apply_paper_buy(
        self,
        verdict: Dict,
        context: Optional[CycleContext] = None,
    ) -> tuple[bool, Optional[Dict]]:
        """
        [Manager] 브로커 주문 없이 paper 매수 상태만 기록합니다.

        """
        stock_code = verdict['stock_code']
        forces = verdict.get('forces', {})

        try:
            tick_price = _validated_tick_price(verdict.get('price'))
            now = context.now if context is not None else self._now()
            owning_session_date = (
                context.xkrx_session_date
                if context is not None
                else self._current_session(now)
            )
            if owning_session_date is None:
                raise KrxCalendarError(
                    "paper buy requires a current regular XKRX session"
                )
            # 1. 최종 paper buy_data 구성
            buy_data = {
                "stock_code": stock_code,
                "stock_name": self.stock_names[stock_code],
                "buy_price": tick_price,

                # 개별 물리적 힘 매핑 (없을 경우 0.0)
                "thrust": forces.get('thrust', 0.0),
                "gravity": forces.get('gravity', 0.0),
                "drag": forces.get('drag', 0.0),
                "magnetic": forces.get('magnetic', 0.0),
                "jerk": forces.get('jerk', 0.0),
                "impulse": forces.get('impulse', 0.0),
                "net_force": forces.get('net_force', 0.0),

                "buy_time": now.strftime('%Y-%m-%d %H:%M:%S'),
                "buy_regime": verdict.get('regime'),
                "status": PositionStatus.OPEN,
                "owning_session_date": owning_session_date,
                "state_changed_at": now,
            }
            
            # 2. 격리된 paper DB 기록 및 내부 포지션 업데이트
            self._paper_transition_guard()
            buy_data['id'] = self.db.record_buy(buy_data)
            self.active_positions[stock_code] = Position(**buy_data)
            
            return True, buy_data

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            if self._strict_paper_errors:
                raise
            return False, None

    def apply_paper_sell(
        self,
        verdict: Dict,
        reason: str,
        context: Optional[CycleContext] = None,
    ) -> tuple[bool, Optional[Position]]:
        """
        [Manager] 브로커 주문 없이 paper 매도 상태만 기록합니다.
        """
        stock_code = verdict['stock_code']

        try:
            tick_price = _validated_tick_price(verdict.get('price'))
            pos = self.active_positions[stock_code]
            if pos.status is not PositionStatus.OPEN:
                raise PaperTradePersistenceError(
                    "only OPEN can become CLOSED"
                )
            candidate = replace(
                pos,
                sell_price=tick_price,
                sell_reason=reason,
            )
            if pos.owning_session_date is None:
                raise PaperTradePersistenceError(
                    "active position has no owning session metadata"
                )

            self._paper_transition_guard()
            receipt = (
                self.db.record_sell(candidate, state_changed_at=context.now)
                if context is not None
                else self.db.record_sell(candidate)
            )
            self._assert_transition_receipt(
                receipt,
                pos,
                previous_status=PositionStatus.OPEN,
                status=PositionStatus.CLOSED,
                owning_session_date=pos.owning_session_date,
                state_changed_at=receipt.state_changed_at,
            )
            pos.sell_price = candidate.sell_price
            pos.sell_reason = candidate.sell_reason
            pos.sell_time = receipt.state_changed_at.strftime("%Y-%m-%d %H:%M:%S")
            pos.profit_rate = candidate.calc_profit_rate
            pos.status = receipt.status
            pos.state_changed_at = receipt.state_changed_at
            del self.active_positions[pos.stock_code]
            
            return True, pos

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            if self._strict_paper_errors:
                raise
            return False, None

    def apply_paper_mark_overnight(
        self,
        position: Position,
        context: Optional[CycleContext] = None,
    ) -> Position:
        """Durably mark one OPEN position before publishing memory state."""

        if position.status is not PositionStatus.OPEN:
            raise PaperTradePersistenceError("only OPEN can become OVERNIGHT")
        if position.owning_session_date is None:
            raise PaperTradePersistenceError("OPEN position has no owning session")
        now = context.now if context is not None else self._now()
        current_session = (
            context.xkrx_session_date
            if context is not None
            else self._current_session(now)
        )
        if current_session != position.owning_session_date:
            raise PaperTradePersistenceError(
                "overnight mark requires the owning regular XKRX session"
            )
        self._paper_transition_guard()
        receipt = self.db.mark_position_overnight(
            position,
            state_changed_at=now,
        )
        self._assert_transition_receipt(
            receipt,
            position,
            previous_status=PositionStatus.OPEN,
            status=PositionStatus.OVERNIGHT,
            owning_session_date=position.owning_session_date,
            state_changed_at=now,
        )
        position.status = receipt.status
        position.state_changed_at = receipt.state_changed_at
        return position

    def reconcile_overnight_positions(
        self,
        context: Optional[CycleContext] = None,
    ) -> int:
        """Reopen stale OVERNIGHT rows once in the current regular session."""

        now = context.now if context is not None else self._now()
        current_session = (
            context.xkrx_session_date
            if context is not None
            else self._current_session(now)
        )
        if current_session is None:
            return 0
        reopened = 0
        for position in tuple(self.active_positions.values()):
            if position.status is not PositionStatus.OVERNIGHT:
                continue
            owner = position.owning_session_date
            if owner is None:
                raise PaperTradePersistenceError(
                    "OVERNIGHT position has no owning session"
                )
            try:
                next_session = self._next_session_resolver(owner)
            except KrxCalendarError:
                raise
            except Exception as error:
                raise KrxCalendarError("next XKRX session lookup failed") from error
            if current_session == owner:
                continue
            if current_session < next_session:
                continue
            self._paper_transition_guard()
            receipt = self.db.reopen_position(
                position,
                owning_session_date=current_session,
                state_changed_at=now,
            )
            self._assert_transition_receipt(
                receipt,
                position,
                previous_status=PositionStatus.OVERNIGHT,
                status=PositionStatus.OPEN,
                owning_session_date=current_session,
                state_changed_at=now,
            )
            position.status = receipt.status
            position.owning_session_date = receipt.owning_session_date
            position.state_changed_at = receipt.state_changed_at
            reopened += 1
        return reopened

    def process_buy_order(self, verdict: Dict) -> tuple[bool, Optional[Dict]]:
        """Legacy paper-only compatibility wrapper; never submit a broker order here."""

        return self.apply_paper_buy(verdict)

    def process_sell_order(self, verdict: Dict, reason: str) -> tuple[bool, Optional[Position]]:
        """Legacy paper-only compatibility wrapper; never submit a broker order here."""

        return self.apply_paper_sell(verdict, reason)
