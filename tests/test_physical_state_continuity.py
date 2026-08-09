from datetime import datetime, timedelta
import math
import threading
import time
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.core import database as database_module
from kiwoom_stock.core.database import (
    PhysicalStateCommitUnknownError,
    PhysicalStatePersistenceError,
    TradeLogger,
    TradeLoggerLifecycleError,
)
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.models import PhysicalObservation
from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
    PhysicalStateValidationError,
    PhysicalTrackerState,
)
from kiwoom_stock.infrastructure.physical_state_repository import AsyncPhysicalStateRepository
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer


KST = ZoneInfo("Asia/Seoul")


class MutableClock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def observation(
    observed_at: datetime,
    *,
    volume: float,
    price: float,
    strength: float = 120.0,
    baseline: float = 100.0,
    stock_code: str = "005930",
) -> PhysicalObservation:
    return PhysicalObservation(
        stock_code=stock_code,
        observed_at=observed_at,
        current_price=price,
        cumulative_volume=volume,
        strength=strength,
        prev_strength_5m=baseline,
        vwap=70_000.0,
        atr_percent=1.0,
        vol_ratio=1.2,
        rsi=55.0,
        tot_sel_req=1_000.0,
        tot_buy_req=1_000.0,
        market_cap=50_000_000_000.0,
    )


def forces(velocity: float) -> dict[str, float]:
    return {
        "current_velocity": velocity,
        "thrust": 0.1,
        "gravity": 0.0,
        "drag": 0.0,
        "magnetic": 0.0,
        "jerk": 0.0,
        "impulse": 0.0,
        "net_force": 0.1,
    }


def persisted_state(at: datetime, *, volume=100.0, price=70_000.0, velocity=1.0):
    return PhysicalTrackerState(
        1, "005930", velocity, volume, price, (10.0,), at, at
    )


def _canonical_rows(db, *stock_codes):
    placeholders = ", ".join("?" for _ in stock_codes)
    where = f"WHERE stock_code IN ({placeholders})" if stock_codes else ""
    return tuple(
        tuple(row)
        for row in db.conn.execute(
            f"""
            SELECT stock_code, schema_version, velocity,
                   last_cumulative_volume, last_price, interval_volume_history,
                   last_observed_at, updated_at, projection_generation,
                   projection_velocity, projection_thrust, projection_gravity,
                   projection_drag, projection_magnetic, projection_jerk,
                   projection_impulse, projection_net_force,
                   projection_last_updated
            FROM physical_tracker_state_v1
            {where}
            ORDER BY stock_code
            """,
            stock_codes,
        ).fetchall()
    )


def _legacy_rows(db, *stock_codes):
    placeholders = ", ".join("?" for _ in stock_codes)
    where = f"WHERE stock_code IN ({placeholders})" if stock_codes else ""
    return tuple(
        tuple(row)
        for row in db.conn.execute(
            f"""
            SELECT stock_code, velocity, thrust, gravity, drag, magnetic,
                   jerk, impulse, net_force, last_updated
            FROM physics_state
            {where}
            ORDER BY stock_code
            """,
            stock_codes,
        ).fetchall()
    )


def test_two_cycles_consume_full_snapshot_across_database_reopen(tmp_path):
    db_path = tmp_path / "continuity.db"
    seeded_at = datetime(2026, 8, 8, 9, 59, tzinfo=KST)
    cycle_1_at = seeded_at + timedelta(minutes=1)
    clock = MutableClock(cycle_1_at)
    first_db = TradeLogger(db_path, clock=clock)
    first_repo = AsyncPhysicalStateRepository(first_db)
    seeded = PhysicalTrackerState(
        1, "005930", 1.25, 1_000.0, 69_900.0, (25.0,), seeded_at, seeded_at
    )
    first_repo.persist_physical_state(seeded, forces(seeded.velocity))
    first_tracker = PhysicalStateTracker(first_repo, clock=clock)
    first = first_tracker.process_observation(
        observation(cycle_1_at, volume=1_100.0, price=70_000.0)
    )
    first_db.close()

    cycle_2_at = cycle_1_at + timedelta(minutes=1)
    clock.now = cycle_2_at
    second_db = TradeLogger(db_path, clock=clock)
    second_repo = AsyncPhysicalStateRepository(second_db)
    second_tracker = PhysicalStateTracker(second_repo, clock=clock)
    second = second_tracker.process_observation(
        observation(cycle_2_at, volume=1_150.0, price=70_100.0)
    )
    hydrated = second_repo.load_physical_state("005930")

    assert hydrated.source is PhysicalStateHydrationSource.PERSISTED
    assert hydrated.state is not None
    assert hydrated.state.last_cumulative_volume == 1_150.0
    assert hydrated.state.last_price == 70_100.0
    assert hydrated.state.interval_volume_history == (25.0, 100.0, 50.0)
    assert hydrated.state.last_observed_at == cycle_2_at
    evidence = second["continuity"]
    assert evidence.hydration_source == "persisted"
    assert evidence.previous_observed_at == cycle_1_at
    assert evidence.history_depth == 3
    assert evidence.baseline_source == "row_4_fixed_cadence"
    assert evidence.baseline_sample_index == 4
    assert evidence.baseline_time_estimated is True
    assert first["forces"]["current_velocity"] != second["forces"]["current_velocity"]
    second_db.close()


