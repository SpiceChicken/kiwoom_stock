"""Typed orchestration boundaries for one :class:`TradingEngine` cycle.

The engine remains the compatibility facade.  This module owns only the
ordering and data-shaping rules for a normal cycle; broker capability,
strategy policy, persistence, and notification implementations stay behind
callbacks supplied by the facade.
"""

from __future__ import annotations

from concurrent.futures import Executor, as_completed
from dataclasses import dataclass
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence

from kiwoom_stock.application.session import (
    CycleContext,
    SessionEndReason,
    TradingSessionResult,
)


Verdict = Dict[str, Any]


@dataclass(frozen=True)
class NormalCycleOutcome:
    """Result of a normal cycle and the next scheduler watermark."""

    terminal_result: Optional[TradingSessionResult]
    next_global_update: float


def complete_cycle_targets(
    due_targets: Sequence[str], active_positions: Collection[str]
) -> List[str]:
    """Return one deterministic fresh batch containing all active positions."""

    if not due_targets or len(due_targets) != len(set(due_targets)):
        raise ValueError("due targets must be a non-empty unique batch")
    return sorted(set(due_targets).union(active_positions))


def acknowledge_due_targets(
    targets: Sequence[str],
    selected_at: float,
    selected_stocks: Collection[str],
    previous_check_time: Mapping[str, float],
) -> Dict[str, float]:
    """Advance polling timestamps only after a successful batch publish."""

    if not targets or len(targets) != len(set(targets)):
        raise ValueError("polling acknowledgement targets must be unique")
    if any(code not in selected_stocks for code in targets):
        raise ValueError("polling acknowledgement target is not selected")
    if isinstance(selected_at, bool) or not isinstance(selected_at, (int, float)):
        raise TypeError("polling acknowledgement timestamp must be numeric")
    next_check_time = dict(previous_check_time)
    next_check_time.update({code: float(selected_at) for code in targets})
    return next_check_time


def fresh_active_marks(
    batch_targets: Collection[str],
    active_positions: Collection[str],
    supply_cache: Mapping[str, Any],
) -> Mapping[str, float]:
    """Extract marks only from the just-published complete market batch."""

    batch_codes = set(batch_targets)
    active_codes = set(active_positions)
    if not active_codes.issubset(batch_codes):
        raise RuntimeError("fresh batch omitted an active position")
    marks: Dict[str, float] = {}
    for code in sorted(active_codes):
        metrics = supply_cache.get(code)
        if metrics is None:
            raise RuntimeError(f"fresh active mark is unavailable: {code}")
        marks[code] = metrics.cur_prc
    return marks


def evaluate_cycle_stocks(
    targets: Sequence[str],
    *,
    executor: Executor,
    worker_task: Callable[[str], Optional[Verdict]],
    checkpoint: Callable[[], None],
    assert_open_for_work: Callable[[], None],
    stop_requested: Callable[[], bool],
    log_evaluation_error: Callable[[str], None],
) -> List[Verdict]:
    """Evaluate a target batch with the engine's bounded worker semantics."""

    checkpoint()
    assert_open_for_work()
    results: List[Verdict] = []
    futures = {
        executor.submit(worker_task, code): code
        for code in targets
    }
    for future in as_completed(futures):
        try:
            checkpoint()
            verdict = future.result()
            if verdict:
                results.append(verdict)
        except Exception as error:
            if stop_requested():
                raise
            try:
                checkpoint()
            except RuntimeError:
                raise
            log_evaluation_error(f"Eval Error ({futures[future]}): {error}")
    checkpoint()
    return sorted(results, key=lambda verdict: verdict["stock_code"])


class TradingCycleCoordinator:
    """Coordinate one normal cycle without owning engine resources."""

    def __init__(
        self,
        *,
        check_monitoring_status: Callable[[CycleContext], bool],
        reconcile_overnight_positions: Callable[[CycleContext], None],
        refresh_global_state: Callable[[], None],
        get_due_targets: Callable[[], List[str]],
        complete_targets: Callable[[List[str]], List[str]],
        prepare_cycle: Callable[[List[str]], None],
        fresh_active_marks: Callable[[List[str]], Mapping[str, float]],
        check_terminal_status: Callable[
            [CycleContext, Mapping[str, float]], Optional[TradingSessionResult]
        ],
        acknowledge_targets: Callable[[List[str], float], None],
        evaluate_stocks: Callable[[List[str]], List[Verdict]],
        process_decisions: Callable[[List[Verdict], CycleContext], None],
        flush_status: Callable[[str], None],
        market_regime_value: Callable[[], str],
        global_update_interval: float = 60.0,
    ) -> None:
        self._check_monitoring_status = check_monitoring_status
        self._reconcile_overnight_positions = reconcile_overnight_positions
        self._refresh_global_state = refresh_global_state
        self._get_due_targets = get_due_targets
        self._complete_targets = complete_targets
        self._prepare_cycle = prepare_cycle
        self._fresh_active_marks = fresh_active_marks
        self._check_terminal_status = check_terminal_status
        self._acknowledge_targets = acknowledge_targets
        self._evaluate_stocks = evaluate_stocks
        self._process_decisions = process_decisions
        self._flush_status = flush_status
        self._market_regime_value = market_regime_value
        self._global_update_interval = global_update_interval

    def run(
        self,
        context: CycleContext,
        selected_at: float,
        last_global_update: float,
    ) -> NormalCycleOutcome:
        """Execute the authoritative normal-cycle ordering."""

        if not self._check_monitoring_status(context):
            return NormalCycleOutcome(
                TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
                last_global_update,
            )

        self._reconcile_overnight_positions(context)
        next_global_update = last_global_update
        if selected_at - last_global_update >= self._global_update_interval:
            self._refresh_global_state()
            next_global_update = selected_at

        due_targets = self._get_due_targets()
        if not due_targets:
            return NormalCycleOutcome(None, next_global_update)

        batch_targets = self._complete_targets(due_targets)
        self._prepare_cycle(batch_targets)
        fresh_marks = self._fresh_active_marks(batch_targets)
        terminal = self._check_terminal_status(context, fresh_marks)
        if terminal is not None:
            return NormalCycleOutcome(terminal, next_global_update)

        self._acknowledge_targets(due_targets, selected_at)
        verdicts = self._evaluate_stocks(batch_targets)
        self._process_decisions(verdicts, context)
        self._flush_status(self._market_regime_value())
        return NormalCycleOutcome(None, next_global_update)
