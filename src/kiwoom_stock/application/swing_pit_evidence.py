"""Deterministic offline PIT replay evidence for the swing candidate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, Sequence

from kiwoom_stock.application.swing_candidate import (
    build_swing_candidate_evaluator,
)
from kiwoom_stock.application.swing_replay import (
    ChronologicalSplit,
    ReplayDataError,
    ReplayEvent,
    ReplayResult,
    run_replay,
)
from kiwoom_stock.application.swing_shadow import (
    SwingShadowInput,
    run_same_input_shadow,
)


class SwingPITContextProvider(Protocol):
    def __call__(self, snapshot: SwingShadowInput) -> Any:
        """Hydrate and return the immutable strategy context for one snapshot."""

    def close(self) -> None:
        """Release the read-only candidate state owner."""


SwingPITContextProviderFactory = Callable[
    [tuple[ReplayEvent, ...]], SwingPITContextProvider
]


@dataclass(frozen=True)
class SwingPITHashParityEvidence:
    """Hash parity and side-effect evidence for two identical offline runs."""

    first_run: ReplayResult
    second_run: ReplayResult
    candidate_enabled: bool
    candidate_database_path: str | None = None
    candidate_portfolio_id: str | None = None
    candidate_call_count_first: int = 0
    candidate_call_count_second: int = 0
    side_effects: bool = False

    def __post_init__(self) -> None:
        if self.first_run.input_hash != self.second_run.input_hash:
            raise ReplayDataError("PIT replay input hash parity failed")
        if self.first_run.output_hash != self.second_run.output_hash:
            raise ReplayDataError("PIT replay output hash parity failed")
        if self.first_run.decision_ids != self.second_run.decision_ids:
            raise ReplayDataError("PIT replay decision identity parity failed")
        first_selection = self.first_run.selection_receipt
        second_selection = self.second_run.selection_receipt
        if (first_selection is None) != (second_selection is None):
            raise ReplayDataError("PIT replay selection receipt parity failed")
        if (
            first_selection is not None
            and second_selection is not None
            and first_selection.selection_hash != second_selection.selection_hash
        ):
            raise ReplayDataError("PIT replay selection hash parity failed")
        if self.first_run.side_effects or self.second_run.side_effects or self.side_effects:
            raise ReplayDataError("PIT replay evidence contains side effects")
        if self.candidate_enabled != (
            self.candidate_database_path is not None
            and self.candidate_portfolio_id is not None
        ):
            raise ReplayDataError("candidate evidence identity is incomplete")
        if self.candidate_call_count_first < 0 or self.candidate_call_count_second < 0:
            raise ReplayDataError("candidate call counts must be non-negative")
        expected = self.first_run.decision_count if self.candidate_enabled else 0
        if (
            self.candidate_call_count_first != expected
            or self.candidate_call_count_second != expected
        ):
            raise ReplayDataError("candidate call count evidence is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "first_run": self.first_run.to_dict(),
            "second_run": self.second_run.to_dict(),
            "input_hash_parity": self.first_run.input_hash == self.second_run.input_hash,
            "output_hash_parity": self.first_run.output_hash == self.second_run.output_hash,
            "candidate_enabled": self.candidate_enabled,
            "candidate_database_path": self.candidate_database_path,
            "candidate_portfolio_id": self.candidate_portfolio_id,
            "candidate_call_count_first": self.candidate_call_count_first,
            "candidate_call_count_second": self.candidate_call_count_second,
            "side_effects": self.side_effects,
        }


def _close_provider(provider: SwingPITContextProvider | None) -> None:
    if provider is None:
        return
    provider.close()


def _run_once(
    *,
    events: tuple[ReplayEvent, ...],
    dataset_id: str,
    strategy_semantics_version: str,
    candidate_enabled: bool,
    context_provider_factory: SwingPITContextProviderFactory | None,
    candidate_database_path: str | None,
    candidate_portfolio_id: str | None,
    legacy_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None,
    chronological_split: ChronologicalSplit | None,
) -> tuple[ReplayResult, int]:
    provider: SwingPITContextProvider | None = None
    candidate_call_count = 0
    if candidate_enabled:
        if context_provider_factory is None:
            raise ReplayDataError("enabled PIT replay requires a context provider factory")
        provider = context_provider_factory(events)
    elif context_provider_factory is not None:
        raise ReplayDataError("disabled PIT replay cannot receive a context provider")

    candidate_evaluator = build_swing_candidate_evaluator(
        expected_strategy_semantics_version=strategy_semantics_version,
    )

    def evaluate(event: ReplayEvent) -> Mapping[str, Any]:
        snapshot = SwingShadowInput(
            event.source_snapshot_id,
            event.decision_at,
            event.payload,
        )
        if provider is not None:
            context = provider(snapshot)
            snapshot = SwingShadowInput(
                snapshot.snapshot_id,
                snapshot.decision_at,
                snapshot.payload,
                context,
            )

        def guarded_candidate(input_snapshot: SwingShadowInput) -> Mapping[str, Any]:
            nonlocal candidate_call_count
            candidate_call_count += 1
            if not candidate_enabled:
                raise ReplayDataError("disabled candidate evaluator was called")
            return candidate_evaluator(input_snapshot)

        shadow = run_same_input_shadow(
            snapshot=snapshot,
            legacy_evaluator=legacy_evaluator,
            candidate_evaluator=guarded_candidate,
            candidate_enabled=candidate_enabled,
            candidate_database_path=candidate_database_path,
            candidate_portfolio_id=candidate_portfolio_id,
        )
        return shadow.evidence.to_safe_dict()

    try:
        return (
            run_replay(
                events=events,
                dataset_id=dataset_id,
                strategy_semantics_version=strategy_semantics_version,
                evaluator=evaluate,
                split=chronological_split,
            ),
            candidate_call_count,
        )
    finally:
        _close_provider(provider)


def run_swing_pit_hash_parity(
    *,
    events: Sequence[ReplayEvent],
    dataset_id: str,
    strategy_semantics_version: str,
    candidate_enabled: bool,
    context_provider_factory: SwingPITContextProviderFactory | None = None,
    candidate_database_path: str | None = None,
    candidate_portfolio_id: str | None = None,
    legacy_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None = None,
    chronological_split: ChronologicalSplit | None = None,
) -> SwingPITHashParityEvidence:
    """Run the same offline PIT event set twice and require exact parity."""

    normalized_events = tuple(events)
    if not normalized_events:
        raise ReplayDataError("PIT replay requires at least one event")
    if candidate_enabled != (
        candidate_database_path is not None and candidate_portfolio_id is not None
    ):
        raise ReplayDataError("candidate identity must match enabled state")
    first, first_calls = _run_once(
        events=normalized_events,
        dataset_id=dataset_id,
        strategy_semantics_version=strategy_semantics_version,
        candidate_enabled=candidate_enabled,
        context_provider_factory=context_provider_factory,
        candidate_database_path=candidate_database_path,
        candidate_portfolio_id=candidate_portfolio_id,
        legacy_evaluator=legacy_evaluator,
        chronological_split=chronological_split,
    )
    second, second_calls = _run_once(
        events=normalized_events,
        dataset_id=dataset_id,
        strategy_semantics_version=strategy_semantics_version,
        candidate_enabled=candidate_enabled,
        context_provider_factory=context_provider_factory,
        candidate_database_path=candidate_database_path,
        candidate_portfolio_id=candidate_portfolio_id,
        legacy_evaluator=legacy_evaluator,
        chronological_split=chronological_split,
    )
    return SwingPITHashParityEvidence(
        first,
        second,
        candidate_enabled,
        candidate_database_path,
        candidate_portfolio_id,
        first_calls,
        second_calls,
    )
