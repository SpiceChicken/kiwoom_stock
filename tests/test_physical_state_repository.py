from datetime import datetime

import pytest

from kiwoom_stock.infrastructure.physical_state_repository import AsyncPhysicalStateRepository


class FakeTradeLogger:
    def __init__(self):
        self.submissions = []
        self.last_state = {
            "velocity": 3.0,
            "timestamp": datetime(2026, 7, 17, 10, 0, 0),
        }

    def get_last_physical_state(self, stock_code):
        self.requested_stock_code = stock_code
        return self.last_state

    def submit_physical_state(self, stock_code, forces):
        self.submissions.append((stock_code, forces))


def test_async_physical_state_repository_delegates_recovery_and_enqueues_directly():
    db = FakeTradeLogger()
    repository = AsyncPhysicalStateRepository(db)

    assert repository.get_last_physical_state("005930") == db.last_state
    assert db.requested_stock_code == "005930"

    forces = {"current_velocity": 1.5, "thrust": 0.3}
    repository.submit_physical_state("005930", forces)
    forces["current_velocity"] = 99.0

    assert db.submissions == [("005930", {"current_velocity": 1.5, "thrust": 0.3})]


def test_async_physical_state_repository_close_is_idempotent_and_rejects_submit():
    db = FakeTradeLogger()
    repository = AsyncPhysicalStateRepository(db)

    repository.close()
    repository.close()

    with pytest.raises(RuntimeError, match="repository is closed"):
        repository.submit_physical_state("005930", {"current_velocity": 1.5})

    assert db.submissions == []
    assert repository.get_last_physical_state("005930") == db.last_state
