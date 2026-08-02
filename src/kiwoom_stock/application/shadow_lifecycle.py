"""Deterministic lifecycle primitives for the bounded shadow worker."""

from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
import signal
import threading
import time
from types import FrameType
from typing import Any, Callable, Dict, Iterator, TypeVar, cast


class ShadowLifecycleError(RuntimeError):
    """The bounded worker cannot continue or shut down safely."""


class ShadowStopRequested(ShadowLifecycleError):
    """A SIGTERM/SIGINT stop request was observed before new work."""


class ShadowDeadlineExceeded(ShadowLifecycleError):
    """The monotonic shutdown deadline has elapsed."""


MonotonicClock = Callable[[], float]
LifecycleResult = TypeVar("LifecycleResult")
SHUTDOWN_TIMEOUT_SECONDS = 30.0


def check_lifecycle(
    *,
    stop_event: threading.Event | None = None,
    deadline_remaining: Callable[[], float] | None = None,
) -> None:
    """Cooperatively stop at every application/infrastructure boundary."""

    if stop_event is not None and stop_event.is_set():
        raise ShadowStopRequested("shadow stop requested")
    if deadline_remaining is not None:
        deadline_remaining()


@dataclass(frozen=True)
class ShutdownDeadline:
    """A monotonic, immutable shutdown budget."""

    expires_at: float
    timeout_seconds: float = 30.0

    @classmethod
    def start(
        cls,
        *,
        clock: MonotonicClock = time.monotonic,
        monotonic: MonotonicClock | None = None,
        timeout_seconds: float = 30.0,
        timeout: float | None = None,
    ) -> "ShutdownDeadline":
        if monotonic is not None:
            clock = monotonic
        if timeout is not None:
            timeout_seconds = timeout
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("shutdown timeout must be positive")
        return cls(
            expires_at=clock() + float(timeout_seconds),
            timeout_seconds=float(timeout_seconds),
        )

    def remaining(
        self,
        *,
        clock: MonotonicClock = time.monotonic,
        monotonic: MonotonicClock | None = None,
    ) -> float:
        """Return remaining seconds, or fail closed after expiry."""

        if monotonic is not None:
            clock = monotonic
        remaining = self.expires_at - clock()
        if remaining <= 0:
            raise ShadowDeadlineExceeded("shadow shutdown deadline exceeded")
        return remaining


@dataclass
class ShadowStopController:
    """Translate process signals into an async-safe Event state change."""

    stop_event: threading.Event = field(default_factory=threading.Event)
    _previous_handlers: Dict[int, object] = field(default_factory=dict, init=False)
    _installed: bool = field(default=False, init=False)

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        del signum
        self.stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self) -> None:
        self.stop_event.set()

    def install(self) -> None:
        if self._installed:
            return
        try:
            for signum in (signal.SIGTERM, signal.SIGINT):
                self._previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle_signal)
        except BaseException:
            for installed_signum, installed_previous in self._previous_handlers.items():
                signal.signal(installed_signum, cast(Any, installed_previous))
            self._previous_handlers.clear()
            raise
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        for signum, previous in self._previous_handlers.items():
            signal.signal(signum, cast(Any, previous))
        self._previous_handlers.clear()
        self._installed = False

    def wait(self, timeout: float | None = None) -> bool:
        return self.stop_event.wait(timeout)

    def ensure_running(self) -> None:
        if self.stop_requested:
            raise ShadowStopRequested("shadow stop requested")

    def __enter__(self) -> "ShadowStopController":
        self.install()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.restore()


@contextmanager
def signal_stop_event(event: threading.Event) -> Iterator[threading.Event]:
    """Install the SIGTERM/SIGINT adapter for an existing stop Event."""

    controller = ShadowStopController(stop_event=event)
    with controller:
        yield event


def execute_with_lifecycle(
    operation: Callable[[ShadowStopController, ShutdownDeadline], LifecycleResult],
    *,
    clock: MonotonicClock = time.monotonic,
    shutdown_timeout_seconds: float = 30.0,
) -> LifecycleResult:
    """Run one bounded operation with signal and deadline guards."""

    deadline = ShutdownDeadline.start(
        clock=clock,
        timeout_seconds=shutdown_timeout_seconds,
    )
    with ShadowStopController() as controller:
        controller.ensure_running()
        result = operation(controller, deadline)
        controller.ensure_running()
        deadline.remaining(clock=clock)
        return result
