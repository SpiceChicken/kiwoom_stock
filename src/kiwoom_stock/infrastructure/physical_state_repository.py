"""Physical-state persistence adapter backed by ``TradeLogger``."""

import copy
import threading
from typing import Any, Mapping, Sequence

from kiwoom_stock.application.ports import PhysicalStatePersistenceError
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateLoadResult,
    PhysicalStateWrite,
    PhysicalTrackerState,
)


class AsyncPhysicalStateRepository:
    """Submit snapshots to the queue worker owned by ``TradeLogger``."""

    def __init__(self, db_logger: TradeLogger):
        self._db = db_logger
        self._state_lock = threading.Lock()
        self._closed = False

    def load_physical_state(self, stock_code: str) -> PhysicalStateLoadResult:
        return self._db.load_physical_state(stock_code)

    def persist_physical_state(
        self,
        state: PhysicalTrackerState,
        forces: Mapping[str, Any],
    ) -> PhysicalStateCommitReceipt:
        with self._state_lock:
            if self._closed:
                raise PhysicalStatePersistenceError(
                    "physical-state repository is closed"
                )
        write = PhysicalStateWrite(state, tuple(dict(forces).items()))
        return self.persist_physical_state_batch((write,)).items[0]

    def persist_physical_state_batch(
        self,
        writes: Sequence[PhysicalStateWrite],
    ) -> PhysicalStateBatchCommitReceipt:
        with self._state_lock:
            if self._closed:
                raise PhysicalStatePersistenceError(
                    "physical-state repository is closed"
                )
            immutable_writes = tuple(copy.deepcopy(write) for write in writes)
            return self._db.submit_physical_tracker_state_batch(immutable_writes)

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