def test_fresh_tracker_jerk_uses_api_baseline_without_private_history(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "fresh.db", clock=lambda: now)
    tracker = PhysicalStateTracker(AsyncPhysicalStateRepository(db), clock=lambda: now)
    result = tracker.process_observation(
        observation(now, volume=1_000.0, price=70_000.0, strength=90.0, baseline=110.0)
    )
    assert result["forces"]["jerk"] < 0.0
    assert not hasattr(tracker, "_strength_history")
    db.close()


def test_real_analyzer_tracker_sqlite_row_zero_row_four_end_to_end(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)

    class Gateway:
        def get_minute_chart(self, stock_code, tic):
            return [
                {
                    "cur_prc": "70000", "open_pric": "70000",
                    "high_pric": "70100", "low_pric": "69900", "trde_qty": "10",
                }
            ] * 15

        def get_stock_basic_info(self, stock_code):
            return {
                "trde_pre": "1.2", "trde_qty": "1000",
                "cur_prc": "70000", "mac": "1000",
            }

        def get_tick_strength(self, stock_code):
            return [
                {"cntr_str": value}
                for value in ("90", "95", "100", "105", "110")
            ]

        def get_order_book(self, stock_code):
            return {"tot_sel_req": "1000", "tot_buy_req": "1000"}

    db = TradeLogger(tmp_path / "analyzer-e2e.db", clock=lambda: now)
    tracker = PhysicalStateTracker(AsyncPhysicalStateRepository(db), clock=lambda: now)
    analyzer = MarketAnalyzer(
        Gateway(), {"proxy_code": "069500"}, tracker, clock=lambda: now
    )
    analyzer.update_priority_supply(["005930"])

    data = analyzer.supply_cache["005930"]
    assert data.forces["jerk"] < 0.0
    assert data.continuity is not None
    assert data.continuity.baseline_sample_index == 4
    assert db.load_physical_state("005930").source is PhysicalStateHydrationSource.PERSISTED
    db.close()


def test_worker_failure_keeps_memory_and_database_prior_and_poisons_later_submits(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / "ack.db", clock=clock)
    repo = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repo, clock=clock)
    tracker.process_observation(observation(now, volume=100.0, price=70_000.0))
    prior_memory = tracker.current_state("005930")
    prior_db = repo.load_physical_state("005930")
    assert prior_memory is not None
    assert prior_memory.last_cumulative_volume == 100.0
    assert prior_memory.last_observed_at == now
    assert prior_db.state is not None
    assert prior_db.state.last_cumulative_volume == 100.0
    assert prior_db.state.last_observed_at == now
    db.conn.execute(
        """
        CREATE TRIGGER reject_projection BEFORE UPDATE ON physics_state
        BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END
        """
    )
    db.conn.commit()
    clock.now = now + timedelta(minutes=1)

    with pytest.raises(PhysicalStatePersistenceError, match="injected commit failure"):
        tracker.process_observation(
            observation(clock.now, volume=150.0, price=70_100.0)
        )
    assert tracker.current_state("005930") == prior_memory
    assert repo.load_physical_state("005930") == prior_db
    assert db._async_queue.unfinished_tasks == 0

    clock.now += timedelta(minutes=1)
    with pytest.raises(PhysicalStatePersistenceError, match="injected commit failure"):
        tracker.process_observation(
            observation(clock.now, volume=160.0, price=70_200.0)
        )
    assert db._async_queue.unfinished_tasks == 0
    with pytest.raises(PhysicalStatePersistenceError, match="injected commit failure"):
        db.submit_physical_state("005930", forces(9.0))
    assert db._async_queue.unfinished_tasks == 0
    with pytest.raises(PhysicalStatePersistenceError):
        db.close()


