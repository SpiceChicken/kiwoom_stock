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
    ExecutionPolicy,
    SHADOW_PROCESS_LOCK_PATH,
)
from kiwoom_stock.application.shadow_lifecycle import (
    SHUTDOWN_TIMEOUT_SECONDS,
    ShutdownDeadline,
    check_lifecycle,
    signal_stop_event,
)


_SEOUL = ZoneInfo("Asia/Seoul")


class ShadowWorkerError(RuntimeError):
    """Safe terminal shadow worker failure."""


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
    try:
        check_lifecycle(
            stop_event=stop_event,
            deadline_remaining=deadline_remaining,
        )
    except Exception as error:
        raise ShadowWorkerError("shadow lifecycle budget rejected runtime") from error
    receipt = runtime.execute_once()
    try:
        check_lifecycle(
            stop_event=stop_event,
            deadline_remaining=deadline_remaining,
        )
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
            result = run_shadow_once(
                policy,
                runtime_factory=runtime_factory,
                clock=clock,
                calendar=calendar,
                stop_event=event,
                deadline_remaining=lambda: deadline.remaining(monotonic=monotonic_clock),
            )
            deadline.remaining(monotonic=monotonic_clock)
            return result


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
