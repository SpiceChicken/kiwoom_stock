from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from kiwoom_stock.application.shadow_lifecycle import (
    ShutdownDeadline,
    ShadowDeadlineExceeded,
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
