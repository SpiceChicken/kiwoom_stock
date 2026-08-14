from __future__ import annotations

import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kiwoom_stock.application.shadow_lifecycle import (
    ContinuousLifecycle,
    SignalLatch,
    RunDeadline,
    SHADOW_SESSION_CLOSE_TIME,
    SHADOW_SESSION_OPEN_TIME,
    SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS,
    ShutdownDeadline,
    ShadowDeadlineExceeded,
    ShadowShutdownDeadlineExceeded,
    ShadowStopController,
    ShadowStopRequested,
    shadow_session_remaining,
    shadow_session_wait_until_open,
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


def test_continuous_run_deadline_has_a_session_sized_process_cap():
    deadline = RunDeadline.start(clock=lambda: 10.0)
    assert deadline.expires_at == 10.0 + SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS
    assert deadline.elapsed(clock=lambda: 25.0) == 15.0
    with pytest.raises(ShadowDeadlineExceeded):
        deadline.remaining(clock=lambda: 10.0 + SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS)


def test_shadow_session_window_is_absolute_kst_and_has_no_dst_ambiguity():
    before_open = datetime(2026, 8, 14, 23, 50, tzinfo=timezone.utc)
    at_open = datetime(2026, 8, 15, 0, 0, tzinfo=timezone.utc)
    at_close = datetime(2026, 8, 15, 6, 30, tzinfo=timezone.utc)

    assert SHADOW_SESSION_OPEN_TIME.isoformat() == "09:00:00"
    assert SHADOW_SESSION_CLOSE_TIME.isoformat() == "15:30:00"
    assert shadow_session_wait_until_open(before_open) == 600.0
    assert shadow_session_wait_until_open(at_open) is None
    assert shadow_session_remaining(at_open) == 23_400.0
    assert shadow_session_remaining(at_close) == 0.0


def test_signal_starts_distinct_thirty_second_shutdown_budget_and_takes_minimum():
    now = [100.0]
    event = threading.Event()
    latch = SignalLatch(event, lambda: now[0])
    lifecycle = ContinuousLifecycle(
        stop_event=latch,
        run_deadline=RunDeadline.start(clock=lambda: 0.0),
        clock=lambda: now[0],
    )
    assert lifecycle.remaining() == SHADOW_CONTINUOUS_MAX_RUNTIME_SECONDS - 100.0

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