def test_second_target_transition_failure_keeps_all_staged_memory_unpublished():
    previous_at = datetime(2026, 8, 8, 9, 59, tzinfo=KST)
    now = previous_at + timedelta(minutes=1)

    class Repository:
        def __init__(self):
            self.batch_calls = 0

        def load_physical_state(self, stock_code):
            if stock_code == "SECOND":
                return PhysicalStateLoadResult(
                    PhysicalStateHydrationSource.PERSISTED,
                    PhysicalTrackerState(
                        1,
                        stock_code,
                        1.0,
                        200.0,
                        70_000.0,
                        (10.0,),
                        previous_at,
                        previous_at,
                    ),
                )
            return PhysicalStateLoadResult(
                PhysicalStateHydrationSource.INITIAL,
                None,
            )

        def persist_physical_state(self, state, force_values):
            raise AssertionError("batch path must own persistence")

        def persist_physical_state_batch(self, writes):
            self.batch_calls += 1
            raise AssertionError("invalid transitions must not persist")

        def close(self):
            pass

    repository = Repository()
    tracker = PhysicalStateTracker(repository, clock=lambda: now)

    with pytest.raises(PhysicalStateValidationError, match="regressed"):
        tracker.process_observations(
            (
                observation(
                    now,
                    stock_code="FIRST",
                    volume=100.0,
                    price=70_000.0,
                ),
                observation(
                    now,
                    stock_code="SECOND",
                    volume=100.0,
                    price=70_000.0,
                ),
            )
        )

    assert repository.batch_calls == 0
    assert tracker.current_state("FIRST") is None
    assert tracker.current_state("SECOND") is None


def test_second_target_sql_failure_rolls_back_whole_batch_and_all_memories(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "batch-rollback.db", clock=lambda: now)
    repository = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repository, clock=lambda: now)
    db.conn.execute(
        """
        CREATE TRIGGER reject_second_canonical
        BEFORE INSERT ON physical_tracker_state_v1
        WHEN NEW.stock_code = 'SECOND'
        BEGIN SELECT RAISE(ABORT, 'injected second target failure'); END
        """
    )
    db.conn.commit()

    with pytest.raises(
        PhysicalStatePersistenceError,
        match="injected second target failure",
    ):
        tracker.process_observations(
            (
                observation(
                    now,
                    stock_code="FIRST",
                    volume=100.0,
                    price=70_000.0,
                ),
                observation(
                    now,
                    stock_code="SECOND",
                    volume=100.0,
                    price=70_000.0,
                ),
            )
        )

    assert tracker.current_state("FIRST") is None
    assert tracker.current_state("SECOND") is None
    assert db.conn.execute(
        "SELECT COUNT(*) FROM physical_tracker_state_v1"
    ).fetchone()[0] == 0
    assert db.conn.execute(
        "SELECT COUNT(*) FROM physics_state"
    ).fetchone()[0] == 0
    with pytest.raises(PhysicalStatePersistenceError, match="second target"):
        db.close()


def test_two_target_final_commit_failure_rolls_back_database_and_memories(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / "batch-final-commit.db", clock=clock)
    repository = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repository, clock=clock)
    tracker.process_observations(
        (
            observation(now, stock_code="FIRST", volume=100.0, price=70_000.0),
            observation(now, stock_code="SECOND", volume=200.0, price=80_000.0),
        )
    )
    prior_memories = {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    }
    prior_canonical = _canonical_rows(db, "FIRST", "SECOND")
    prior_legacy = _legacy_rows(db, "FIRST", "SECOND")

    class FinalCommitFailure:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def commit(self):
            raise RuntimeError("injected final commit failure")

    db._worker_conn = FinalCommitFailure(db._worker_conn)
    clock.now += timedelta(minutes=1)

    with pytest.raises(PhysicalStatePersistenceError, match="final commit failure"):
        tracker.process_observations(
            (
                observation(
                    clock.now,
                    stock_code="FIRST",
                    volume=150.0,
                    price=70_100.0,
                ),
                observation(
                    clock.now,
                    stock_code="SECOND",
                    volume=250.0,
                    price=80_100.0,
                ),
            )
        )

    assert {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    } == prior_memories
    assert _canonical_rows(db, "FIRST", "SECOND") == prior_canonical
    assert _legacy_rows(db, "FIRST", "SECOND") == prior_legacy
    queued_before = db._async_queue.unfinished_tasks
    third_state = PhysicalTrackerState(
        1,
        "THIRD",
        1.0,
        1.0,
        1.0,
        (1.0,),
        clock.now,
        clock.now,
    )
    with pytest.raises(PhysicalStatePersistenceError, match="final commit failure"):
        db.submit_physical_tracker_state(third_state, forces(1.0))
    with pytest.raises(PhysicalStatePersistenceError, match="final commit failure"):
        db.submit_physical_state("THIRD", forces(1.0))
    assert db._async_queue.unfinished_tasks == queued_before
    assert _canonical_rows(db, "THIRD") == ()
    assert _legacy_rows(db, "THIRD") == ()
    with pytest.raises(PhysicalStatePersistenceError, match="final commit failure"):
        db.close()


