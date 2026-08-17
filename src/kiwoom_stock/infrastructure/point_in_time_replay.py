"""Offline point-in-time source; no broker, network, or current-universe lookup."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from kiwoom_stock.application.swing_replay import (
    CsvArtifactLocator,
    ReplayDataError,
    ReplayEvent,
)
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.domain.swing_contracts import (
    AdmissionResult,
    EpisodeRearmEvidence,
    EpisodeSnapshot,
    EpisodeState,
)
from kiwoom_stock.domain.swing_strategy import (
    FastContext,
    PositionContext,
    RiskContext,
    SlowContext,
    SwingEvaluationContext,
    SwingStrategyPolicy,
)


@dataclass(frozen=True)
class PointInTimeReplaySource:
    events: tuple[ReplayEvent, ...]

    def __post_init__(self) -> None:
        if any(self.events[index].decision_at > self.events[index + 1].decision_at for index in range(len(self.events) - 1)):
            raise ReplayDataError("point-in-time source must be chronological")
        if len({event.event_id for event in self.events}) != len(self.events):
            raise ReplayDataError("point-in-time event ids must be unique")

    def available_before(self, decision_at: datetime) -> tuple[ReplayEvent, ...]:
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ReplayDataError("decision_at must be timezone-aware")
        return tuple(
            event
            for event in self.events
            if event.decision_at <= decision_at and event.available_at < decision_at
        )

    def for_decision(self, event_id: str) -> tuple[ReplayEvent, ...]:
        target = next((event for event in self.events if event.event_id == event_id), None)
        if target is None:
            raise ReplayDataError("unknown replay event")
        return self.available_before(target.decision_at)


SWING_PIT_REPLAY_SCHEMA = "swing-pit-replay-v1"
SWING_PIT_REPLAY_COLUMNS = (
    "schema_version",
    "event_id",
    "session_date",
    "decision_at",
    "available_at",
    "source_snapshot_id",
    "payload_json",
)


@dataclass(frozen=True)
class CsvPITReplaySource:
    """Strict, read-only loader for the approved PIT replay CSV contract."""

    dataset_id: str
    path: Path
    events: tuple[ReplayEvent, ...]

    def __post_init__(self) -> None:
        if not self.dataset_id.strip() or not self.path.is_absolute():
            raise ReplayDataError("PIT CSV dataset identity/path is invalid")
        if not self.events:
            raise ReplayDataError("PIT CSV must contain at least one event")

    @classmethod
    def load(cls, path: str | Path, *, dataset_id: str) -> "CsvPITReplaySource":
        csv_path = Path(path)
        if not csv_path.is_absolute():
            raise ReplayDataError("PIT CSV path must be absolute")
        if csv_path.is_symlink() or not csv_path.is_file():
            raise ReplayDataError("PIT CSV path must be a regular non-symlink file")
        if not dataset_id.strip():
            raise ReplayDataError("PIT CSV dataset id is required")

        events: list[ReplayEvent] = []
        try:
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                if fieldnames is None or tuple(fieldnames) != SWING_PIT_REPLAY_COLUMNS:
                    raise ReplayDataError(
                        "PIT CSV header must exactly match the approved schema"
                    )
                for row_number, row in enumerate(reader, start=2):
                    if row.get(None) is not None or any(
                        value is None for value in row.values()
                    ):
                        raise ReplayDataError(
                            f"PIT CSV row {row_number} has missing or extra fields"
                        )
                    if row["schema_version"] != SWING_PIT_REPLAY_SCHEMA:
                        raise ReplayDataError(
                            f"PIT CSV row {row_number} has an unsupported schema version"
                        )
                    try:
                        payload = json.loads(row["payload_json"])
                    except json.JSONDecodeError as error:
                        raise ReplayDataError(
                            f"PIT CSV row {row_number} payload_json is invalid JSON"
                        ) from error
                    if not isinstance(payload, Mapping):
                        raise ReplayDataError(
                            f"PIT CSV row {row_number} payload_json must be an object"
                        )
                    events.append(
                        ReplayEvent(
                            row["event_id"],
                            date.fromisoformat(row["session_date"]),
                            datetime.fromisoformat(row["decision_at"]),
                            datetime.fromisoformat(row["available_at"]),
                            row["source_snapshot_id"],
                            payload,
                        )
                    )
        except OSError as error:
            raise ReplayDataError("PIT CSV could not be read") from error
        except (TypeError, ValueError) as error:
            if isinstance(error, ReplayDataError):
                raise
            raise ReplayDataError("PIT CSV contains an invalid typed event") from error

        if not events:
            raise ReplayDataError("PIT CSV must contain at least one event")
        try:
            PointInTimeReplaySource(tuple(events))
        except ReplayDataError as error:
            raise ReplayDataError("PIT CSV events are not a valid ordered source") from error
        return cls(dataset_id, csv_path, tuple(events))

    @classmethod
    def from_artifact(
        cls,
        *,
        output_root: str | Path,
        session_date: date,
        filename: str,
        dataset_id: str,
    ) -> "CsvPITReplaySource":
        """Load one CSV from the standard ``KIWOOM_OUTPUT_DIR`` artifact path."""

        locator = CsvArtifactLocator(Path(output_root).resolve(), session_date)
        return cls.load(locator.resolve(filename), dataset_id=dataset_id)

    def as_point_in_time_source(self) -> PointInTimeReplaySource:
        return PointInTimeReplaySource(self.events)


_SWING_CONTEXT_SCHEMA = "swing-context-v1"


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReplayDataError(f"{name} must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        raise ReplayDataError(f"{name} keys are invalid")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayDataError(f"{name} must be non-empty text")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ReplayDataError(f"{name} must be an aware ISO instant")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReplayDataError(f"{name} is not an ISO instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReplayDataError(f"{name} must be timezone-aware")
    return parsed


def _session_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ReplayDataError(f"{name} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ReplayDataError(f"{name} is not an ISO date") from error
    if parsed.isoformat() != value:
        raise ReplayDataError(f"{name} must use canonical ISO date text")
    return parsed


def _integer(value: object, name: str, *, positive: bool = False) -> int:
    if type(value) is not int or (value <= 0 if positive else value < 0):
        qualifier = "positive" if positive else "non-negative"
        raise ReplayDataError(f"{name} must be a {qualifier} integer")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise ReplayDataError(f"{name} must be boolean")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, str):
        raise ReplayDataError(f"{name} must be a Decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ReplayDataError(f"{name} is not a Decimal") from error
    if not parsed.is_finite():
        raise ReplayDataError(f"{name} must be finite")
    return parsed


def _slow(value: object) -> SlowContext:
    raw = _mapping(value, "swing_context.slow")
    _exact_keys(raw, {
        "session_date", "bar_closed_at", "available_at", "computed_at",
        "source_snapshot_id", "strategy_semantics_version", "lookback_sessions",
        "warmup_complete", "thesis_valid", "entry_eligible", "score",
    }, "swing_context.slow")
    return SlowContext(
        _session_date(raw["session_date"], "slow.session_date"),
        _aware(raw["bar_closed_at"], "slow.bar_closed_at"),
        _aware(raw["available_at"], "slow.available_at"),
        _aware(raw["computed_at"], "slow.computed_at"),
        _text(raw["source_snapshot_id"], "slow.source_snapshot_id"),
        _text(raw["strategy_semantics_version"], "slow.strategy_semantics_version"),
        _integer(raw["lookback_sessions"], "slow.lookback_sessions", positive=True),
        _boolean(raw["warmup_complete"], "slow.warmup_complete"),
        _boolean(raw["thesis_valid"], "slow.thesis_valid"),
        _boolean(raw["entry_eligible"], "slow.entry_eligible"),
        _decimal(raw["score"], "slow.score"),
    )


def _fast(value: object) -> FastContext:
    raw = _mapping(value, "swing_context.fast")
    _exact_keys(raw, {
        "bar_id", "bar_closed_at", "available_at", "computed_at",
        "source_snapshot_id", "strategy_semantics_version", "trigger_rising",
        "entry_eligible", "score",
    }, "swing_context.fast")
    return FastContext(
        _text(raw["bar_id"], "fast.bar_id"),
        _aware(raw["bar_closed_at"], "fast.bar_closed_at"),
        _aware(raw["available_at"], "fast.available_at"),
        _aware(raw["computed_at"], "fast.computed_at"),
        _text(raw["source_snapshot_id"], "fast.source_snapshot_id"),
        _text(raw["strategy_semantics_version"], "fast.strategy_semantics_version"),
        _boolean(raw["trigger_rising"], "fast.trigger_rising"),
        _boolean(raw["entry_eligible"], "fast.entry_eligible"),
        _decimal(raw["score"], "fast.score"),
    )


def _risk(value: object) -> RiskContext:
    raw = _mapping(value, "swing_context.risk")
    _exact_keys(raw, {
        "raw_executable_price_krw", "holding_session_number", "mark_complete",
        "entry_capacity_available", "hard_risk_reason", "target_hit", "stop_hit",
        "same_bar_ambiguous",
    }, "swing_context.risk")
    price = raw["raw_executable_price_krw"]
    if price is not None:
        price = _integer(price, "risk.raw_executable_price_krw", positive=True)
    hard_risk_reason = raw["hard_risk_reason"]
    if hard_risk_reason is not None:
        hard_risk_reason = _text(hard_risk_reason, "risk.hard_risk_reason")
    return RiskContext(
        price,
        _integer(raw["holding_session_number"], "risk.holding_session_number", positive=True),
        _boolean(raw["mark_complete"], "risk.mark_complete"),
        _boolean(raw["entry_capacity_available"], "risk.entry_capacity_available"),
        hard_risk_reason,
        _boolean(raw["target_hit"], "risk.target_hit"),
        _boolean(raw["stop_hit"], "risk.stop_hit"),
        _boolean(raw["same_bar_ambiguous"], "risk.same_bar_ambiguous"),
    )


def _position(value: object) -> PositionContext:
    raw = _mapping(value, "swing_context.position")
    _exact_keys(raw, {"active", "position_id", "symbol"}, "swing_context.position")
    active = _boolean(raw["active"], "position.active")
    position_id = raw["position_id"]
    symbol = raw["symbol"]
    if not isinstance(position_id, str) or not isinstance(symbol, str):
        raise ReplayDataError("position identities must be text")
    return PositionContext(active, position_id, symbol)


def _episode(value: object) -> EpisodeSnapshot:
    raw = _mapping(value, "swing_context.episode")
    _exact_keys(raw, {
        "state", "semantic_version", "consumed_event_ids", "admission_results",
    }, "swing_context.episode")
    consumed = raw["consumed_event_ids"]
    results = raw["admission_results"]
    if not isinstance(consumed, list) or not all(isinstance(item, str) and item.strip() for item in consumed):
        raise ReplayDataError("episode consumed event ids are invalid")
    if not isinstance(results, list):
        raise ReplayDataError("episode admission results are invalid")
    parsed_results: list[tuple[str, AdmissionResult]] = []
    for item in results:
        if not isinstance(item, list) or len(item) != 2:
            raise ReplayDataError("episode admission result tuple is invalid")
        parsed_results.append((_text(item[0], "episode admission event id"), AdmissionResult(item[1])))
    return EpisodeSnapshot(
        EpisodeState(raw["state"]),
        _text(raw["semantic_version"], "episode.semantic_version"),
        frozenset(consumed),
        tuple(parsed_results),
    )


def _rearm(value: object) -> EpisodeRearmEvidence | None:
    if value is None:
        return None
    raw = _mapping(value, "swing_context.rearm_evidence")
    _exact_keys(raw, {
        "flat", "slow_predicate_false", "completed_fast_false_bars",
        "cooldown_sessions", "signal_persistent",
    }, "swing_context.rearm_evidence")
    return EpisodeRearmEvidence(
        _boolean(raw["flat"], "rearm.flat"),
        _boolean(raw["slow_predicate_false"], "rearm.slow_predicate_false"),
        _integer(raw["completed_fast_false_bars"], "rearm.completed_fast_false_bars"),
        _integer(raw["cooldown_sessions"], "rearm.cooldown_sessions"),
        _boolean(raw["signal_persistent"], "rearm.signal_persistent"),
    )


def swing_context_from_replay_event(event: ReplayEvent) -> SwingEvaluationContext:
    """Restore a typed swing context from an explicit PIT wire payload.

    No feature is inferred from raw market bars here. A missing or future
    context is rejected so the same adapter can be used by offline replay and
    a later read-only staging source.
    """

    raw = _mapping(event.payload.get("swing_context"), "swing_context")
    _exact_keys(raw, {
        "schema_version", "strategy_policy", "episode_id", "slow", "fast", "risk",
        "position", "episode", "rearm_evidence",
    }, "swing_context")
    if raw["schema_version"] != _SWING_CONTEXT_SCHEMA:
        raise ReplayDataError("swing_context schema version is unsupported")
    policy_raw = _mapping(raw["strategy_policy"], "swing_context.strategy_policy")
    _exact_keys(policy_raw, {
        "semantic_version", "hard_risk_threshold_version",
        "minimum_holding_session", "maximum_holding_session",
    }, "swing_context.strategy_policy")
    try:
        policy = SwingStrategyPolicy(
            _text(policy_raw["semantic_version"], "policy.semantic_version"),
            _text(policy_raw["hard_risk_threshold_version"], "policy.hard_risk_threshold_version"),
            _integer(policy_raw["minimum_holding_session"], "policy.minimum_holding_session", positive=True),
            _integer(policy_raw["maximum_holding_session"], "policy.maximum_holding_session", positive=True),
        )
        episode_id = _text(raw["episode_id"], "swing_context.episode_id")
        slow = _slow(raw["slow"])
        fast = _fast(raw["fast"])
        context = SwingEvaluationContext(
            slow,
            fast,
            _risk(raw["risk"]),
            _position(raw["position"]),
            _episode(raw["episode"]),
            policy,
            episode_id,
            _rearm(raw["rearm_evidence"]),
        )
    except ReplayDataError:
        raise
    except (TypeError, ValueError) as error:
        raise ReplayDataError("swing_context typed contract is invalid") from error
    for name, instant in (
        ("slow.bar_closed_at", slow.bar_closed_at),
        ("slow.available_at", slow.available_at),
        ("slow.computed_at", slow.computed_at),
        ("fast.bar_closed_at", fast.bar_closed_at),
        ("fast.available_at", fast.available_at),
        ("fast.computed_at", fast.computed_at),
    ):
        if instant >= event.decision_at:
            raise ReplayDataError(f"{name} contains future data")
    return context


@dataclass(frozen=True)
class SwingContextReplayAdapter:
    """Callable adapter for replay/staging event streams."""

    strategy_semantics_version: str

    def __post_init__(self) -> None:
        if not self.strategy_semantics_version.strip():
            raise ValueError("strategy semantics version is required")

    def __call__(self, event: ReplayEvent) -> SwingEvaluationContext:
        context = swing_context_from_replay_event(event)
        if context.policy.semantic_version != self.strategy_semantics_version:
            raise ReplayDataError("strategy semantics version differs from adapter")
        return context

    def builder_for(
        self,
        event: ReplayEvent,
    ) -> Callable[[SwingShadowInput, Any], SwingEvaluationContext]:
        """Bind one PIT event to the candidate provider context-builder port."""

        def build(
            snapshot: SwingShadowInput,
            _hydration: Any,
        ) -> SwingEvaluationContext:
            if (
                snapshot.snapshot_id != event.source_snapshot_id
                or snapshot.decision_at != event.decision_at
            ):
                raise ReplayDataError(
                    "PIT event identity differs from the shadow input"
                )
            return self(event)

        return build

    def builder_for_events(
        self,
        events: Sequence[ReplayEvent],
    ) -> Callable[[SwingShadowInput, Any], SwingEvaluationContext]:
        """Bind a chronological event set to one provider context builder.

        A candidate provider is intentionally long-lived for one bounded replay
        run, while each event still gets its own PIT context. The lookup key is
        the immutable snapshot identity rather than an event list position so
        reordering or accidental cross-event use fails closed.
        """

        by_identity: dict[tuple[str, datetime], ReplayEvent] = {}
        for event in events:
            identity = (event.source_snapshot_id, event.decision_at)
            if identity in by_identity:
                raise ReplayDataError("PIT event snapshot identities must be unique")
            by_identity[identity] = event

        def build(
            snapshot: SwingShadowInput,
            hydration: Any,
        ) -> SwingEvaluationContext:
            event = by_identity.get((snapshot.snapshot_id, snapshot.decision_at))
            if event is None:
                raise ReplayDataError(
                    "PIT event identity is not present in the bound event set"
                )
            return self.builder_for(event)(snapshot, hydration)

        return build
