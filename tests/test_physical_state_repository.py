from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateCommitReceipt,
    PhysicalStateLoadResult,
    PhysicalStateWrite,
    PhysicalTrackerState,
)
from kiwoom_stock.infrastructure.physical_state_repository import AsyncPhysicalStateRepository


NOW = datetime(2026, 8, 8, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))


class FakeTradeLogger:
    def __init__(self):
        self.submissions = []
        self.last_state = PhysicalStateLoadResult(
            PhysicalStateHydrationSource.PERSISTED,
            PhysicalTrackerState(1, "005930", 3.0, 100.0, 70_000.0, (10.0,), NOW, NOW),
        )

    def load_physical_state(self, stock_code):
        self.requested_stock_code = stock_code
        return self.last_state

    def submit_physical_tracker_state(self, state, forces):
        return self.submit_physical_tracker_state_batch(
            (PhysicalStateWrite(state, tuple(dict(forces).items())),)
        ).items[0]

    def submit_physical_tracker_state_batch(self, writes):
        writes = tuple(writes)
        self.submissions.extend(
            (write.state, dict(write.forces)) for write in writes
        )
        receipts = tuple(
            PhysicalStateCommitReceipt(
                write.state.stock_code,
                write.state.last_observed_at.isoformat(),
                write.state.updated_at,
            )
            for write in writes
        )
        return PhysicalStateBatchCommitReceipt(
            receipts[0].generation, receipts, receipts[0].committed_at
        )


def _forces(velocity=1.5):
    return {
        "current_velocity": velocity,
        "thrust": 0.3,
        "gravity": 0.0,
        "drag": 0.0,
        "magnetic": 0.0,
        "jerk": 0.0,
        "impulse": 0.0,
        "net_force": velocity,
    }


def test_async_physical_state_repository_delegates_typed_snapshot():
    db = FakeTradeLogger()
    repository = AsyncPhysicalStateRepository(db)

    assert repository.load_physical_state("005930") == db.last_state
    assert db.requested_stock_code == "005930"

    state = db.last_state.state
    assert state is not None
    forces = _forces()
    receipt = repository.persist_physical_state(state, forces)
    forces["current_velocity"] = 99.0

    assert db.submissions == [(state, _forces())]
    assert receipt.generation == NOW.isoformat()


def test_async_physical_state_repository_close_is_idempotent_and_rejects_submit():
    db = FakeTradeLogger()
    repository = AsyncPhysicalStateRepository(db)
    state = db.last_state.state
    assert state is not None

    repository.close()
    repository.close()

    with pytest.raises(RuntimeError, match="repository is closed"):
        repository.persist_physical_state(state, _forces())

    assert db.submissions == []
    assert repository.load_physical_state("005930") == db.last_state
