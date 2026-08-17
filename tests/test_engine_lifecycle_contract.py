import threading

import pytest

from test_engine import _RetryableLedger, _lifecycle_engine
from kiwoom_stock.monitoring.engine import TradingEngineLifecycleError


def test_engine_close_orders_resources_and_is_idempotent():
    engine, events = _lifecycle_engine()

    engine.close()
    engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._closed is True
    assert engine._closing is False
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine.run()
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._evaluate_stocks([])
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._prepare_cycle([])


def test_engine_close_attempts_every_step_and_surfaces_ordinary_lifecycle_failure():
    engine, events = _lifecycle_engine(
        executor_error=RuntimeError("executor failed"),
        physical_error=ValueError("physical close failed"),
        ledger_error=RuntimeError("ledger close failed"),
    )

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert "evaluation executor (RuntimeError)" in str(first_error.value)
    assert "physical-state repository (ValueError)" in str(first_error.value)
    assert "paper ledger (RuntimeError)" in str(first_error.value)
    assert str(repeated_error.value) == str(first_error.value)
    assert engine._closed is True
    assert engine._closing is False


def test_engine_retries_only_an_incomplete_ledger_and_keeps_first_failure():
    engine, events = _lifecycle_engine()
    ledger = _RetryableLedger(events, error=RuntimeError("ledger remains open"))
    engine.db = ledger

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()

    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._work_closed is True
    assert engine._closed is False
    assert engine._executor_close_complete is True
    assert engine._physical_close_complete is True
    assert engine._ledger_close_complete is False
    with pytest.raises(RuntimeError, match="cannot start new work"):
        engine._prepare_cycle([])

    ledger.error = None
    with pytest.raises(TradingEngineLifecycleError) as recovered_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert str(recovered_error.value) == str(first_error.value)
    assert str(repeated_error.value) == str(first_error.value)
    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
        "ledger.close",
    ]
    assert ledger.is_closed is True
    assert engine._ledger_close_complete is True
    assert engine._closed is True


def test_engine_retry_concurrent_waiter_observes_first_failure_without_repeating_steps(
    monkeypatch,
):
    engine, events = _lifecycle_engine()
    ledger = _RetryableLedger(events, error=RuntimeError("ledger remains open"))
    engine.db = ledger

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()

    ledger.error = None
    ledger.block_close = True
    ledger.close_entered.clear()
    errors = []

    def close_and_capture():
        try:
            engine.close()
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=close_and_capture)
    owner.start()
    assert ledger.close_entered.wait(timeout=5)

    close_event = engine._close_complete
    original_wait = close_event.wait
    waiter_entered = threading.Event()

    def observed_wait(*args, **kwargs):
        waiter_entered.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(close_event, "wait", observed_wait)
    waiter = threading.Thread(target=close_and_capture)
    waiter.start()
    assert waiter_entered.wait(timeout=5)
    ledger.release_close.set()
    owner.join(timeout=5)
    waiter.join(timeout=5)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, TradingEngineLifecycleError) for error in errors)
    assert all(str(error) == str(first_error.value) for error in errors)
    assert events.count("ledger.close") == 2
    assert events.count("physical.close") == 1
    assert sum(event[0] == "executor.shutdown" for event in events if isinstance(event, tuple)) == 1
    assert engine._closed is True


def test_engine_does_not_retry_a_ledger_reporting_terminal_after_close_error():
    engine, events = _lifecycle_engine(
        ledger_error=RuntimeError("historical ledger close error"),
    )

    with pytest.raises(TradingEngineLifecycleError) as first_error:
        engine.close()
    with pytest.raises(TradingEngineLifecycleError) as repeated_error:
        engine.close()

    assert str(repeated_error.value) == str(first_error.value)
    assert events.count("ledger.close") == 1
    assert engine.db.is_closed is True
    assert engine._ledger_close_complete is True
    assert engine._closed is True


@pytest.mark.parametrize(
    ("executor_error", "physical_error", "process_control"),
    [
        (
            KeyboardInterrupt("executor interrupted"),
            RuntimeError("physical close failed"),
            "executor",
        ),
        (
            RuntimeError("executor failed"),
            SystemExit(7),
            "physical",
        ),
    ],
)
def test_engine_close_attempts_all_cleanup_then_reraises_process_control(
    executor_error,
    physical_error,
    process_control,
):
    expected_error = (
        executor_error if process_control == "executor" else physical_error
    )
    engine, events = _lifecycle_engine(
        executor_error=executor_error,
        physical_error=physical_error,
        ledger_error=RuntimeError("ledger close failed"),
    )

    with pytest.raises(type(expected_error)) as first_error:
        engine.close()
    with pytest.raises(type(expected_error)) as repeated_error:
        engine.close()

    assert first_error.value is expected_error
    assert repeated_error.value is expected_error
    assert events == [
        (
            "executor.shutdown",
            {"wait": True, "cancel_futures": False},
        ),
        "physical.close",
        "ledger.close",
    ]
    assert engine._lifecycle_failure is not None
    assert any("ledger close failed" in failure for failure in engine._lifecycle_failure)
    assert engine._closed is True
    assert engine._closing is False
