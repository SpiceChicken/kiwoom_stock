"""Bounded one-shot shadow use case."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable, Mapping, Protocol
from zoneinfo import ZoneInfo

import exchange_calendars as xcals

from kiwoom_stock.application.execution import (
    ExecutionMode,
    ExecutionPolicy,
    SHADOW_PROCESS_LOCK_PATH,
)
from kiwoom_stock.application.shadow_lifecycle import (
    ContinuousLifecycle,
    RunDeadline,
    SHADOW_CONTINUOUS_INTERVAL_SECONDS,
    SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS,
    SHUTDOWN_TIMEOUT_SECONDS,
    SignalLatch,
    ShadowRunDeadlineExceeded,
    ShadowShutdownDeadlineExceeded,
    ShadowStopRequested,
    ShutdownDeadline,
    check_lifecycle,
    signal_stop_event,
)


_SEOUL = ZoneInfo("Asia/Seoul")
SHADOW_EVIDENCE_SCHEMA_VERSION = 1


class ShadowWorkerError(RuntimeError):
    """Safe terminal shadow worker failure."""


class ShadowTerminalReason(str, Enum):
    STOP_REQUESTED = "stop-requested"
    RUN_DEADLINE = "run-deadline"
    SHUTDOWN_DEADLINE = "shutdown-deadline"
    FAILURE = "failure"
    CALENDAR_CLOSED = "calendar-closed"


class ShadowCycleTerminated(ShadowWorkerError):
    """Typed terminal outcome after runtime cleanup has completed."""

    def __init__(
        self,
        reason: ShadowTerminalReason,
        *,
        resources_closed: bool,
        error_type: str | None = None,
    ) -> None:
        self.reason = reason
        self.resources_closed = resources_closed
        self.error_type = error_type
        super().__init__("shadow cycle terminated")


class CalendarUnavailableError(ShadowWorkerError):
    """The calendar could not authoritatively classify the date."""


class CalendarDecision(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class ShadowAdmission:
    now: datetime
    kst_date: date
    decision: CalendarDecision
    stop_event: Event | None = None
    deadline_remaining: Callable[[], float] | None = None

    def clock(self) -> datetime:
        return self.now

    def checkpoint(self) -> None:
        check_lifecycle(
            stop_event=self.stop_event,
            deadline_remaining=self.deadline_remaining,
        )


@dataclass(frozen=True)
class ShadowExecutionReceipt:
    cycles: int
    http_attempts: int
    api_counts: Mapping[str, int]
    db_identity: str
    resources_closed: bool
    local_counts: Mapping[str, int]


class ShadowRuntimePort(Protocol):
    def execute_once(self) -> ShadowExecutionReceipt: ...


class StopEventPort(Protocol):
    def set(self) -> None: ...

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


class RuntimeStopEvent:
    """Raise the typed stop transition at runtime lifecycle checkpoints."""

    def __init__(
        self,
        event: StopEventPort,
        observe_stop: Callable[[], bool],
    ) -> None:
        self._event = event
        self._observe_stop = observe_stop
        self._terminal_raised = False

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            self._observe_stop()
            if not self._terminal_raised:
                self._terminal_raised = True
                raise ShadowStopRequested("shadow stop requested")
            return True
        return False

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


def seoul_now() -> datetime:
    return datetime.now(_SEOUL)


def strict_krx_calendar(target_date: date) -> CalendarDecision:
    try:
        calendar = xcals.get_calendar("XKRX")
        return (
            CalendarDecision.OPEN
            if calendar.is_session(target_date.isoformat())
            else CalendarDecision.CLOSED
        )
    except Exception:
        raise CalendarUnavailableError("KRX calendar decision is unavailable") from None


@dataclass(frozen=True)
class ShadowRunResult:
    status: str
    mode: str
    kst_date: str
    calendar: str
    source_sha: str
    image_digest: str
    activation_id: str
    stock_code: str
    proxy_code: str
    cycles: int
    http_attempts: int
    api_counts: Mapping[str, int]
    db_identity: str | None
    resources_closed: bool
    side_effects: Mapping[str, bool]
    local_counts: Mapping[str, int]

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SHADOW_EVIDENCE_SCHEMA_VERSION,
            "status": self.status,
            "mode": self.mode,
            "kst_date": self.kst_date,
            "calendar": self.calendar,
            "source_sha": self.source_sha,
            "image_digest": self.image_digest,
            "activation_id": self.activation_id,
            "stock_code": self.stock_code,
            "proxy_code": self.proxy_code,
            "cycles": self.cycles,
            "http_attempts": self.http_attempts,
            "api_counts": dict(self.api_counts),
            "db_identity": self.db_identity,
            "resources_closed": self.resources_closed,
            "side_effects": dict(self.side_effects),
            "local_counts": dict(self.local_counts),
        }


@dataclass(frozen=True)
class ShadowContinuousResult:
    """Redacted terminal summary for one bounded continuous process."""

    status: str
    mode: str
    source_sha: str
    image_digest: str
    activation_id: str
    cycles: int
    elapsed_seconds: float
    resources_closed: bool
    side_effects: Mapping[str, bool]
    reason: str
    error_type: str | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status == "FAILED" or not self.resources_closed else 0

    def to_safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": SHADOW_EVIDENCE_SCHEMA_VERSION,
            "event": "terminal",
            "status": self.status,
            "mode": self.mode,
            "source_sha": self.source_sha,
            "image_digest": self.image_digest,
            "activation_id": self.activation_id,
            "cycles": self.cycles,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "resources_closed": self.resources_closed,
            "side_effects": dict(self.side_effects),
            "reason": self.reason,
        }
        if self.error_type is not None:
            result["error_type"] = self.error_type
        return result


def run_shadow_once(
    policy: ExecutionPolicy,
    *,
    runtime_factory: Callable[[ExecutionPolicy, ShadowAdmission], ShadowRuntimePort],
    clock: Callable[[], datetime] = seoul_now,
    calendar: Callable[[date], CalendarDecision] = strict_krx_calendar,
    stop_event: Event | None = None,
    deadline_remaining: Callable[[], float] | None = None,
) -> ShadowRunResult:
    """Admit by KST calendar, build once, calculate once, and close once."""

    try:
        check_lifecycle(
            stop_event=stop_event,
            deadline_remaining=deadline_remaining,
        )
    except ShadowStopRequested:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.STOP_REQUESTED,
            resources_closed=True,
        ) from None
    except (ShadowRunDeadlineExceeded, ShadowShutdownDeadlineExceeded) as error:
        reason = (
            ShadowTerminalReason.RUN_DEADLINE
            if isinstance(error, ShadowRunDeadlineExceeded)
            else ShadowTerminalReason.SHUTDOWN_DEADLINE
        )
        raise ShadowCycleTerminated(reason, resources_closed=True) from None
    except Exception as error:
        raise ShadowWorkerError("shadow lifecycle budget rejected admission") from error
    now = clock()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ShadowWorkerError("shadow clock must return an aware datetime")
    kst_now = now.astimezone(_SEOUL)
    kst_date = kst_now.date()
    decision = calendar(kst_date)
    try:
        check_lifecycle(
            stop_event=stop_event,
            deadline_remaining=deadline_remaining,
        )
    except ShadowStopRequested:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.STOP_REQUESTED,
            resources_closed=True,
        ) from None
    except (ShadowRunDeadlineExceeded, ShadowShutdownDeadlineExceeded) as error:
        reason = (
            ShadowTerminalReason.RUN_DEADLINE
            if isinstance(error, ShadowRunDeadlineExceeded)
            else ShadowTerminalReason.SHUTDOWN_DEADLINE
        )
        raise ShadowCycleTerminated(reason, resources_closed=True) from None
    except Exception as error:
        raise ShadowWorkerError("shadow lifecycle budget rejected admission") from error
    if not isinstance(decision, CalendarDecision):
        raise CalendarUnavailableError("KRX calendar returned an invalid decision")
    activation = policy.activation
    common = {
        "mode": policy.mode.value,
        "kst_date": kst_date.isoformat(),
        "calendar": decision.value,
        "source_sha": activation.source_sha,
        "image_digest": activation.image_digest,
        "activation_id": activation.activation_id,
        "stock_code": policy.stock_code,
        "proxy_code": policy.proxy_code,
    }
    if decision is CalendarDecision.CLOSED:
        return ShadowRunResult(
            status="CLOSED",
            cycles=0,
            http_attempts=0,
            api_counts={},
            db_identity=None,
            resources_closed=True,
            side_effects=_zero_external_side_effects(),
            local_counts={},
            **common,
        )

    try:
        runtime = runtime_factory(
            policy,
            ShadowAdmission(
                now=kst_now,
                kst_date=kst_date,
                decision=decision,
                stop_event=stop_event,
                deadline_remaining=deadline_remaining,
            ),
        )
    except ShadowCycleTerminated:
        raise
    except ShadowStopRequested:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.STOP_REQUESTED,
            resources_closed=True,
        ) from None
    except ShadowRunDeadlineExceeded:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.RUN_DEADLINE,
            resources_closed=True,
        ) from None
    except ShadowShutdownDeadlineExceeded:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.SHUTDOWN_DEADLINE,
            resources_closed=True,
        ) from None
    receipt = runtime.execute_once()
    try:
        check_lifecycle(
            stop_event=stop_event,
            deadline_remaining=deadline_remaining,
        )
    except ShadowStopRequested:
        raise ShadowCycleTerminated(
            ShadowTerminalReason.STOP_REQUESTED,
            resources_closed=receipt.resources_closed,
        ) from None
    except (ShadowRunDeadlineExceeded, ShadowShutdownDeadlineExceeded) as error:
        reason = (
            ShadowTerminalReason.RUN_DEADLINE
            if isinstance(error, ShadowRunDeadlineExceeded)
            else ShadowTerminalReason.SHUTDOWN_DEADLINE
        )
        raise ShadowCycleTerminated(
            reason,
            resources_closed=receipt.resources_closed,
        ) from None
    except Exception as error:
        raise ShadowWorkerError("shadow lifecycle budget exceeded during execution") from error
    if receipt.cycles != 1:
        raise ShadowWorkerError("shadow runtime violated the one-cycle contract")
    if receipt.resources_closed is not True:
        raise ShadowWorkerError("shadow runtime did not close all resources")
    if not isinstance(receipt.http_attempts, int) or receipt.http_attempts < 0:
        raise ShadowWorkerError("shadow runtime reported invalid HTTP evidence")
    if receipt.http_attempts > policy.max_http_attempts:
        raise ShadowWorkerError("shadow HTTP attempt budget was exceeded")
    return ShadowRunResult(
        status="PASS",
        cycles=1,
        http_attempts=receipt.http_attempts,
        api_counts=receipt.api_counts,
        db_identity=receipt.db_identity,
        resources_closed=receipt.resources_closed,
        side_effects=_zero_external_side_effects(),
        local_counts=receipt.local_counts,
        **common,
    )


def run_shadow_once_managed(
    policy: ExecutionPolicy,
    *,
    runtime_factory: Callable[[ExecutionPolicy, ShadowAdmission], ShadowRuntimePort],
    lock_path: str | Path = SHADOW_PROCESS_LOCK_PATH,
    clock: Callable[[], datetime] = seoul_now,
    calendar: Callable[[date], CalendarDecision] = strict_krx_calendar,
    stop_event: Event | None = None,
    monotonic: Callable[[], float] | None = None,
    lock_factory: Callable[[str | Path], Any] | None = None,
) -> ShadowRunResult:
    """Run the one-shot worker under the process lock and signal adapter.

    The runtime remains the sole owner of resource cleanup; this wrapper only
    establishes process ownership and the bounded shutdown signal contract.
    """

    event = stop_event if stop_event is not None else Event()
    monotonic_clock = monotonic or time.monotonic
    deadline = ShutdownDeadline.start(
        monotonic=monotonic_clock,
        timeout=SHUTDOWN_TIMEOUT_SECONDS,
    )
    if lock_factory is None:
        raise ShadowWorkerError("shadow process lock adapter was not injected")
    with signal_stop_event(event):
        with lock_factory(lock_path):
            if event.is_set():
                raise ShadowWorkerError(
                    "shadow stop requested before admission "
                    f"(remaining={deadline.remaining(monotonic=monotonic_clock):.6f})"
                )
            try:
                result = run_shadow_once(
                    policy,
                    runtime_factory=runtime_factory,
                    clock=clock,
                    calendar=calendar,
                    stop_event=event,
                    deadline_remaining=lambda: deadline.remaining(
                        monotonic=monotonic_clock
                    ),
                )
            except ShadowCycleTerminated as error:
                raise ShadowWorkerError(
                    "shadow lifecycle budget terminated one-shot execution"
                ) from error
            deadline.remaining(monotonic=monotonic_clock)
            return result


def run_shadow_continuous(
    policy: ExecutionPolicy,
    *,
    runtime_factory: Callable[[ExecutionPolicy, ShadowAdmission], ShadowRuntimePort],
    emit: Callable[[Mapping[str, Any]], None],
    lock_path: str | Path = SHADOW_PROCESS_LOCK_PATH,
    clock: Callable[[], datetime] = seoul_now,
    calendar: Callable[[date], CalendarDecision] = strict_krx_calendar,
    stop_event: StopEventPort | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    lock_factory: Callable[[str | Path], Any] | None = None,
) -> ShadowContinuousResult:
    """Repeat the verified one-shot primitive under one bounded process owner."""

    if policy.mode is not ExecutionMode.SHADOW_CONTINUOUS:
        raise ShadowWorkerError("continuous runner requires shadow-continuous policy")
    if lock_factory is None:
        raise ShadowWorkerError("shadow process lock adapter was not injected")
    event: StopEventPort = stop_event if stop_event is not None else Event()
    deadline = RunDeadline.start(
        clock=monotonic,
        timeout_seconds=SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS,
    )
    signal_latch = SignalLatch(event, monotonic)  # type: ignore[arg-type]
    lifecycle = ContinuousLifecycle(
        stop_event=signal_latch,
        run_deadline=deadline,
        clock=monotonic,
    )
    runtime_stop_event = RuntimeStopEvent(signal_latch, lifecycle.stop_requested)
    activation = policy.activation
    cycles = 0
    last_closed = True

    def terminal(
        status: str,
        reason: ShadowTerminalReason,
        error_type: str | None = None,
    ) -> ShadowContinuousResult:
        return ShadowContinuousResult(
            status=status,
            mode=policy.mode.value,
            source_sha=activation.source_sha,
            image_digest=activation.image_digest,
            activation_id=activation.activation_id,
            cycles=cycles,
            elapsed_seconds=deadline.elapsed(clock=monotonic),
            resources_closed=last_closed,
            side_effects=_zero_external_side_effects(),
            reason=reason.value,
            error_type=error_type,
        )

    try:
        with signal_stop_event(signal_latch):  # type: ignore[arg-type]
            with lock_factory(lock_path):
                while True:
                    if lifecycle.stop_requested():
                        return terminal("STOPPED", ShadowTerminalReason.STOP_REQUESTED)
                    try:
                        lifecycle.remaining()
                    except ShadowRunDeadlineExceeded:
                        return terminal("DEADLINE", ShadowTerminalReason.RUN_DEADLINE)
                    except ShadowShutdownDeadlineExceeded:
                        return terminal("FAILED", ShadowTerminalReason.SHUTDOWN_DEADLINE)
                    cycle_started = monotonic()
                    last_closed = False
                    try:
                        result = run_shadow_once(
                            policy,
                            runtime_factory=runtime_factory,
                            clock=clock,
                            calendar=calendar,
                            stop_event=runtime_stop_event,  # type: ignore[arg-type]
                            deadline_remaining=lifecycle.remaining,
                        )
                    except ShadowCycleTerminated as error:
                        last_closed = error.resources_closed
                        if not last_closed:
                            return terminal(
                                "FAILED",
                                error.reason,
                                error.error_type,
                            )
                        if error.reason is ShadowTerminalReason.STOP_REQUESTED:
                            return terminal("STOPPED", error.reason)
                        if error.reason is ShadowTerminalReason.RUN_DEADLINE:
                            return terminal("DEADLINE", error.reason)
                        return terminal("FAILED", error.reason, error.error_type)
                    except BaseException as error:
                        last_closed = False
                        return terminal(
                            "FAILED",
                            ShadowTerminalReason.FAILURE,
                            type(error).__name__,
                        )
                    last_closed = result.resources_closed
                    if result.status == "CLOSED":
                        return terminal(
                            "CLOSED",
                            ShadowTerminalReason.CALENDAR_CLOSED,
                        )
                    cycles += 1
                    cycle_evidence = result.to_safe_dict()
                    cycle_evidence.update(
                        {
                            "event": "cycle",
                            "cycle_index": cycles,
                            "elapsed_seconds": round(
                                max(0.0, monotonic() - cycle_started), 6
                            ),
                            "interval_seconds": SHADOW_CONTINUOUS_INTERVAL_SECONDS,
                        }
                    )
                    emit(cycle_evidence)
                    try:
                        remaining = lifecycle.remaining()
                    except ShadowRunDeadlineExceeded:
                        return terminal("DEADLINE", ShadowTerminalReason.RUN_DEADLINE)
                    except ShadowShutdownDeadlineExceeded:
                        return terminal("FAILED", ShadowTerminalReason.SHUTDOWN_DEADLINE)
                    wait_seconds = min(SHADOW_CONTINUOUS_INTERVAL_SECONDS, remaining)
                    if signal_latch.wait(wait_seconds):
                        lifecycle.stop_requested()
                        return terminal("STOPPED", ShadowTerminalReason.STOP_REQUESTED)
                    try:
                        lifecycle.remaining()
                    except ShadowRunDeadlineExceeded:
                        return terminal("DEADLINE", ShadowTerminalReason.RUN_DEADLINE)
                    except ShadowShutdownDeadlineExceeded:
                        return terminal("FAILED", ShadowTerminalReason.SHUTDOWN_DEADLINE)
    except BaseException as error:
        return terminal(
            "FAILED",
            ShadowTerminalReason.FAILURE,
            type(error).__name__,
        )


def _zero_external_side_effects() -> dict[str, bool]:
    return {
        "account": False,
        "broker_orders": False,
        "oauth_revoke": False,
        "slack": False,
        "s3": False,
        "gemini": False,
        "reports": False,
    }
