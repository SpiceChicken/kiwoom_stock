"""Deterministic point-in-time replay and artifact-location contracts."""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from kiwoom_stock.utils.market_cal import KST, krx_session_ordinals


class ReplayDataError(ValueError):
    """Replay input is malformed, future-leaking, or not chronologically valid."""


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(KST).isoformat(timespec="microseconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {field.name: _wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list, frozenset)):
        return [_wire(item) for item in value]
    return value


def canonical_replay_hash(value: Any) -> str:
    encoded = json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True)
class ReplayEvent:
    event_id: str
    session_date: date
    decision_at: datetime
    available_at: datetime
    source_snapshot_id: str
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.event_id.strip() or not self.source_snapshot_id.strip() or not isinstance(self.session_date, date):
            raise ReplayDataError("replay event identity is invalid")
        for value, name in ((self.decision_at, "decision_at"), (self.available_at, "available_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ReplayDataError(f"{name} must be timezone-aware")
        if self.available_at >= self.decision_at:
            raise ReplayDataError("replay source must be available strictly before decision")
        if not isinstance(self.payload, Mapping):
            raise ReplayDataError("replay payload must be a mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class ReplayResult:
    dataset_id: str
    strategy_semantics_version: str
    input_hash: str
    output_hash: str
    decision_count: int
    decision_ids: tuple[str, ...]
    artifact_paths: tuple[str, ...] = ()
    side_effects: bool = False
    selection_receipt: ReplaySelectionReceipt | None = None

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.strategy_semantics_version.strip():
            raise ValueError("replay identity is required")
        if self.decision_count != len(self.decision_ids) or len(set(self.decision_ids)) != len(self.decision_ids):
            raise ValueError("replay decision count/identity mismatch")
        if self.side_effects:
            raise ValueError("replay must remain side-effect free")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "dataset_id": self.dataset_id,
            "strategy_semantics_version": self.strategy_semantics_version,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "decision_count": self.decision_count,
            "decision_ids": list(self.decision_ids),
            "artifact_paths": list(self.artifact_paths),
            "side_effects": self.side_effects,
        }
        if self.selection_receipt is not None:
            result["selection_receipt"] = self.selection_receipt.to_dict()
        return result


@dataclass(frozen=True)
class ChronologicalSplit:
    train_end: date
    test_start: date
    purge_sessions: int = 0

    def __post_init__(self) -> None:
        if self.test_start <= self.train_end or self.purge_sessions < 0:
            raise ReplayDataError("chronological split boundaries are invalid")

    def allows_test_session(self, session: date) -> bool:
        return session >= self.test_start


PURGE_POLICY_VERSION = "session-tail-purge-v1"


@dataclass(frozen=True)
class ReplaySelectionReceipt:
    """Hashable proof of full-to-train/purged/test event selection."""

    policy_version: str
    train_end: date
    test_start: date
    purge_sessions: int
    full_event_ids: tuple[str, ...]
    train_event_ids: tuple[str, ...]
    purged_event_ids: tuple[str, ...]
    test_event_ids: tuple[str, ...]
    purged_session_ordinals: tuple[tuple[str, int], ...]
    full_input_hash: str
    selection_hash: str

    @property
    def selected_event_ids(self) -> tuple[str, ...]:
        selected = set(self.train_event_ids) | set(self.test_event_ids)
        return tuple(
            event_id for event_id in self.full_event_ids if event_id in selected
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "train_end": self.train_end.isoformat(),
            "test_start": self.test_start.isoformat(),
            "purge_sessions": self.purge_sessions,
            "full_event_ids": list(self.full_event_ids),
            "train_event_ids": list(self.train_event_ids),
            "purged_event_ids": list(self.purged_event_ids),
            "test_event_ids": list(self.test_event_ids),
            "purged_session_ordinals": [
                {"session_date": session, "ordinal": ordinal}
                for session, ordinal in self.purged_session_ordinals
            ],
            "full_input_hash": self.full_input_hash,
            "selection_hash": self.selection_hash,
        }


def select_replay_events(
    events: tuple[ReplayEvent, ...],
    split: ChronologicalSplit,
) -> tuple[tuple[ReplayEvent, ...], ReplaySelectionReceipt]:
    """Apply policy A: purge the train tail by XKRX session ordinals."""

    if not events:
        raise ReplayDataError("purged replay requires at least one event")
    if any(
        events[index].decision_at > events[index + 1].decision_at
        for index in range(len(events) - 1)
    ):
        raise ReplayDataError("replay events are not chronologically ordered")
    if len({event.event_id for event in events}) != len(events):
        raise ReplayDataError("replay event ids must be unique")

    boundary_dates = [event.session_date for event in events]
    boundary_dates.extend((split.train_end, split.test_start))
    try:
        ordinals = krx_session_ordinals(
            min(boundary_dates), max(boundary_dates)
        )
    except Exception as error:
        raise ReplayDataError("XKRX session selection is unavailable") from error
    if split.train_end not in ordinals or split.test_start not in ordinals:
        raise ReplayDataError("split boundaries must be XKRX sessions")

    event_ordinals: dict[str, int] = {}
    for event in events:
        ordinal = ordinals.get(event.session_date)
        if ordinal is None:
            raise ReplayDataError("replay event session must be an XKRX session")
        event_ordinals[event.event_id] = ordinal

    train_ordinal = ordinals[split.train_end]
    test_ordinal = ordinals[split.test_start]
    gap_sessions = test_ordinal - train_ordinal
    if split.purge_sessions > gap_sessions:
        raise ReplayDataError("purge session gap is insufficient")
    purge_start = test_ordinal - split.purge_sessions

    train_ids: list[str] = []
    purged_ids: list[str] = []
    test_ids: list[str] = []
    for event in events:
        ordinal = event_ordinals[event.event_id]
        if ordinal >= test_ordinal:
            test_ids.append(event.event_id)
        elif ordinal <= train_ordinal and ordinal < purge_start:
            train_ids.append(event.event_id)
        else:
            purged_ids.append(event.event_id)

    purged_sessions = tuple(
        (session.isoformat(), ordinal)
        for session, ordinal in sorted(ordinals.items(), key=lambda item: item[1])
        if train_ordinal < ordinal < test_ordinal
        or (
            split.purge_sessions > 0
            and purge_start <= ordinal <= train_ordinal
        )
    )
    full_ids = tuple(event.event_id for event in events)
    train_tuple = tuple(train_ids)
    purged_tuple = tuple(purged_ids)
    test_tuple = tuple(test_ids)
    selection_payload = {
        "policy_version": PURGE_POLICY_VERSION,
        "train_end": split.train_end,
        "test_start": split.test_start,
        "purge_sessions": split.purge_sessions,
        "full_event_ids": full_ids,
        "train_event_ids": train_tuple,
        "purged_event_ids": purged_tuple,
        "test_event_ids": test_tuple,
        "purged_session_ordinals": purged_sessions,
    }
    receipt = ReplaySelectionReceipt(
        policy_version=PURGE_POLICY_VERSION,
        train_end=split.train_end,
        test_start=split.test_start,
        purge_sessions=split.purge_sessions,
        full_event_ids=full_ids,
        train_event_ids=train_tuple,
        purged_event_ids=purged_tuple,
        test_event_ids=test_tuple,
        purged_session_ordinals=purged_sessions,
        full_input_hash=canonical_replay_hash(events),
        selection_hash=canonical_replay_hash(selection_payload),
    )
    selected_ids = set(receipt.selected_event_ids)
    selected_events = tuple(
        event for event in events if event.event_id in selected_ids
    )
    if not selected_events:
        raise ReplayDataError("purge selection left no train or test events")
    return selected_events, receipt


@dataclass(frozen=True)
class CsvArtifactLocator:
    """Resolve report paths without creating directories or writing files."""

    output_root: Path
    session_date: date

    @property
    def session_directory(self) -> Path:
        return self.output_root / "output" / self.session_date.strftime("%Y%m%d")

    def resolve(self, filename: str) -> Path:
        if not filename.endswith(".csv") or Path(filename).name != filename:
            raise ValueError("CSV artifact filename must be a direct .csv name")
        return self.session_directory / filename


def run_replay(
    *,
    events: tuple[ReplayEvent, ...],
    dataset_id: str,
    strategy_semantics_version: str,
    evaluator: Callable[[ReplayEvent], Mapping[str, Any]],
    artifact_paths: tuple[str, ...] = (),
    split: ChronologicalSplit | None = None,
) -> ReplayResult:
    """Run a deterministic evaluator over an already point-in-time event stream."""

    selection_receipt: ReplaySelectionReceipt | None = None
    selected_events = events
    if split is not None:
        selected_events, selection_receipt = select_replay_events(events, split)
    if any(
        selected_events[index].decision_at > selected_events[index + 1].decision_at
        for index in range(len(selected_events) - 1)
    ):
        raise ReplayDataError("replay events are not chronologically ordered")
    if len({event.event_id for event in selected_events}) != len(selected_events):
        raise ReplayDataError("replay event ids must be unique")
    input_hash = canonical_replay_hash(selected_events)
    outputs: list[Mapping[str, Any]] = []
    for event in selected_events:
        output = evaluator(event)
        if not isinstance(output, Mapping):
            raise ReplayDataError("replay evaluator must return a mapping")
        outputs.append(dict(output))
    output_hash = canonical_replay_hash(
        tuple(
            (event.event_id, output)
            for event, output in zip(selected_events, outputs)
        )
    )
    return ReplayResult(
        dataset_id,
        strategy_semantics_version,
        input_hash,
        output_hash,
        len(selected_events),
        tuple(event.event_id for event in selected_events),
        artifact_paths,
        selection_receipt=selection_receipt,
    )