@pytest.mark.parametrize("mismatch", ["generation", "order", "committed_at"])
def test_worker_false_coherent_receipt_rolls_back_before_commit_and_poisons(
    mismatch,
    tmp_path,
):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / f"batch-receipt-{mismatch}.db", clock=clock)
    repository = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repository, clock=clock)
    initial = (
        observation(now, stock_code="FIRST", volume=100.0, price=70_000.0),
        observation(now, stock_code="SECOND", volume=200.0, price=80_000.0),
    )
    tracker.process_observations(initial)
    prior_memories = {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    }
    prior_canonical = tuple(
        db.conn.execute(
            """
            SELECT stock_code, last_cumulative_volume, last_observed_at
            FROM physical_tracker_state_v1 ORDER BY stock_code
            """
        ).fetchall()
    )
    prior_legacy = tuple(
        db.conn.execute(
            """
            SELECT stock_code, velocity, last_updated
            FROM physics_state ORDER BY stock_code
            """
        ).fetchall()
    )
    original_persist_item = db._persist_physical_item

    def false_receipt(item):
        receipt = original_persist_item(item)
        assert receipt is not None
        stock_code = receipt.stock_code
        generation = receipt.generation
        committed_at = receipt.committed_at
        if mismatch == "generation":
            generation = "false-but-coherent-generation"
        elif mismatch == "order":
            stock_code = "SECOND" if stock_code == "FIRST" else "FIRST"
        else:
            committed_at += timedelta(seconds=1)
        return PhysicalStateCommitReceipt(stock_code, generation, committed_at)

    db._persist_physical_item = false_receipt
    clock.now += timedelta(minutes=1)

    with pytest.raises(PhysicalStatePersistenceError, match="precommit attestation"):
        tracker.process_observations(
            (
                observation(
                    clock.now,
                    stock_code="FIRST",
                    volume=150.0,
                    price=70_100.0,
                ),
                observation(
                    clock.now,
                    stock_code="SECOND",
                    volume=250.0,
                    price=80_100.0,
                ),
            )
        )

    assert {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    } == prior_memories
    assert tuple(
        db.conn.execute(
            """
            SELECT stock_code, last_cumulative_volume, last_observed_at
            FROM physical_tracker_state_v1 ORDER BY stock_code
            """
        ).fetchall()
    ) == prior_canonical
    assert tuple(
        db.conn.execute(
            """
            SELECT stock_code, velocity, last_updated
            FROM physics_state ORDER BY stock_code
            """
        ).fetchall()
    ) == prior_legacy
    queued_before = db._async_queue.unfinished_tasks
    with pytest.raises(PhysicalStatePersistenceError, match="precommit attestation"):
        db.submit_physical_tracker_state(
            persisted_state(clock.now),
            forces(2.0),
        )
    assert db._async_queue.unfinished_tasks == queued_before
    with pytest.raises(PhysicalStatePersistenceError, match="precommit attestation"):
        db.close()


@pytest.mark.parametrize("mismatch", ["generation", "membership"])
def test_batch_receipt_mismatch_keeps_all_tracker_memories_unpublished(mismatch):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)

    class MismatchedReceiptRepository:
        def load_physical_state(self, stock_code):
            return PhysicalStateLoadResult(
                PhysicalStateHydrationSource.INITIAL,
                None,
            )

        def persist_physical_state(self, state, force_values):
            raise AssertionError("batch path must own persistence")

        def persist_physical_state_batch(self, writes):
            writes = tuple(writes)
            generation = (
                "wrong-generation"
                if mismatch == "generation"
                else writes[0].state.last_observed_at.isoformat()
            )
            receipts = tuple(
                PhysicalStateCommitReceipt(
                    write.state.stock_code,
                    generation,
                    write.state.updated_at,
                )
                for write in writes
            )
            if mismatch == "membership":
                receipts = tuple(reversed(receipts))
            return PhysicalStateBatchCommitReceipt(
                generation,
                receipts,
                writes[0].state.updated_at,
            )

        def close(self):
            pass

    tracker = PhysicalStateTracker(
        MismatchedReceiptRepository(),
        clock=lambda: now,
    )

    with pytest.raises(PhysicalStateValidationError, match="receipt"):
        tracker.process_observations(
            (
                observation(
                    now,
                    stock_code="FIRST",
                    volume=100.0,
                    price=70_000.0,
                ),
                observation(
                    now,
                    stock_code="SECOND",
                    volume=100.0,
                    price=70_000.0,
                ),
            )
        )

    assert tracker.current_state("FIRST") is None
    assert tracker.current_state("SECOND") is None


