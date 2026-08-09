"""Deterministic SQLite queue and connection lifecycle tests."""

import asyncio
from pathlib import Path
import queue
import sqlite3
import threading

import pytest

from kiwoom_stock.core.database import (
    PhysicalStatePersistenceError,
    TradeLogger,
    TradeLoggerLifecycleError,
    _PhysicalStateTask,
    _PhysicalStateTaskItem,
)


class _CloseFailureProxy:
    def __init__(self, connection, *, failures: int, message: str):
        self.connection = connection
        self.failures = failures
        self.message = message
        self.close_calls = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        self.close_calls += 1
        if self.close_calls <= self.failures:
            raise RuntimeError(self.message)
        self.connection.close()


class _BlockingExecuteProxy:
    def __init__(self, connection):
        self.connection = connection
        self.execute_entered = threading.Event()
        self.release_execute = threading.Event()

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, *args, **kwargs):
        self.execute_entered.set()
        assert self.release_execute.wait(timeout=5)
        return self.connection.execute(*args, **kwargs)


def _forces(*, velocity: float, net_force: float) -> dict[str, float]:
    return {
        "current_velocity": velocity,
        "thrust": 0.1,
        "gravity": -0.2,
        "drag": -0.3,
        "magnetic": 0.4,
        "jerk": 0.5,
        "impulse": 0.6,
        "net_force": net_force,
    }


def _install_one_shot_failure(
    monkeypatch,
    target,
    method_name: str,
    error: BaseException,
    *,
    after_call: bool = False,
):
    original = getattr(target, method_name)
    calls = []

    def wrapped(*args, **kwargs):
        calls.append(None)
        if len(calls) == 1:
            if after_call:
                original(*args, **kwargs)
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(target, method_name, wrapped)
    return calls, original


def _assert_terminal_resources(
    logger: TradeLogger,
    main_connection: sqlite3.Connection,
    worker_connection: sqlite3.Connection,
) -> None:
    assert logger._closed is True
    assert logger.is_closed is True
    assert logger._async_queue.unfinished_tasks == 0
    assert not logger._worker_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        main_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        worker_connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="rejects new physical-state tasks"):
        logger.submit_physical_state(
            "005930",
            _forces(velocity=99.0, net_force=99.0),
        )


def test_submit_captures_an_immutable_force_snapshot():
    logger = TradeLogger.__new__(TradeLogger)
    logger._async_queue = queue.Queue()
    logger._state_lock = threading.Lock()
    logger._accepting_submissions = True
    logger._worker_failure = None
    forces = {"current_velocity": 1.25, "nested": {"value": 7}}

    logger.submit_physical_state("005930", forces)
    forces["current_velocity"] = 99.0
    forces["nested"]["value"] = 99

    task = logger._async_queue.get_nowait()
    try:
        assert dict(task.items[0].forces) == {
            "current_velocity": 1.25,
            "nested": {"value": 7},
        }
    finally:
        logger._async_queue.task_done()


