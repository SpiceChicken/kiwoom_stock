"""
[Engine] 트레이딩 시스템의 핵심 실행 엔진 (Physics-Sniper Version)
- 역할: 모듈 조율 및 동적 폴링(Dynamic Polling) 기반 흐름 제어
- 원칙: '관심도'에 따라 종목별 검사 주기(Interval)를 다르게 배정하여 API 제한을 완벽 방어한다.
"""

import logging
import threading
import time as time_mod
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Union, cast
from concurrent.futures import ThreadPoolExecutor, as_completed

from kiwoom_stock.application.ports import (
    MarketDataGateway,
    PaperTradeLedger,
    PaperTradePersistenceError,
    PhysicalStateRepository,
)
from kiwoom_stock.application.session import (
    CriticalNotificationOutcome,
    CycleContext,
    SessionEndReason,
    TradingSessionResult,
)
from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.domain.models import (
    PhysicalContinuityEvidence,
    PositionDecision,
    PositionStatus,
)
from kiwoom_stock.domain.strategy import TargetStopPolicy
from kiwoom_stock.utils.market_cal import (
    KrxCalendarError,
    current_krx_session,
    require_aware_kst,
    seoul_now,
)

logger = logging.getLogger(__name__)

# Legacy tests and callers may replace this constructor seam. Keeping the name
# does not import the optional Slack/Gemini/reporting graph for shadow startup.
Notifier: Optional[Any] = None


class TradingEngineLifecycleError(RuntimeError):
    """One or more engine-owned resources failed during shutdown."""