def test_frozen_observation_is_durable_and_matches_fresh_runtime(tmp_path):
    db_path = tmp_path / "frozen.db"
    first_at = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(first_at)
    db = TradeLogger(db_path, clock=clock)
    tracker = PhysicalStateTracker(AsyncPhysicalStateRepository(db), clock=clock)
    tracker.process_observation(observation(first_at, volume=100.0, price=70_000.0))
    clock.now += timedelta(minutes=1)
    frozen = tracker.process_observation(
        observation(clock.now, volume=100.0, price=70_100.0)
    )
    long_lived_state = tracker.current_state("005930")
    db.close()

    reopened = TradeLogger(db_path, clock=clock)
    fresh = PhysicalStateTracker(AsyncPhysicalStateRepository(reopened), clock=clock)
    fresh_state = fresh.load_or_initialize("005930")
    assert fresh_state == long_lived_state
    assert frozen["continuity"].previous_observed_at == first_at
    reopened.close()


def test_legacy_and_v1_are_separate_and_mixed_projection_cold_starts(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "mixed.db", clock=lambda: now)
    repo = AsyncPhysicalStateRepository(db)
    state = persisted_state(now)
    repo.persist_physical_state(state, forces(1.0))

    legacy_columns = {
        row[1] for row in db.conn.execute("PRAGMA table_info(physics_state)")
    }
    assert legacy_columns == {
        "stock_code", "velocity", "thrust", "gravity", "drag", "magnetic",
        "jerk", "impulse", "net_force", "last_updated",
    }
    legacy = db.get_last_physical_state("005930")
    assert legacy is not None and legacy["velocity"] == state.velocity

    db.submit_physical_state("005930", forces(88.0))
    db.flush()
    mixed = repo.load_physical_state("005930")
    assert mixed.source is PhysicalStateHydrationSource.LEGACY_COLD_START
    assert mixed.state is None
    db.close()


def test_old_writer_to_new_reader_is_legacy_cold_start(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "old-new.db", clock=lambda: now)
    db.submit_physical_state("005930", forces(12.0))
    db.flush()
    result = db.load_physical_state("005930")
    assert result.source is PhysicalStateHydrationSource.LEGACY_COLD_START
    assert db.get_last_physical_state("005930")["velocity"] == 12.0
    db.close()