def test_configured_path_is_shared_by_main_and_worker_without_cwd_fallback(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    configured_path = tmp_path / "configured" / "paper.sqlite3"
    configured_path.parent.mkdir()
    logger = TradeLogger(configured_path)

    try:
        assert Path(logger.db_path) == configured_path
        buy_id = logger.record_buy(
            {
                "stock_code": "005930",
                "stock_name": "Samsung",
                "buy_price": 50_000.0,
                "buy_time": "2026-07-18 10:00:00",
                "buy_regime": "STABLE_BULL",
                **{
                    key: value
                    for key, value in _forces(
                        velocity=0.0,
                        net_force=0.0,
                    ).items()
                    if key != "current_velocity"
                },
            }
        )
        logger.submit_physical_state("005930", _forces(velocity=1.25, net_force=1.1))
        logger.flush()
        logger.flush()

        assert buy_id > 0
        assert logger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        row = logger.conn.execute(
            "SELECT stock_code, velocity, net_force FROM physics_state"
        ).fetchone()
        assert tuple(row) == ("005930", 1.25, 1.1)
        assert logger._worker_thread.is_alive()
        assert logger._async_queue.unfinished_tasks == 0
        assert not (tmp_path / "trades.db").exists()
    finally:
        logger.close()


def test_async_compatibility_shim_delegates_to_same_queue(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    logger = TradeLogger(tmp_path / "paper.sqlite3")

    try:
        asyncio.run(
            logger.async_log_physical_state(
                "035420",
                _forces(velocity=2.5, net_force=1.4),
            )
        )
        logger.flush()

        row = logger.conn.execute(
            "SELECT stock_code, velocity, net_force FROM physics_state"
        ).fetchone()
        assert tuple(row) == ("035420", 2.5, 1.4)
        assert not (tmp_path / "trades.db").exists()
    finally:
        logger.close()


def test_close_drains_latest_state_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    configured_path = tmp_path / "paper.sqlite3"
    logger = TradeLogger(configured_path)
    worker_connection = logger._worker_conn
    main_connection = logger.conn

    for value in range(20):
        logger.submit_physical_state(
            "005930",
            _forces(velocity=float(value), net_force=float(value) + 0.5),
        )

    logger.close()
    logger.close()

    assert logger._async_queue.unfinished_tasks == 0
    assert not logger._worker_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        main_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        worker_connection.execute("SELECT 1")
    with pytest.raises(RuntimeError, match="rejects new physical-state tasks"):
        logger.submit_physical_state("005930", _forces(velocity=99.0, net_force=99.0))

    connection = sqlite3.connect(configured_path)
    try:
        row = connection.execute(
            "SELECT stock_code, velocity, net_force FROM physics_state"
        ).fetchone()
    finally:
        connection.close()

    assert row == ("005930", 19.0, 19.5)
    assert not (tmp_path / "trades.db").exists()


def test_worker_failure_surfaces_after_drain_and_close_still_releases_resources(
    monkeypatch,
    tmp_path,
):
    monkeypatch.chdir(tmp_path)
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    worker_connection = logger._worker_conn
    main_connection = logger.conn

    logger.submit_physical_state("BROKEN", {"current_velocity": 1.0})

    with pytest.raises(PhysicalStatePersistenceError) as flush_error:
        logger.flush()
    assert "KeyError" in str(flush_error.value)

    with pytest.raises(PhysicalStatePersistenceError) as close_error:
        logger.close()
    with pytest.raises(PhysicalStatePersistenceError) as repeated_close_error:
        logger.close()

    assert str(close_error.value) == str(flush_error.value)
    assert str(repeated_close_error.value) == str(flush_error.value)
    assert logger._async_queue.unfinished_tasks == 0
    assert not logger._worker_thread.is_alive()
    with pytest.raises(sqlite3.ProgrammingError):
        main_connection.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        worker_connection.execute("SELECT 1")
    assert not (tmp_path / "trades.db").exists()


def test_one_shot_sentinel_failure_is_latched_after_same_call_recovers(
    monkeypatch,
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    logger.submit_physical_state(
        "005930",
        _forces(velocity=1.25, net_force=1.5),
    )
    calls, _ = _install_one_shot_failure(
        monkeypatch,
        logger._async_queue,
        "put",
        RuntimeError("sentinel unavailable once"),
    )

    with pytest.raises(TradeLoggerLifecycleError) as close_error:
        logger.close()
    with pytest.raises(TradeLoggerLifecycleError) as repeated_error:
        logger.close()

    assert len(calls) == 2
    assert "sentinel enqueue" in str(close_error.value)
    assert str(repeated_error.value) == str(close_error.value)
    _assert_terminal_resources(logger, main_connection, worker_connection)


def test_persistent_sentinel_failure_keeps_close_retryable_until_restored(
    monkeypatch,
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    logger.submit_physical_state(
        "005930",
        _forces(velocity=2.5, net_force=2.75),
    )
    original_put = logger._async_queue.put
    calls = []

    def fail_bounded_passes(*args, **kwargs):
        calls.append(None)
        raise RuntimeError("sentinel remains unavailable")

    monkeypatch.setattr(logger._async_queue, "put", fail_bounded_passes)

    with pytest.raises(TradeLoggerLifecycleError) as incomplete_error:
        logger.close()

    assert len(calls) == 2
    assert logger._closed is False
    assert logger._worker_thread.is_alive()
    assert logger._main_connection_closed is True
    assert logger._worker_connection_closed is False
    with pytest.raises(RuntimeError, match="rejects new physical-state tasks"):
        logger.submit_physical_state(
            "000660",
            _forces(velocity=3.0, net_force=3.25),
        )

    logger._async_queue.join()
    monkeypatch.setattr(logger._async_queue, "put", original_put)
    with pytest.raises(TradeLoggerLifecycleError) as recovered_error:
        logger.close()

    assert str(recovered_error.value) == str(incomplete_error.value)
    _assert_terminal_resources(logger, main_connection, worker_connection)

    connection = sqlite3.connect(tmp_path / "paper.sqlite3")
    try:
        row = connection.execute(
            "SELECT stock_code, velocity, net_force FROM physics_state"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("005930", 2.5, 2.75)


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (KeyboardInterrupt, "queue drain interrupted"),
        (SystemExit, "queue drain terminated"),
    ],
)
def test_process_control_during_queue_drain_is_reobserved_after_cleanup(
    monkeypatch,
    tmp_path,
    error_type,
    message,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    calls, _ = _install_one_shot_failure(
        monkeypatch,
        logger._async_queue,
        "join",
        error_type(message),
    )

    with pytest.raises(error_type) as close_error:
        logger.close()
    with pytest.raises(error_type) as repeated_error:
        logger.close()

    assert len(calls) == 1
    assert close_error.value.args == (message,)
    assert repeated_error.value.args == (message,)
    _assert_terminal_resources(logger, main_connection, worker_connection)


def test_thread_join_failure_after_join_is_latched_and_cleanup_continues(
    monkeypatch,
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    calls, _ = _install_one_shot_failure(
        monkeypatch,
        logger._worker_thread,
        "join",
        RuntimeError("thread join observation failed"),
        after_call=True,
    )

    with pytest.raises(TradeLoggerLifecycleError) as close_error:
        logger.close()
    with pytest.raises(TradeLoggerLifecycleError) as repeated_error:
        logger.close()

    assert len(calls) == 1
    assert "worker thread join" in str(close_error.value)
    assert str(repeated_error.value) == str(close_error.value)
    _assert_terminal_resources(logger, main_connection, worker_connection)


def test_main_connection_one_shot_close_failure_recovers_in_same_call(
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    main_proxy = _CloseFailureProxy(
        main_connection,
        failures=1,
        message="main close failed once",
    )
    logger.conn = main_proxy

    with pytest.raises(TradeLoggerLifecycleError) as close_error:
        logger.close()
    with pytest.raises(TradeLoggerLifecycleError) as repeated_error:
        logger.close()

    assert main_proxy.close_calls == 2
    assert "main connection close" in str(close_error.value)
    assert str(repeated_error.value) == str(close_error.value)
    _assert_terminal_resources(logger, main_connection, worker_connection)


def test_worker_connection_one_shot_close_failure_recovers_in_same_call(
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    worker_proxy = _CloseFailureProxy(
        worker_connection,
        failures=1,
        message="worker close failed once",
    )
    logger._worker_conn = worker_proxy

    with pytest.raises(TradeLoggerLifecycleError) as close_error:
        logger.close()
    with pytest.raises(TradeLoggerLifecycleError) as repeated_error:
        logger.close()

    assert worker_proxy.close_calls == 2
    assert "worker connection close" in str(close_error.value)
    assert str(repeated_error.value) == str(close_error.value)
    _assert_terminal_resources(logger, main_connection, worker_connection)


def test_concurrent_waiter_reobserves_completed_close_failure(
    monkeypatch,
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    join_entered = threading.Event()
    release_join = threading.Event()
    waiter_entered = threading.Event()
    errors = []

    def blocking_failed_join():
        join_entered.set()
        assert release_join.wait(timeout=5)
        raise RuntimeError("concurrent queue drain failed")

    monkeypatch.setattr(logger._async_queue, "join", blocking_failed_join)

    def close_and_capture():
        try:
            logger.close()
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=close_and_capture)
    owner.start()
    assert join_entered.wait(timeout=5)

    close_event = logger._close_complete
    original_wait = close_event.wait

    def observed_wait(*args, **kwargs):
        waiter_entered.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(close_event, "wait", observed_wait)
    waiter = threading.Thread(target=close_and_capture)
    waiter.start()
    assert waiter_entered.wait(timeout=5)
    release_join.set()
    owner.join(timeout=5)
    waiter.join(timeout=5)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(errors) == 2
    assert all(isinstance(error, TradeLoggerLifecycleError) for error in errors)
    assert str(errors[0]) == str(errors[1])
    _assert_terminal_resources(logger, main_connection, worker_connection)


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (KeyboardInterrupt, "sentinel publication interrupted"),
        (SystemExit, "sentinel publication terminated"),
    ],
)
def test_after_call_sentinel_process_control_recovers_duplicate_for_all_callers(
    monkeypatch,
    tmp_path,
    error_type,
    message,
):
    configured_path = tmp_path / "paper.sqlite3"
    logger = TradeLogger(configured_path)
    main_connection = logger.conn
    worker_connection = logger._worker_conn
    blocking_worker_connection = _BlockingExecuteProxy(worker_connection)
    logger._worker_conn = blocking_worker_connection
    logger.submit_physical_state(
        "005930",
        _forces(velocity=4.5, net_force=4.75),
    )
    assert blocking_worker_connection.execute_entered.wait(timeout=5)

    original_put = logger._async_queue.put
    second_put_complete = threading.Event()
    put_calls = []

    def after_call_failure_put(*args, **kwargs):
        result = original_put(*args, **kwargs)
        put_calls.append(None)
        if len(put_calls) == 1:
            raise error_type(message)
        second_put_complete.set()
        return result

    monkeypatch.setattr(logger._async_queue, "put", after_call_failure_put)
    errors = []

    def close_and_capture():
        try:
            logger.close()
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=close_and_capture)
    owner.start()
    assert second_put_complete.wait(timeout=5)

    close_event = logger._close_complete
    original_wait = close_event.wait
    waiter_entered = threading.Event()

    def observed_wait(*args, **kwargs):
        waiter_entered.set()
        return original_wait(*args, **kwargs)

    monkeypatch.setattr(close_event, "wait", observed_wait)
    waiter = threading.Thread(target=close_and_capture)
    waiter.start()
    assert waiter_entered.wait(timeout=5)

    blocking_worker_connection.release_execute.set()
    owner.join(timeout=5)
    waiter.join(timeout=5)

    assert not owner.is_alive()
    assert not waiter.is_alive()
    assert len(put_calls) == 2
    assert len(errors) == 2
    assert all(type(error) is error_type for error in errors)
    assert all(error.args == (message,) for error in errors)
    with pytest.raises(error_type) as repeated_error:
        logger.close()
    assert repeated_error.value.args == (message,)
    _assert_terminal_resources(logger, main_connection, worker_connection)

    connection = sqlite3.connect(configured_path)
    try:
        row = connection.execute(
            "SELECT stock_code, velocity, net_force FROM physics_state"
        ).fetchone()
    finally:
        connection.close()
    assert row == ("005930", 4.5, 4.75)


def test_dead_worker_never_discards_a_physical_task_mixed_after_sentinel(
    monkeypatch,
    tmp_path,
):
    logger = TradeLogger(tmp_path / "paper.sqlite3")
    worker_connection = logger._worker_conn
    blocking_worker_connection = _BlockingExecuteProxy(worker_connection)
    logger._worker_conn = blocking_worker_connection
    logger.submit_physical_state(
        "005930",
        _forces(velocity=5.5, net_force=5.75),
    )
    assert blocking_worker_connection.execute_entered.wait(timeout=5)

    original_put = logger._async_queue.put
    sentinel_enqueued = threading.Event()

    def observed_put(*args, **kwargs):
        result = original_put(*args, **kwargs)
        sentinel_enqueued.set()
        return result

    monkeypatch.setattr(logger._async_queue, "put", observed_put)
    errors = []

    def close_and_capture():
        try:
            logger.close()
        except BaseException as error:
            errors.append(error)

    owner = threading.Thread(target=close_and_capture)
    owner.start()
    assert sentinel_enqueued.wait(timeout=5)

    orphan_physical_task = _PhysicalStateTask(
        items=(
            _PhysicalStateTaskItem(
                stock_code="ORPHAN",
                forces=tuple(_forces(velocity=6.5, net_force=6.75).items()),
                timestamp_str="2026-07-18 12:00:00.000000",
            ),
        ),
    )
    logger._async_queue.put(orphan_physical_task)
    blocking_worker_connection.release_execute.set()
    owner.join(timeout=5)

    assert not owner.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TradeLoggerLifecycleError)
    assert "queue drain" in str(errors[0])
    assert logger.is_closed is False
    assert not logger._worker_thread.is_alive()
    assert logger._async_queue.unfinished_tasks == 1
    with logger._async_queue.mutex:
        remaining_items = tuple(logger._async_queue.queue)
    assert remaining_items == (orphan_physical_task,)

    assert logger._async_queue.get_nowait() is orphan_physical_task
    logger._async_queue.task_done()
    with pytest.raises(TradeLoggerLifecycleError):
        logger.close()
    assert logger.is_closed is True