class TradingEngine:
    """[Control Tower] 트레이딩 시스템 메인 컨트롤러"""
    
    def __init__(
        self,
        client,
        config: Dict,
        *,
        ledger: PaperTradeLedger,
        physical_state_repository: PhysicalStateRepository,
        market_gateway: MarketDataGateway,
        target_stop_policy: Optional[TargetStopPolicy] = None,
        notifier: Optional[Any] = None,
        paper_transition_guard: Optional[Callable[[], None]] = None,
        wall_clock: Callable[[], datetime] = seoul_now,
        stop_event: Optional[threading.Event] = None,
        deadline_remaining: Optional[Callable[[], float]] = None,
    ):
        self.config = config
        self._lifecycle_lock = threading.Lock()
        self._close_complete = threading.Event()
        self._closing = False
        self._closed = False
        self._work_closed = False
        self._executor_close_complete = False
        self._physical_close_complete = False
        self._ledger_close_complete = False
        self._lifecycle_failure: Optional[Tuple[str, ...]] = None
        self._lifecycle_process_control: Optional[BaseException] = None
        
        # -------------------------------------------------------------
        # 🚀 [동적 폴링 타이머 세팅]
        # fast_interval: 보유 중이거나 엔진이 켜진 종목 (10초 단위 밀착 감시)
        # slow_interval: 죽어있는 심해 종목 (60초 단위 여유 감시)
        # -------------------------------------------------------------
        self.fast_interval = config.get("fast_interval", 10)
        self.slow_interval = config.get("slow_interval", 60)
        
        self._last_check_time: Dict[str, float] = {}
        self._last_global_update = 0.0  # 시황/조건검색 업데이트용 타이머
        self._terminal_result: Optional[TradingSessionResult] = None
        self._wall_clock = wall_clock
        self._paper_only = paper_transition_guard is not None
        self._stop_event = stop_event
        self._deadline_remaining = deadline_remaining
        self._shadow_cycle_lock = threading.Lock()
        self._shadow_cycle_state = "not-started"

        self.db = ledger
        self.physical_state_repository = physical_state_repository

        try:
            # [Modules] 기능별 모듈 초기화
            self.state_tracker = (
                PhysicalStateTracker(self.physical_state_repository, clock=wall_clock)
                if self._paper_only
                else PhysicalStateTracker(self.physical_state_repository)
            )
            analyzer_kwargs: Dict[str, Any] = {}
            if self._paper_only:
                analyzer_kwargs["clock"] = wall_clock
            self.analyzer = MarketAnalyzer(
                market_gateway,
                config.get("market", {}),
                self.state_tracker,
                **analyzer_kwargs,
            )
            strategy_kwargs: Dict[str, Any] = {"clock": wall_clock}
            if target_stop_policy is not None:
                strategy_kwargs["target_stop_policy"] = target_stop_policy
            self.strategy = TradingStrategy(
                config.get("strategy", {}),
                **strategy_kwargs,
            )
            manager_kwargs: Dict[str, Any] = {"clock": wall_clock}
            if paper_transition_guard is not None:
                manager_kwargs["paper_transition_guard"] = paper_transition_guard
                manager_kwargs["strict_paper_errors"] = True
            self.stock_mgr = StockManager(
                market_gateway, self.db, config.get("filters", {}), **manager_kwargs
            )
            if notifier is not None:
                self.notifier = notifier
            else:
                notifier_factory: Any = Notifier
                if notifier_factory is None:
                    from .notifier import Notifier as notifier_factory
                self.notifier = notifier_factory(self.stock_mgr.stock_names, config)
            self.executor = ThreadPoolExecutor(max_workers=config.get("max_workers", 8))
        except BaseException:
            raise
        logger.info("Trading Engine Initialized with Dynamic Polling.")

    def run_shadow_cycle(self, stock_code: str) -> Dict[str, Any]:
        """Atomically consume the sole shadow calculation capability."""

        if not self._paper_only:
            raise RuntimeError("shadow cycle requires the paper-only engine")
        with self._shadow_cycle_lock:
            if self._shadow_cycle_state != "not-started":
                raise RuntimeError("shadow engine cycle capability is already consumed")
            self._shadow_cycle_state = "running"
        try:
            self._checkpoint_shadow_lifecycle()
            self._assert_open_for_work()
            if stock_code != "005930":
                raise ValueError("shadow cycle target must be 005930")
            self.analyzer.update_regime()
            self._checkpoint_shadow_lifecycle()
            if self.analyzer.market_regime.name == "UNKNOWN":
                raise RuntimeError("shadow market regime remained UNKNOWN")
            self.strategy.update_context(self.analyzer.market_regime)
            self._checkpoint_shadow_lifecycle()
            self.stock_mgr.stocks = [stock_code]
            self.stock_mgr.stock_names.setdefault(stock_code, stock_code)
            self._prepare_cycle([stock_code])
            self._checkpoint_shadow_lifecycle()
            metrics = self.analyzer.supply_cache.get(stock_code)
            if metrics is None:
                raise RuntimeError("shadow market snapshot did not produce supply metrics")
            required_forces = {
                "thrust", "gravity", "drag", "magnetic", "jerk", "impulse",
                "net_force", "current_velocity", "volume_drop_ratio",
            }
            if metrics.cur_prc <= 0.0 or set(getattr(metrics, "forces", {})) != required_forces:
                raise RuntimeError("shadow market calculation contract failed")
            if not isinstance(metrics.continuity, PhysicalContinuityEvidence):
                raise RuntimeError("shadow continuity evidence is unavailable")
            verdicts = self._evaluate_stocks([stock_code])
            self._checkpoint_shadow_lifecycle()
            if len(verdicts) != 1:
                raise RuntimeError("shadow strategy evaluation did not produce one verdict")
            self._process_decisions(verdicts)
            self._checkpoint_shadow_lifecycle()
            self.notifier.flush_status(self.analyzer.market_regime.value)
            self._checkpoint_shadow_lifecycle()
            return {
                "cycles": 1,
                "target_count": 1,
                "verdict_count": len(verdicts),
                "market_regime": self.analyzer.market_regime.value,
                "continuity": metrics.continuity,
            }
        finally:
            with self._shadow_cycle_lock:
                self._shadow_cycle_state = "terminal"

    def _checkpoint_shadow_lifecycle(self) -> None:
        """Abort cooperative shadow work before another side-effect boundary."""

        stop_event = getattr(self, "_stop_event", None)
        deadline_remaining = getattr(self, "_deadline_remaining", None)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("shadow stop requested")
        if deadline_remaining is not None:
            try:
                deadline_remaining()
            except Exception as error:
                raise RuntimeError("shadow shutdown deadline exceeded") from error

    def run(self) -> TradingSessionResult:
        """[Main Loop] 무한 루프: 분석 -> 판단 -> 행동"""
        if getattr(self, "_paper_only", False):
            raise RuntimeError(
                "paper-only engine exposes run_shadow_cycle only"
            )
        terminal_result = cast(
            Optional[TradingSessionResult], getattr(self, "_terminal_result", None)
        )
        if terminal_result is not None:
            return terminal_result

        self._assert_open_for_work()

        logger.info(f"Engine Start (Fast Track: {self.fast_interval}s / Slow Track: {self.slow_interval}s)")
        
        while True:
            try:
                context = self._create_cycle_context()
                if context is None:
                    return TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED)
                now = time_mod.time()
                terminal_result = self._run_normal_cycle(context, now)
                if terminal_result is not None:
                    return terminal_result
                
                # 메인 루프는 1초마다 초고속으로 회전함 (종목별 간격 조절은 _get_due_targets가 담당)
                time_mod.sleep(1)

            except KeyboardInterrupt:
                logger.warning("User Terminated.")
                return TradingSessionResult(reason=SessionEndReason.USER_INTERRUPT)
            except Exception as e:
                logger.critical(f"Main Loop Error: {e}", exc_info=True)
                self.notifier.notify_error(str(e))
                time_mod.sleep(10)

    def _create_cycle_context(self) -> Optional[CycleContext]:
        """Read the normal-cycle wall clock exactly once and resolve its session."""

        try:
            now = require_aware_kst(self._wall_clock(), "normal cycle clock")
            session_date = current_krx_session(now)
        except KrxCalendarError:
            raise
        except Exception as error:
            raise KrxCalendarError("normal cycle context is unavailable") from error
        if session_date is None:
            return None
        return CycleContext(now=now, xkrx_session_date=session_date)

    def _run_normal_cycle(
        self,
        context: CycleContext,
        selected_at: float,
    ) -> Optional[TradingSessionResult]:
        """Run one fail-closed normal cycle in the authoritative safety order."""

        terminal = cast(
            Optional[TradingSessionResult], getattr(self, "_terminal_result", None)
        )
        if terminal is not None:
            return terminal
        if not self._check_monitoring_status(context):
            return TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED)

        self.stock_mgr.reconcile_overnight_positions(context)

        if selected_at - getattr(self, '_last_global_update', 0) >= 60.0:
            self.analyzer.update_regime()
            self.strategy.update_context(self.analyzer.market_regime)
            self.stock_mgr.update_target_stocks()
            self._last_global_update = selected_at

        due_targets = self._get_due_targets()
        if not due_targets:
            return None

        batch_targets = self._complete_cycle_targets(due_targets)
        self._prepare_cycle(batch_targets)
        fresh_marks = self._fresh_active_marks(batch_targets)
        terminal = self._check_terminal_status(context, fresh_marks)
        if terminal is not None:
            return terminal

        self._ack_due_targets(due_targets, selected_at)
        verdicts = self._evaluate_stocks(batch_targets)
        self._process_decisions(verdicts, context)
        self.notifier.flush_status(self.analyzer.market_regime.value)
        return None

    def _complete_cycle_targets(self, due_targets: List[str]) -> List[str]:
        """Include every active code in one deterministic fresh batch generation."""

        if not due_targets or len(due_targets) != len(set(due_targets)):
            raise ValueError("due targets must be a non-empty unique batch")
        return sorted(set(due_targets).union(self.stock_mgr.active_positions))

    def _fresh_active_marks(self, batch_targets: List[str]) -> Mapping[str, float]:
        """Extract exact marks only from the just-published complete batch."""

        batch_codes = set(batch_targets)
        active_codes = set(self.stock_mgr.active_positions)
        if not active_codes.issubset(batch_codes):
            raise RuntimeError("fresh batch omitted an active position")
        marks: Dict[str, float] = {}
        for code in sorted(active_codes):
            metrics = self.analyzer.supply_cache.get(code)
            if metrics is None:
                raise RuntimeError(f"fresh active mark is unavailable: {code}")
            marks[code] = metrics.cur_prc
        return marks

    def _assert_open_for_work(self) -> None:
        if (
            getattr(self, "_work_closed", False)
            or getattr(self, "_closing", False)
            or getattr(self, "_closed", False)
        ):
            raise RuntimeError("TradingEngine is closed and cannot start new work")

    def _raise_lifecycle_failure(self) -> None:
        process_control = getattr(self, "_lifecycle_process_control", None)
        if process_control is not None:
            raise process_control
        failure = getattr(self, "_lifecycle_failure", None)
        if failure:
            raise TradingEngineLifecycleError(
                "TradingEngine close failed: " + "; ".join(failure)
            )

    def close(self) -> None:
        """Stop evaluation, then close state submission and the shared ledger."""
        with self._lifecycle_lock:
            if self._closed:
                close_owner = False
                close_event = None
            elif self._closing:
                close_owner = False
                close_event = self._close_complete
            else:
                self._work_closed = True
                self._closing = True
                self._close_complete = threading.Event()
                close_owner = True
                close_event = self._close_complete

        if not close_owner and close_event is not None:
            close_event.wait()
        elif close_owner:
            failures = []
            process_control: Optional[BaseException] = None

            def record_failure(label: str, error: BaseException) -> None:
                nonlocal process_control
                if isinstance(error, Exception):
                    failures.append(
                        f"{label} ({type(error).__name__}): {error}"
                    )
                elif process_control is None:
                    process_control = error
                logger.exception("Failed to close engine-owned %s.", label)

            try:
                if not getattr(self, "_executor_close_complete", False):
                    try:
                        stop_event = getattr(self, "_stop_event", None)
                        deadline_remaining = getattr(
                            self, "_deadline_remaining", None
                        )
                        bounded_stop = bool(
                            stop_event is not None and stop_event.is_set()
                        )
                        if not bounded_stop and deadline_remaining is not None:
                            try:
                                deadline_remaining()
                            except Exception:
                                bounded_stop = True
                        self.executor.shutdown(
                            wait=not bounded_stop,
                            cancel_futures=bounded_stop,
                        )
                    except BaseException as error:
                        record_failure("evaluation executor", error)
                    finally:
                        self._executor_close_complete = True

                if not getattr(self, "_physical_close_complete", False):
                    try:
                        self.physical_state_repository.close()
                    except BaseException as error:
                        record_failure("physical-state repository", error)
                    finally:
                        self._physical_close_complete = True

                if not getattr(self, "_ledger_close_complete", False):
                    ledger_complete = False
                    try:
                        self.db.close()
                    except BaseException as error:
                        record_failure("paper ledger", error)
                        try:
                            ledger_complete = getattr(self.db, "is_closed", False) is True
                        except BaseException as observation_error:
                            record_failure(
                                "paper ledger terminal observation",
                                observation_error,
                            )
                    else:
                        ledger_complete = True
                    if ledger_complete:
                        self._ledger_close_complete = True
            finally:
                with self._lifecycle_lock:
                    if failures and self._lifecycle_failure is None:
                        self._lifecycle_failure = tuple(failures)
                    if (
                        process_control is not None
                        and self._lifecycle_process_control is None
                    ):
                        self._lifecycle_process_control = process_control
                    self._closing = False
                    self._closed = (
                        self._executor_close_complete
                        and self._physical_close_complete
                        and self._ledger_close_complete
                    )
                assert close_event is not None
                close_event.set()

        self._raise_lifecycle_failure()

    def _get_due_targets(self) -> List[str]:
        """[Scheduler] 투트랙 인터벌 정책에 따라 현재 검사해야 할 종목 리스트 반환"""
        now = time_mod.time()
        due_stocks = []
        
        for code in self.stock_mgr.stocks:
            last_checked = self._last_check_time.get(code, 0.0)
            
            # 상태 추출
            is_active = code in self.stock_mgr.active_positions
            cached_data = self.analyzer.supply_cache.get(code)
            last_strength = getattr(cached_data, 'strength', 0.0) if cached_data else 0.0
            
            # [핵심 로직] 내 돈이 들어갔거나, 세력이 개입(체결강도 100 이상)한 종목은 Fast Track
            if is_active or last_strength >= 100.0:
                target_interval = self.fast_interval
            else:
                target_interval = self.slow_interval

            # 시간이 도래한 종목만 스캔 리스트에 추가
            if now - last_checked >= target_interval:
                due_stocks.append(code)

        return due_stocks

    def _ack_due_targets(self, targets: List[str], selected_at: float) -> None:
        """Advance polling timestamps only after one successful batch publish."""

        if not targets or len(targets) != len(set(targets)):
            raise ValueError("polling acknowledgement targets must be unique")
        if any(code not in self.stock_mgr.stocks for code in targets):
            raise ValueError("polling acknowledgement target is not selected")
        if isinstance(selected_at, bool) or not isinstance(selected_at, (int, float)):
            raise TypeError("polling acknowledgement timestamp must be numeric")
        next_check_time = dict(self._last_check_time)
        next_check_time.update({code: float(selected_at) for code in targets})
        self._last_check_time = next_check_time

    def _check_monitoring_status(self, context: CycleContext) -> bool:
        """Check only terminal/session monitoring admission for this context."""

        if getattr(self, "_terminal_result", None) is not None:
            return False
        if not self.strategy.is_monitoring_time(context.now):
            logger.info("Outside of trading hours.")
            return False
        return True

    def _check_terminal_status(
        self,
        context: CycleContext,
        fresh_active_marks: Mapping[str, float],
    ) -> Optional[TradingSessionResult]:
        """Purely score current fresh marks, then latch a terminal result if due."""

        realized_score = self.db.get_cumulative_realized_trade_return_score(
            context.xkrx_session_date
        )
        cumulative_score = (
            self.stock_mgr.calculate_fresh_cumulative_trade_return_score(
                realized_score,
                fresh_active_marks,
            )
        )
        if self.strategy.is_kill_switch_activated(cumulative_score):
            self._terminal_result = self._create_kill_switch_result(cumulative_score)
            return self._terminal_result
        return None

    def _create_kill_switch_result(
        self,
        cumulative_trade_return_score: float,
    ) -> TradingSessionResult:
        """Latch a terminal stop without creating orders or mutating the ledger."""
        score_floor = self.strategy.cumulative_trade_return_score_floor
        unresolved_codes = tuple(sorted(self.stock_mgr.active_positions))
        message = (
            "🚨 KILL-SWITCH ACTIVATED — "
            f"Cumulative trade return score: {cumulative_trade_return_score:.1f} "
            "percentage-points; "
            f"floor: {score_floor:.1f} percentage-points. "
            "No automatic liquidation was attempted. "
            f"Unresolved active positions: {len(unresolved_codes)}."
        )
        logger.critical(message)

        try:
            self.notifier.notify_critical(message)
        except (Exception, KeyboardInterrupt) as error:
            logger.exception("Critical notifier callable raised during kill-switch stop.")
            return TradingSessionResult(
                reason=SessionEndReason.KILL_SWITCH,
                cumulative_trade_return_score=cumulative_trade_return_score,
                cumulative_trade_return_score_floor=score_floor,
                unresolved_position_codes=unresolved_codes,
                critical_notification_outcome=CriticalNotificationOutcome.CALL_RAISED,
                critical_notification_error_type=type(error).__name__,
            )

        return TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            cumulative_trade_return_score=cumulative_trade_return_score,
            cumulative_trade_return_score_floor=score_floor,
            unresolved_position_codes=unresolved_codes,
            critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
        )

    def _prepare_cycle(self, targets: List[str]):
        """[Pre-process] 선택된 타겟(Fast or Slow)의 최신 API 데이터만 선별 수집"""
        self._checkpoint_shadow_lifecycle()
        self._assert_open_for_work()
        # [최적화] 전체 50개가 아닌 due_targets(예: 3개)에 대해서만 API 호출
        self.analyzer.update_priority_supply(targets)
        self._checkpoint_shadow_lifecycle()
        self.notifier.start_status_session()

    def _evaluate_stocks(self, targets: List[str]) -> List[Dict]:
        """[Parallel] 워커 스레드를 통한 전략 평가"""
        self._checkpoint_shadow_lifecycle()
        self._assert_open_for_work()
        results = []
        futures = {self.executor.submit(self._worker_task, code): code for code in targets}
        
        for f in as_completed(futures):
            try:
                self._checkpoint_shadow_lifecycle()
                if res := f.result(): results.append(res)
            except Exception as e:
                stop_event = getattr(self, "_stop_event", None)
                if stop_event is not None and stop_event.is_set():
                    raise
                try:
                    self._checkpoint_shadow_lifecycle()
                except RuntimeError:
                    raise
                logger.error(f"Eval Error ({futures[f]}): {e}")
        self._checkpoint_shadow_lifecycle()
        return sorted(results, key=lambda verdict: verdict["stock_code"])

    def _worker_task(self, code: str) -> Optional[Dict]:
        """[Worker] 단위 작업: 데이터 조회 + 전략 계산"""
        self._checkpoint_shadow_lifecycle()
        metrics = self.analyzer.supply_cache.get(code)
        if not metrics: return None
        
        if verdict := self.strategy.evaluate(metrics):
            self._checkpoint_shadow_lifecycle()
            return verdict
        return None

    def _process_decisions(
        self,
        verdicts: List[Dict],
        context: Optional[CycleContext] = None,
    ):
        """[Decision] 전략 결과를 바탕으로 매수/매도/관망 결정 (Orchestrator 역할)"""
        if context is None:
            self.stock_mgr.reconcile_overnight_positions()
        for verdict in verdicts:
            self._checkpoint_shadow_lifecycle()
            stock_code = verdict['stock_code']

            # 1. 매도(청산) 검사 - 보유 중인 경우
            if stock_code in self.stock_mgr.active_positions:
                persisted = self.stock_mgr.active_positions[stock_code]
                if persisted.status is PositionStatus.OVERNIGHT:
                    decision_args = (
                        persisted,
                        verdict["price"],
                        verdict.get("forces", {}),
                    )
                    overnight_decision = (
                        self.strategy.decide_position(*decision_args, context.now)
                        if context is not None
                        else self.strategy.decide_position(*decision_args)
                    )
                    if overnight_decision.decision is not PositionDecision.HOLD:
                        raise PaperTradePersistenceError(
                            "OVERNIGHT must reconcile to OPEN before a decision transition"
                        )
                    self._log_status(verdict)
                    continue
                pos = self.stock_mgr.update_position_data(verdict)
                if not pos: continue

                self._log_status(verdict)
                forces = verdict.get('forces', {})
                decision_args = (
                    pos,
                    verdict['price'],
                    forces,
                )
                decision = (
                    self.strategy.decide_position(*decision_args, context.now)
                    if context is not None
                    else self.strategy.decide_position(*decision_args)
                )

                if decision.decision is PositionDecision.SELL:
                    assert decision.reason is not None
                    if pos.status is not PositionStatus.OPEN:
                        raise PaperTradePersistenceError(
                            "only OPEN can become CLOSED"
                        )
                    if getattr(self, "_paper_only", False):
                        self._execute_paper_transition(
                            'SELL', verdict, decision.reason, context
                        )
                    else:
                        self._execute_order('SELL', verdict, decision.reason, context)
                    if hasattr(self.strategy, '_kinetic_state'):
                        self.strategy._kinetic_state.pop(stock_code, None)
                elif decision.decision is PositionDecision.MARK_OVERNIGHT:
                    self.stock_mgr.apply_paper_mark_overnight(pos, context)
                    if hasattr(self.strategy, '_kinetic_state'):
                        self.strategy._kinetic_state.pop(stock_code, None)
            
            # 2. 매수(진입) 검사 - 미보유 시
            else:
                self._log_status(verdict)
                if self._should_enter(verdict, context):
                    if getattr(self, "_paper_only", False):
                        self._execute_paper_transition('BUY', verdict, context=context)
                    else:
                        self._execute_order('BUY', verdict, context=context)

    def _execute_paper_transition(
        self,
        side: str,
        verdict: Dict,
        reason: Optional[str] = None,
        context: Optional[CycleContext] = None,
    ) -> None:
        """Apply a local shadow-paper transition with no broker capability."""

        code = verdict['stock_code']
        success = False
        data: Union[Dict, Position, None] = None
        if side == 'BUY':
            success, data = self.stock_mgr.apply_paper_buy(verdict, context)
            if success and isinstance(data, dict):
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            success, data = self.stock_mgr.apply_paper_sell(
                verdict, reason if reason else "Unknown", context
            )
            if success and isinstance(data, Position):
                self.notifier.notify_sell(data)
        if not success:
            raise RuntimeError(f"shadow paper {side} transition failed: {code}")

    def _should_enter(
        self,
        verdict: Dict,
        context: Optional[CycleContext] = None,
    ) -> bool:
        """[Filter] 진입 조건 종합 검증 (시간, 중복, 신호)"""
        return (
            self.strategy.is_trading_window(
                context.now if context is not None else None
            ) and
            verdict['is_buy_signal'] and
            verdict['stock_code'] not in self.stock_mgr.active_positions
        )

    def _execute_order(
        self,
        side: str,
        verdict: Dict,
        reason: Optional[str] = None,
        context: Optional[CycleContext] = None,
    ):
        """[Execution] 매매 집행 통합 메서드"""
        code = verdict['stock_code']
        success = False
        data: Union[Dict, Position, None] = None

        if side == 'BUY':
            success, data = self.stock_mgr.apply_paper_buy(verdict, context)
            if success and isinstance(data, dict): 
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            safe_reason = reason if reason else "Unknown"
            success, data = self.stock_mgr.apply_paper_sell(
                verdict, safe_reason, context
            )
            if success and isinstance(data, Position):
                self.notifier.notify_sell(data)

        if not success:
            logger.error(f"❌ {side} Order Failed: {code}")

    def _log_status(self, verdict: Dict):
        """Notifier로 상태 전송"""
        try:
            self.notifier.collect_status({
                "name": self.stock_mgr.stock_names.get(verdict['stock_code'], verdict['stock_code']),
                "price": verdict["price"],
                "reason": verdict['status'],
                "forces": verdict.get('forces', {}) 
            })
        except Exception:
            pass
