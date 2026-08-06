from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from kiwoom_stock.application.shadow_lifecycle import (
    ContinuousLifecycle,
    SignalLatch,
    RunDeadline,
    SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS,
    ShutdownDeadline,
    ShadowDeadlineExceeded,
    ShadowShutdownDeadlineExceeded,
    ShadowStopController,
    ShadowStopRequested,
    execute_with_lifecycle,
)
from kiwoom_stock.infrastructure.shadow_process_lock import (
    ShadowProcessAlreadyRunning,
    ShadowProcessLock,
)


def test_deadline_uses_monotonic_clock_and_fails_closed():
    now = iter((10.0, 39.0, 40.0))
    deadline = ShutdownDeadline.start(clock=lambda: next(now), timeout_seconds=30)
    assert deadline.remaining(clock=lambda: 10.0) == 30.0
    assert deadline.remaining(clock=lambda: 39.0) == 1.0
    with pytest.raises(ShadowDeadlineExceeded):
        deadline.remaining(clock=lambda: 40.0)


def test_continuous_run_deadline_is_fixed_to_fifteen_monotonic_minutes():
    deadline = RunDeadline.start(clock=lambda: 10.0)
    assert deadline.expires_at == 10.0 + SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS
    assert deadline.elapsed(clock=lambda: 25.0) == 15.0
    with pytest.raises(ShadowDeadlineExceeded):
        deadline.remaining(clock=lambda: 910.0)


def test_signal_starts_distinct_thirty_second_shutdown_budget_and_takes_minimum():
    now = [100.0]
    event = threading.Event()
    latch = SignalLatch(event, lambda: now[0])
    lifecycle = ContinuousLifecycle(
        stop_event=latch,
        run_deadline=RunDeadline.start(clock=lambda: 0.0),
        clock=lambda: now[0],
    )
    assert lifecycle.remaining() == 800.0

    latch.set()
    assert lifecycle.remaining() == 30.0
    now[0] = 129.5
    assert lifecycle.remaining() == 0.5
    now[0] = 130.0
    with pytest.raises(ShadowShutdownDeadlineExceeded):
        lifecycle.remaining()


def test_signal_latch_uses_set_timestamp_even_when_consumer_observes_late():
    now = [5.0]
    event = threading.Event()
    latch = SignalLatch(event, lambda: now[0])
    lifecycle = ContinuousLifecycle(
        stop_event=latch,
        run_deadline=RunDeadline.start(clock=lambda: 0.0),
        clock=lambda: now[0],
    )
    latch.set()
    now[0] = 34.0
    assert lifecycle.remaining() == 1.0


def test_signal_latch_concurrent_set_preserves_earliest_expiry():
    calls = []
    event = threading.Event()

    def clock():
        value = len(calls) + 1
        calls.append(value)
        return float(value)

    latch = SignalLatch(event, clock)
    threads = [threading.Thread(target=latch.set) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(calls) == list(range(1, 9))
    assert latch.shutdown_deadline is not None
    assert latch.shutdown_deadline.expires_at == 31.0


def test_signal_handler_path_is_lock_free_and_reentrant_signal_cannot_extend_expiry():
    event = threading.Event()
    calls = 0
    latch = None

    def clock():
        nonlocal calls
        calls += 1
        if calls == 1:
            assert latch is not None
            latch.signal()
            return 10.0
        return 11.0 if calls == 2 else 20.0

    latch = SignalLatch(event, clock)

    latch._lock.acquire()
    try:
        latch.signal()
    finally:
        latch._lock.release()

    assert latch.shutdown_deadline is not None
    assert latch.shutdown_deadline.expires_at == 40.0
    latch.signal()
    assert latch.shutdown_deadline.expires_at == 40.0


def test_stop_controller_sets_event_and_restores_previous_handlers():
    controller = ShadowStopController()
    previous = signal.getsignal(signal.SIGTERM)
    with controller:
        assert signal.getsignal(signal.SIGTERM) == controller._handle_signal
        controller._handle_signal(signal.SIGTERM, None)
        assert controller.stop_requested
        with pytest.raises(ShadowStopRequested):
            controller.ensure_running()
    assert signal.getsignal(signal.SIGTERM) == previous


def test_stop_controller_wait_is_deterministic():
    controller = ShadowStopController()
    thread = threading.Thread(target=lambda: (threading.Event().wait(0.01), controller.request_stop()))
    thread.start()
    assert controller.wait(timeout=1)
    thread.join()


def test_execute_with_lifecycle_rejects_stop_and_preserves_result():
    result = execute_with_lifecycle(lambda _controller, deadline: deadline.timeout_seconds)
    assert result == 30.0

    def stopped(controller, _deadline):
        controller.request_stop()
        return None

    with pytest.raises(ShadowStopRequested):
        execute_with_lifecycle(stopped)


def test_process_lock_is_nonblocking_and_releases(tmp_path):
    path = tmp_path / "shadow.lock"
    first = ShadowProcessLock(path)
    second = ShadowProcessLock(path)
    with first:
        assert first.held
        with pytest.raises(ShadowProcessAlreadyRunning):
            second.acquire()
    second.acquire()
    second.release()


def test_process_lock_rejects_relative_path(tmp_path):
    with pytest.raises(ValueError):
        ShadowProcessLock(Path("shadow.lock"))