def _insert_canonical(db, code, now, **overrides):
    values = {
        "schema": 1,
        "velocity": 1.0,
        "volume": 100.0,
        "price": 70_000.0,
        "history": "[]",
        "observed": now.isoformat(),
        "updated": now.isoformat(),
        "generation": now.isoformat(),
        "projection_velocity": 1.0,
        "projection_thrust": 0.0,
        "projection_gravity": 0.0,
        "projection_drag": 0.0,
        "projection_magnetic": 0.0,
        "projection_jerk": 0.0,
        "projection_impulse": 0.0,
        "projection_net_force": 0.0,
        "projection_time": now.strftime("%Y-%m-%d %H:%M:%S.%f"),
    }
    values.update(overrides)
    db.conn.execute(
        """
        INSERT INTO physical_tracker_state_v1
        (stock_code, schema_version, velocity, last_cumulative_volume, last_price,
         interval_volume_history, last_observed_at, updated_at,
         projection_generation, projection_velocity, projection_thrust,
         projection_gravity, projection_drag, projection_magnetic,
         projection_jerk, projection_impulse, projection_net_force,
         projection_last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code, values["schema"], values["velocity"], values["volume"],
            values["price"], values["history"], values["observed"], values["updated"],
            values["generation"], values["projection_velocity"],
            values["projection_thrust"], values["projection_gravity"],
            values["projection_drag"], values["projection_magnetic"],
            values["projection_jerk"], values["projection_impulse"],
            values["projection_net_force"], values["projection_time"],
        ),
    )
    db.conn.execute(
        """
        INSERT INTO physics_state
        (stock_code, velocity, thrust, gravity, drag, magnetic, jerk, impulse,
         net_force, last_updated) VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, ?)
        """,
        (code, values["projection_velocity"], values["projection_time"]),
    )
    db.conn.commit()


@pytest.mark.parametrize("schema_version", [2, 99])
def test_unknown_schema_fails_closed(schema_version, tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / f"unknown-{schema_version}.db", clock=lambda: now)
    _insert_canonical(db, "005930", now, schema=schema_version)
    with pytest.raises(PhysicalStateValidationError, match="unsupported"):
        db.load_physical_state("005930")
    db.close()


@pytest.mark.parametrize("history", ['[true]', '["10"]'])
def test_json_bool_and_string_history_are_rejected(history, tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "json.db", clock=lambda: now)
    _insert_canonical(db, "005930", now, history=history)
    with pytest.raises(PhysicalStateValidationError, match="non-numeric"):
        db.load_physical_state("005930")
    db.close()


def test_future_regressing_time_and_volume_do_not_advance(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / "guard.db", clock=clock)
    tracker = PhysicalStateTracker(AsyncPhysicalStateRepository(db), clock=clock)
    with pytest.raises(PhysicalStateValidationError, match="future"):
        tracker.process_observation(
            observation(now + timedelta(seconds=1), volume=100.0, price=70_000.0)
        )
    tracker.process_observation(observation(now, volume=100.0, price=70_000.0))
    prior = tracker.current_state("005930")
    clock.now += timedelta(minutes=1)
    with pytest.raises(PhysicalStateValidationError, match="regressed"):
        tracker.process_observation(
            observation(clock.now, volume=99.0, price=70_100.0)
        )
    assert tracker.current_state("005930") == prior
    db.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"stock_code": True},
        {"cumulative_volume": True},
        {"cumulative_volume": "10"},
        {"cumulative_volume": -1.0},
        {"current_price": 0.0},
        {"market_cap": 0.0},
        {"observed_at": "2026-08-08"},
    ],
)
def test_observation_rejects_invalid_types_and_bounds(changes):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    values = dict(
        stock_code="005930", observed_at=now, current_price=70_000.0,
        cumulative_volume=100.0, strength=100.0, prev_strength_5m=90.0,
        vwap=70_000.0, atr_percent=1.0, vol_ratio=1.0, rsi=50.0,
        tot_sel_req=1.0, tot_buy_req=1.0, market_cap=1_000_000.0,
    )
    values.update(changes)
    with pytest.raises(PhysicalStateValidationError):
        PhysicalObservation(**values)


@pytest.mark.parametrize("decay", [-0.1, math.inf, True, "0.5"])
def test_decay_constant_rejects_invalid_values(decay, tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "decay.db", clock=lambda: now)
    tracker = PhysicalStateTracker(AsyncPhysicalStateRepository(db), clock=lambda: now)
    with pytest.raises(PhysicalStateValidationError):
        tracker.load_or_initialize("005930", decay_constant=decay)
    db.close()


def test_incompatible_adapter_fails_fast():
    class WrongAdapter:
        def load_physical_state(self, stock_code):
            return None

    with pytest.raises(TypeError, match="PhysicalStateRepository"):
        PhysicalStateTracker(WrongAdapter())


def test_force_only_legacy_projection_mutation_cold_starts(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "force-mixed.db", clock=lambda: now)
    repo = AsyncPhysicalStateRepository(db)
    repo.persist_physical_state(persisted_state(now), forces(1.0))
    db.conn.execute(
        "UPDATE physics_state SET jerk = jerk + 1 WHERE stock_code = ?",
        ("005930",),
    )
    db.conn.commit()

    mixed = repo.load_physical_state("005930")
    assert mixed.source is PhysicalStateHydrationSource.LEGACY_COLD_START
    assert mixed.state is None
    db.close()


def test_corrupt_projection_generation_fails_closed(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "generation.db", clock=lambda: now)
    repo = AsyncPhysicalStateRepository(db)
    repo.persist_physical_state(persisted_state(now), forces(1.0))
    db.conn.execute(
        """
        UPDATE physical_tracker_state_v1
        SET projection_generation = ? WHERE stock_code = ?
        """,
        ((now - timedelta(minutes=1)).isoformat(), "005930"),
    )
    db.conn.commit()

    with pytest.raises(PhysicalStateValidationError, match="generation"):
        repo.load_physical_state("005930")
    db.close()


def test_ack_timeout_is_terminal_and_rejects_typed_and_legacy_without_enqueue(
    tmp_path,
):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / "ack-timeout.db", clock=clock)
    repo = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repo, clock=clock)
    tracker.process_observation(observation(now, volume=100.0, price=70_000.0))
    prior_memory = tracker.current_state("005930")
    original_persist = db._persist_physical_task
    entered = threading.Event()
    release = threading.Event()

    def blocked_persist(task):
        entered.set()
        assert release.wait(timeout=2)
        return original_persist(task)

    db._persist_physical_task = blocked_persist
    db._physical_ack_timeout = lambda: 0.05
    clock.now += timedelta(minutes=1)
    caught = []

    def submit_transition():
        try:
            tracker.process_observation(
                observation(clock.now, volume=150.0, price=70_100.0)
            )
        except Exception as error:
            caught.append(error)

    submitter = threading.Thread(target=submit_transition)
    submitter.start()
    assert entered.wait(timeout=1)
    submitter.join(timeout=1)
    assert not submitter.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], PhysicalStateCommitUnknownError)
    assert tracker.current_state("005930") == prior_memory

    queued_before = db._async_queue.unfinished_tasks
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.submit_physical_tracker_state(
            persisted_state(clock.now, volume=160.0, price=70_200.0),
            forces(2.0),
        )
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.submit_physical_state("005930", forces(3.0))
    assert db._async_queue.unfinished_tasks == queued_before

    release.set()
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.flush()
    late_row = db.conn.execute(
        """
        SELECT last_cumulative_volume, last_observed_at
        FROM physical_tracker_state_v1 WHERE stock_code = ?
        """,
        ("005930",),
    ).fetchone()
    assert late_row["last_cumulative_volume"] == 150.0
    assert late_row["last_observed_at"] == clock.now.isoformat()
    assert tracker.current_state("005930") == prior_memory
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.close()


def test_two_target_ack_timeout_late_commit_is_durable_all_or_none(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    clock = MutableClock(now)
    db = TradeLogger(tmp_path / "ack-timeout-two-target.db", clock=clock)
    repository = AsyncPhysicalStateRepository(db)
    tracker = PhysicalStateTracker(repository, clock=clock)
    tracker.process_observations(
        (
            observation(now, stock_code="FIRST", volume=100.0, price=70_000.0),
            observation(now, stock_code="SECOND", volume=200.0, price=80_000.0),
        )
    )
    prior_memories = {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    }
    prior_canonical = _canonical_rows(db, "FIRST", "SECOND")
    prior_legacy = _legacy_rows(db, "FIRST", "SECOND")
    original_persist = db._persist_physical_task
    entered = threading.Event()
    release = threading.Event()

    def blocked_persist(task):
        entered.set()
        assert release.wait(timeout=2)
        return original_persist(task)

    db._persist_physical_task = blocked_persist
    db._physical_ack_timeout = lambda: 0.05
    clock.now += timedelta(minutes=1)
    caught = []

    def submit_batch():
        try:
            tracker.process_observations(
                (
                    observation(
                        clock.now,
                        stock_code="FIRST",
                        volume=150.0,
                        price=70_100.0,
                    ),
                    observation(
                        clock.now,
                        stock_code="SECOND",
                        volume=250.0,
                        price=80_100.0,
                    ),
                )
            )
        except Exception as error:
            caught.append(error)

    submitter = threading.Thread(target=submit_batch)
    submitter.start()
    assert entered.wait(timeout=1)
    submitter.join(timeout=1)
    assert not submitter.is_alive()
    assert len(caught) == 1
    assert isinstance(caught[0], PhysicalStateCommitUnknownError)
    assert {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    } == prior_memories
    assert _canonical_rows(db, "FIRST", "SECOND") == prior_canonical
    assert _legacy_rows(db, "FIRST", "SECOND") == prior_legacy

    queued_before = db._async_queue.unfinished_tasks
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.submit_physical_state("THIRD", forces(3.0))
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.submit_physical_tracker_state(
            PhysicalTrackerState(
                1,
                "THIRD",
                1.0,
                1.0,
                1.0,
                (1.0,),
                clock.now,
                clock.now,
            ),
            forces(1.0),
        )
    assert db._async_queue.unfinished_tasks == queued_before
    assert _canonical_rows(db, "THIRD") == ()
    assert _legacy_rows(db, "THIRD") == ()

    release.set()
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.flush()
    canonical = {
        row["stock_code"]: row
        for row in db.conn.execute(
            """
            SELECT stock_code, last_cumulative_volume, last_observed_at,
                   projection_generation, projection_velocity,
                   projection_thrust, projection_gravity, projection_drag,
                   projection_magnetic, projection_jerk, projection_impulse,
                   projection_net_force, projection_last_updated
            FROM physical_tracker_state_v1
            WHERE stock_code IN ('FIRST', 'SECOND')
            """
        ).fetchall()
    }
    legacy = {
        row["stock_code"]: row
        for row in db.conn.execute(
            """
            SELECT stock_code, velocity, thrust, gravity, drag, magnetic,
                   jerk, impulse, net_force, last_updated
            FROM physics_state
            WHERE stock_code IN ('FIRST', 'SECOND')
            """
        ).fetchall()
    }
    assert set(canonical) == set(legacy) == {"FIRST", "SECOND"}
    assert {
        code: row["last_cumulative_volume"] for code, row in canonical.items()
    } == {"FIRST": 150.0, "SECOND": 250.0}
    expected_generation = clock.now.isoformat()
    expected_legacy_time = clock.now.strftime("%Y-%m-%d %H:%M:%S.%f")
    for code in ("FIRST", "SECOND"):
        canonical_row = canonical[code]
        legacy_row = legacy[code]
        assert canonical_row["last_observed_at"] == expected_generation
        assert canonical_row["projection_generation"] == expected_generation
        assert canonical_row["projection_last_updated"] == expected_legacy_time
        assert tuple(canonical_row[key] for key in (
            "projection_velocity",
            "projection_thrust",
            "projection_gravity",
            "projection_drag",
            "projection_magnetic",
            "projection_jerk",
            "projection_impulse",
            "projection_net_force",
            "projection_last_updated",
        )) == tuple(legacy_row[key] for key in (
            "velocity",
            "thrust",
            "gravity",
            "drag",
            "magnetic",
            "jerk",
            "impulse",
            "net_force",
            "last_updated",
        ))
    assert _canonical_rows(db, "FIRST", "SECOND") != prior_canonical
    assert _legacy_rows(db, "FIRST", "SECOND") != prior_legacy
    assert {
        code: tracker.current_state(code) for code in ("FIRST", "SECOND")
    } == prior_memories
    with pytest.raises(PhysicalStatePersistenceError, match="timed out"):
        db.close()


def test_ack_timeout_race_returns_receipt_when_commit_already_completed(
    monkeypatch,
    tmp_path,
):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    completion = database_module._PhysicalStateCompletion()

    class FalseWaitEvent(threading.Event):
        def wait(self, timeout=None):
            assert super().wait(timeout=1)
            return False

    completion.event = FalseWaitEvent()
    monkeypatch.setattr(
        database_module,
        "_PhysicalStateCompletion",
        lambda: completion,
    )
    db = TradeLogger(tmp_path / "ack-race.db", clock=lambda: now)
    receipt = db.submit_physical_tracker_state(
        persisted_state(now),
        forces(1.0),
    )

    assert receipt.generation == now.isoformat()
    assert db._worker_failure is None
    db.close()


def test_close_submission_race_is_latched_and_bounded(tmp_path):
    now = datetime(2026, 8, 8, 10, 0, tzinfo=KST)
    db = TradeLogger(tmp_path / "close-race.db", clock=lambda: now)
    original_persist = db._persist_physical_task
    entered = threading.Event()
    release = threading.Event()

    def blocked_persist(task):
        entered.set()
        assert release.wait(timeout=2)
        return original_persist(task)

    db._persist_physical_task = blocked_persist
    db.submit_physical_state("005930", forces(1.0))
    assert entered.wait(timeout=1)
    deadline = time.monotonic() + 0.1
    db.set_shutdown_deadline(lambda: deadline - time.monotonic())
    close_errors = []
    started = time.monotonic()

    def close_logger():
        try:
            db.close()
        except Exception as error:
            close_errors.append(error)

    closer = threading.Thread(target=close_logger)
    closer.start()
    while db._accepting_submissions and time.monotonic() < deadline:
        time.sleep(0.001)
    queued_before = db._async_queue.unfinished_tasks
    with pytest.raises(RuntimeError, match="closed"):
        db.submit_physical_state("005930", forces(2.0))
    with pytest.raises(RuntimeError, match="closed"):
        db.submit_physical_tracker_state(persisted_state(now), forces(2.0))
    assert db._async_queue.unfinished_tasks == queued_before
    closer.join(timeout=1)
    assert not closer.is_alive()
    assert time.monotonic() - started < 1.0
    assert any(isinstance(error, TradeLoggerLifecycleError) for error in close_errors)

    release.set()
    db._worker_thread.join(timeout=1)
    assert not db._worker_thread.is_alive()
