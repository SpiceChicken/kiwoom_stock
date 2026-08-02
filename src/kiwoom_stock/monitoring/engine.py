"""
[Engine] 트레이딩 시스템의 핵심 실행 엔진 (Physics-Sniper Version)
- 역할: 모듈 조율 및 동적 폴링(Dynamic Polling) 기반 흐름 제어
- 원칙: '관심도'에 따라 종목별 검사 주기(Interval)를 다르게 배정하여 API 제한을 완벽 방어한다.
"""

import logging
import threading
import time as time_mod
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

from kiwoom_stock.application.ports import PaperTradeLedger, PhysicalStateRepository
from kiwoom_stock.application.session import (
    CriticalNotificationOutcome,
    SessionEndReason,
    TradingSessionResult,
)
from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import AsyncPhysicalStateRepository
from kiwoom_stock.monitoring.manager import Position

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
        ledger: Optional[PaperTradeLedger] = None,
        physical_state_repository: Optional[PhysicalStateRepository] = None,
        notifier: Optional[Any] = None,
        paper_transition_guard: Optional[Callable[[], None]] = None,
        wall_clock: Callable[[], datetime] = datetime.now,
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
        self._paper_only = paper_transition_guard is not None
        self._stop_event = stop_event
        self._deadline_remaining = deadline_remaining
        self._shadow_cycle_lock = threading.Lock()
        self._shadow_cycle_state = "not-started"

        if (ledger is None) != (physical_state_repository is None):
            raise ValueError(
                "ledger and physical_state_repository must be injected together"
            )

        fallback_owned = ledger is None
        if fallback_owned:
            warning_message = (
                "TradingEngine(client, config) persistence fallback is deprecated; "
                "inject the configured ledger and physical-state repository"
            )
            warnings.warn(warning_message, DeprecationWarning, stacklevel=2)
            logger.warning(warning_message)
            fallback_ledger = TradeLogger()
            try:
                fallback_repository = AsyncPhysicalStateRepository(fallback_ledger)
            except BaseException:
                self._close_constructor_fallback(None, fallback_ledger)
                raise
            ledger = fallback_ledger
            physical_state_repository = fallback_repository

        assert ledger is not None
        assert physical_state_repository is not None
        self.db = ledger
        self.physical_state_repository = physical_state_repository

        try:
            # [Modules] 기능별 모듈 초기화
            self.state_tracker = (
                PhysicalStateTracker(self.physical_state_repository, clock=wall_clock)
                if self._paper_only
                else PhysicalStateTracker(self.physical_state_repository)
            )
            self.analyzer = MarketAnalyzer(
                client.market,
                config.get("market", {}),
                self.state_tracker,
            )
            self.strategy = (
                TradingStrategy(config.get("strategy", {}), clock=wall_clock)
                if self._paper_only
                else TradingStrategy(config.get("strategy", {}))
            )
            manager_kwargs: Dict[str, Any] = {}
            if paper_transition_guard is not None:
                manager_kwargs["paper_transition_guard"] = paper_transition_guard
                manager_kwargs["clock"] = wall_clock
                manager_kwargs["strict_paper_errors"] = True
            self.stock_mgr = StockManager(
                client.market, self.db, config.get("filters", {}), **manager_kwargs
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
            if fallback_owned:
                self._close_constructor_fallback(
                    self.physical_state_repository,
                    self.db,
                )
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
                # 1. 운영 시간 및 리스크 점검
                if not self._check_system_status():
                    terminal_result = cast(
                        Optional[TradingSessionResult],
                        getattr(self, "_terminal_result", None),
                    )
                    if terminal_result is not None:
                        return terminal_result
                    if not self.strategy.is_monitoring_time():
                        return TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED)
                    time_mod.sleep(60)
                    continue

                now = time_mod.time()

                # 2. [글로벌 업데이트] API 부하가 큰 시황/조건검색은 60초에 한 번만 갱신
                if now - getattr(self, '_last_global_update', 0) >= 60.0:
                    self.analyzer.update_regime()
                    self.strategy.update_context(self.analyzer.market_regime)
                    self.stock_mgr.update_target_stocks()
                    self._last_global_update = now

                # 3. [동적 폴링] 이번 틱(Tick)에 검사해야 할 타겟만 영리하게 추출
                due_targets = self._get_due_targets()

                if not due_targets:
                    # 검사할 종목이 없으면 엔진은 1초만 대기하고 바로 다음 루프 회전
                    time_mod.sleep(1)
                    continue

                # 4. 사이클 준비 및 평가 (추출된 타겟만 API 호출)
                self._prepare_cycle(due_targets)
                verdicts = self._evaluate_stocks(due_targets)

                # 5. 매매 의사 결정 및 집행
                self._process_decisions(verdicts)

                # 6. 주기적 리포팅
                self.notifier.flush_status(self.analyzer.market_regime.value)
                
                # 메인 루프는 1초마다 초고속으로 회전함 (종목별 간격 조절은 _get_due_targets가 담당)
                time_mod.sleep(1)

            except KeyboardInterrupt:
                logger.warning("User Terminated.")
                return TradingSessionResult(reason=SessionEndReason.USER_INTERRUPT)
            except Exception as e:
                logger.critical(f"Main Loop Error: {e}", exc_info=True)
                self.notifier.notify_error(str(e))
                time_mod.sleep(10)

    @staticmethod
    def _close_constructor_fallback(
        physical_state_repository: Optional[PhysicalStateRepository],
        ledger: PaperTradeLedger,
    ) -> None:
        """Release direct-constructor fallback resources without masking its error."""
        for label, resource in (
            ("physical-state repository", physical_state_repository),
            ("paper ledger", ledger),
        ):
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException:
                logger.exception(
                    "Failed to close fallback %s after engine construction error.",
                    label,
                )

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
                self._last_check_time[code] = now
                
        return due_stocks

    def _check_system_status(self) -> bool:
        """[Check 1] 장 운영 시간 및 킬스위치 점검"""
        if getattr(self, "_terminal_result", None) is not None:
            return False

        # 1. 시간 체크
        if not self.strategy.is_monitoring_time():
            logger.info("Outside of trading hours.")
            return False

        # 2. 킬스위치(누적 손실) 체크
        total_pnl = self.stock_mgr.get_total_pnl_status(self.db.get_today_realized_pnl())
        if self.strategy.is_kill_switch_activated(total_pnl):
            self._terminal_result = self._create_kill_switch_result(total_pnl)
            return False
            
        return True

    def _create_kill_switch_result(self, total_pnl: float) -> TradingSessionResult:
        """Latch a terminal stop without creating orders or mutating the ledger."""
        loss_limit = self.strategy.total_loss_limit
        unresolved_codes = tuple(sorted(self.stock_mgr.active_positions))
        message = (
            f"🚨 KILL-SWITCH ACTIVATED (PnL: {total_pnl:.1f}%, "
            f"Limit: {loss_limit:.1f}%) | 자동 청산을 실행하지 않았습니다. "
            f"미해결 활성 포지션: {len(unresolved_codes)}개"
        )
        logger.critical(message)

        try:
            self.notifier.notify_critical(message)
        except (Exception, KeyboardInterrupt) as error:
            logger.exception("Critical notifier callable raised during kill-switch stop.")
            return TradingSessionResult(
                reason=SessionEndReason.KILL_SWITCH,
                total_pnl=total_pnl,
                loss_limit=loss_limit,
                unresolved_position_codes=unresolved_codes,
                critical_notification_outcome=CriticalNotificationOutcome.CALL_RAISED,
                critical_notification_error_type=type(error).__name__,
            )

        return TradingSessionResult(
            reason=SessionEndReason.KILL_SWITCH,
            total_pnl=total_pnl,
            loss_limit=loss_limit,
            unresolved_position_codes=unresolved_codes,
            critical_notification_outcome=CriticalNotificationOutcome.CALL_RETURNED,
        )

    def _prepare_cycle(self, targets: List[str]):
        """[Pre-process] 선택된 타겟(Fast or Slow)의 최신 API 데이터만 선별 수집"""
        self._checkpoint_shadow_lifecycle()
        self._assert_open_for_work()
        for stock_code in targets:
            self._checkpoint_shadow_lifecycle()
            if stock_code not in self.state_tracker._l1_cache:
                self.state_tracker.recover_state_from_crash(stock_code)
                
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
        return results

    def _worker_task(self, code: str) -> Optional[Dict]:
        """[Worker] 단위 작업: 데이터 조회 + 전략 계산"""
        self._checkpoint_shadow_lifecycle()
        metrics = self.analyzer.supply_cache.get(code)
        if not metrics: return None
        
        if verdict := self.strategy.evaluate(metrics):
            self._checkpoint_shadow_lifecycle()
            return verdict
        return None

    def _process_decisions(self, verdicts: List[Dict]):
        """[Decision] 전략 결과를 바탕으로 매수/매도/관망 결정 (Orchestrator 역할)"""
        for verdict in verdicts:
            self._checkpoint_shadow_lifecycle()
            self._log_status(verdict)
            stock_code = verdict['stock_code']

            # 1. 매도(청산) 검사 - 보유 중인 경우
            if stock_code in self.stock_mgr.active_positions:
                pos = self.stock_mgr.update_position_data(verdict)
                if not pos: continue
                
                forces = verdict.get('forces', {}) 
                exit_reason = self.strategy.get_exit_reason(pos, verdict['price'], forces)
                
                if exit_reason:
                    if getattr(self, "_paper_only", False):
                        self._execute_paper_transition('SELL', verdict, exit_reason)
                    else:
                        self._execute_order('SELL', verdict, exit_reason)
                    if hasattr(self.strategy, '_kinetic_state'):
                        self.strategy._kinetic_state.pop(stock_code, None)
            
            # 2. 매수(진입) 검사 - 미보유 시
            elif self._should_enter(verdict):
                if getattr(self, "_paper_only", False):
                    self._execute_paper_transition('BUY', verdict)
                else:
                    self._execute_order('BUY', verdict)

    def _execute_paper_transition(
        self,
        side: str,
        verdict: Dict,
        reason: Optional[str] = None,
    ) -> None:
        """Apply a local shadow-paper transition with no broker capability."""

        code = verdict['stock_code']
        success = False
        data: Union[Dict, Position, None] = None
        if side == 'BUY':
            success, data = self.stock_mgr.apply_paper_buy(verdict)
            if success and isinstance(data, dict):
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            success, data = self.stock_mgr.apply_paper_sell(
                verdict, reason if reason else "Unknown"
            )
            if success and isinstance(data, Position):
                self.notifier.notify_sell(data)
        if not success:
            raise RuntimeError(f"shadow paper {side} transition failed: {code}")

    def _should_enter(self, verdict: Dict) -> bool:
        """[Filter] 진입 조건 종합 검증 (시간, 중복, 신호)"""
        return (
            self.strategy.is_trading_window() and
            verdict['is_buy_signal'] and
            verdict['stock_code'] not in self.stock_mgr.active_positions
        )

    def _execute_order(self, side: str, verdict: Dict, reason: Optional[str] = None):
        """[Execution] 매매 집행 통합 메서드"""
        code = verdict['stock_code']
        success = False
        data: Union[Dict, Position, None] = None

        if side == 'BUY':
            success, data = self.stock_mgr.process_buy_order(verdict)
            if success and isinstance(data, dict): 
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            safe_reason = reason if reason else "Unknown"
            success, data = self.stock_mgr.process_sell_order(verdict, safe_reason)
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
