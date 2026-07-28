"""Physical-state persistence adapter backed by ``TradeLogger``."""

import threading
from typing import Any, Mapping, Optional

from kiwoom_stock.core.database import TradeLogger


class AsyncPhysicalStateRepository:
    """Submit snapshots to the queue worker owned by ``TradeLogger``."""

    def __init__(self, db_logger: TradeLogger):
        self._db = db_logger
        self._state_lock = threading.Lock()
        self._closed = False

    def get_last_physical_state(self, stock_code: str) -> Optional[Mapping[str, Any]]:
        return self._db.get_last_physical_state(stock_code)

    def submit_physical_state(self, stock_code: str, forces: Mapping[str, Any]) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("physical-state repository is closed")
            self._db.submit_physical_state(stock_code, dict(forces))

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
