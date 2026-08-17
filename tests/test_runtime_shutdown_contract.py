import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiwoom_stock.application import runtime as runtime_module
from kiwoom_stock.application.runtime import TradingRuntime


def test_normal_runtime_starts_bounded_budget_before_engine_close_failure(monkeypatch):
    ledger = SimpleNamespace(set_shutdown_deadline=MagicMock())
    market_owner = SimpleNamespace(close=MagicMock())
    observed = []

    def close_monitor():
        remaining = ledger.set_shutdown_deadline.call_args.args[0]
        observed.append(remaining())
        raise RuntimeError("stuck database worker")

    monitor = SimpleNamespace(close=close_monitor)
    runtime = TradingRuntime(
        settings=MagicMock(),
        app_config={},
        output_dir_str="/tmp/output",
        monitor=monitor,
        _market_owner=market_owner,
        _ledger=ledger,
    )
    monotonic_values = iter((100.0, 131.0))
    monkeypatch.setattr(runtime_module.time, "monotonic", lambda: next(monotonic_values))

    with pytest.raises(RuntimeError, match="stuck database worker"):
        runtime.shutdown_engine()

    assert observed == [0.0]
    assert monitor._stop_event.is_set()
    ledger.set_shutdown_deadline.assert_called_once()
    market_owner.close.assert_not_called()
    runtime.close()
    market_owner.close.assert_called_once_with()


def test_normal_runtime_concurrent_shutdown_has_one_owner_and_shared_error(
    monkeypatch,
):
    monkeypatch.setattr(runtime_module, "NORMAL_SHUTDOWN_TIMEOUT_SECONDS", 0.5)
    ledger = SimpleNamespace(set_shutdown_deadline=MagicMock())
    close_entered = threading.Event()
    close_release = threading.Event()
    waiter_entered = threading.Event()
    start_barrier = threading.Barrier(3)
    owner_error = RuntimeError("owner close failed")
    close_calls = []
    errors = []
    wait_timeouts = []

    class ObservedCompletion:
        def __init__(self):
            self.event = threading.Event()

        def set(self):
            self.event.set()

        def wait(self, timeout=None):
            wait_timeouts.append(timeout)
            waiter_entered.set()
            return self.event.wait(timeout=timeout)

    def close_monitor():
        close_calls.append(None)
        close_entered.set()
        assert close_release.wait(timeout=1.0)
        raise owner_error

    monitor = SimpleNamespace(close=close_monitor)
    runtime = TradingRuntime(
        settings=MagicMock(),
        app_config={},
        output_dir_str="/tmp/output",
        monitor=monitor,
        _market_owner=SimpleNamespace(close=MagicMock()),
        _ledger=ledger,
    )
    runtime._shutdown_complete = ObservedCompletion()

    def shutdown_and_capture():
        start_barrier.wait()
        try:
            runtime.shutdown_engine()
        except BaseException as error:
            errors.append(error)

    callers = [threading.Thread(target=shutdown_and_capture) for _ in range(2)]
    for caller in callers:
        caller.start()
    start_barrier.wait()
    try:
        assert close_entered.wait(timeout=1.0)
        assert waiter_entered.wait(timeout=1.0)
    finally:
        close_release.set()
    for caller in callers:
        caller.join(timeout=1.0)

    assert all(not caller.is_alive() for caller in callers)
    assert len(close_calls) == 1
    assert len(wait_timeouts) == 1
    assert 0.0 <= wait_timeouts[0] <= 0.5
    ledger.set_shutdown_deadline.assert_called_once()
    remaining = ledger.set_shutdown_deadline.call_args.args[0]
    assert monitor._deadline_remaining is remaining
    assert len(errors) == 2
    assert all(error is owner_error for error in errors)
