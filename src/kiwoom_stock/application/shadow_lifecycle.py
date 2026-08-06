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


class ShadowRunDeadlineExceeded(ShadowDeadlineExceeded):
    """The fixed process-level exposure cap elapsed."""


class ShadowShutdownDeadlineExceeded(ShadowDeadlineExceeded):
    """The signal-triggered graceful shutdown budget elapsed."""


MonotonicClock = Callable[[], float]
LifecycleResult = TypeVar("LifecycleResult")
SHUTDOWN_TIMEOUT_SECONDS = 30.0
SHADOW_CONTINUOUS_INTERVAL_SECONDS = 60.0
SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS = 15.0 * 60.0


class SignalLatch:
    """Event-compatible owner that records signal time without handler locking."""

    def __init__(self, event: threading.Event, clock: MonotonicClock) -> None:
        self._event = event
        self._clock = clock
        self._signal_times: list[float] = []
        self._lock = threading.Lock()
        self._shutdown_deadline: ShutdownDeadline | None = None

    def signal(self) -> None:
        """Record a signal using only reentrant-safe primitives, then set Event."""

        self._signal_times.append(self._clock())
        self._event.set()

    def _materialize_deadline(self) -> None:
        if not self._event.is_set():
            return
        if not self._signal_times:
            self._signal_times.append(self._clock())
        first_signal_at = min(self._signal_times)
        candidate = ShutdownDeadline(
            expires_at=first_signal_at + SHUTDOWN_TIMEOUT_SECONDS,
            timeout_seconds=SHUTDOWN_TIMEOUT_SECONDS,
        )
        with self._lock:
            if (
                self._shutdown_deadline is None
                or candidate.expires_at < self._shutdown_deadline.expires_at
            ):
                self._shutdown_deadline = candidate

    def set(self) -> None:
        self.signal()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        observed = self._event.wait(timeout)
        if observed:
            self._materialize_deadline()
        return observed

    @property
    def shutdown_deadline(self) -> ShutdownDeadline | None:
        self._materialize_deadline()
        return self._shutdown_deadline


def check_lifecycle(
    *,
    stop_event: threading.Event | None = None,
    deadline_remaining: Callable[[], float] | None = None,
) -> None:
    """Cooperatively stop at every application/infrastructure boundary."""

    if deadline_remaining is not None:
        deadline_remaining()
    if stop_event is not None and stop_event.is_set():
        raise ShadowStopRequested("shadow stop requested")


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


@dataclass(frozen=True)
class RunDeadline:
    """Process-level monotonic exposure cap for continuous shadow execution."""

    started_at: float
    expires_at: float

    @classmethod
    def start(
        cls,
        *,
        clock: MonotonicClock = time.monotonic,
        timeout_seconds: float = SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS,
    ) -> "RunDeadline":
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ValueError("run timeout must be positive")
        started_at = clock()
        return cls(started_at=started_at, expires_at=started_at + float(timeout_seconds))

    def remaining(self, *, clock: MonotonicClock = time.monotonic) -> float:
        remaining = self.expires_at - clock()
        if remaining <= 0:
            raise ShadowRunDeadlineExceeded("shadow continuous run deadline exceeded")
        return remaining

    def elapsed(self, *, clock: MonotonicClock = time.monotonic) -> float:
        return max(0.0, clock() - self.started_at)


@dataclass
class ContinuousLifecycle:
    """Own the run cap and a lazily-started signal shutdown budget."""

    stop_event: SignalLatch
    run_deadline: RunDeadline
    clock: MonotonicClock = time.monotonic

    def stop_requested(self) -> bool:
        requested = self.stop_event.is_set()
        return requested

    def remaining(self) -> float:
        run_remaining = self.run_deadline.remaining(clock=self.clock)
        if not self.stop_requested():
            return run_remaining
        shutdown_deadline = self.stop_event.shutdown_deadline
        if shutdown_deadline is None:
            raise ShadowShutdownDeadlineExceeded(
                "shadow signal timestamp was not latched"
            )
        try:
            shutdown_remaining = shutdown_deadline.remaining(clock=self.clock)
        except ShadowDeadlineExceeded:
            raise ShadowShutdownDeadlineExceeded(
                "shadow signal shutdown deadline exceeded"
            ) from None
        return min(run_remaining, shutdown_remaining)


@dataclass
class ShadowStopController:
    """Translate process signals into an async-safe Event state change."""

    stop_event: threading.Event = field(default_factory=threading.Event)
    _previous_handlers: Dict[int, object] = field(default_factory=dict, init=False)
    _installed: bool = field(default=False, init=False)

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        del signum
        signal_setter = getattr(self.stop_event, "signal", None)
        if callable(signal_setter):
            signal_setter()
        else:
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
