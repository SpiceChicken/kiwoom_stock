"""Dependency-free in-memory observer for the shadow execution graph."""

from typing import Any, Dict, List


class LocalShadowNotifier:
    def __init__(self) -> None:
        self.status_data: List[Dict[str, Any]] = []
        self.counters = {
            "status": 0,
            "paper_buy": 0,
            "paper_sell": 0,
            "error": 0,
            "critical": 0,
        }

    def start_status_session(self) -> None:
        self.status_data = []

    def collect_status(self, data: Dict[str, Any]) -> None:
        self.status_data.append(dict(data))
        self.counters["status"] += 1

    def flush_status(self, _regime: str) -> None:
        return None

    def notify_buy(self, _buy_data: Dict[str, Any]) -> None:
        self.counters["paper_buy"] += 1

    def notify_sell(self, _position: Any) -> None:
        self.counters["paper_sell"] += 1

    def notify_error(self, _message: str) -> None:
        self.counters["error"] += 1

    def notify_critical(self, _message: str) -> None:
        self.counters["critical"] += 1

    def safe_counts(self) -> Dict[str, int]:
        return dict(self.counters)
