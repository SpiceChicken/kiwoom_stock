"""Offline staging composition for CSV-backed swing PIT evidence."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping

from kiwoom_stock.application.swing_candidate_state import (
    open_read_only_swing_candidate_state_provider,
)
from kiwoom_stock.application.swing_pit_evidence import (
    SwingPITHashParityEvidence,
    run_swing_pit_hash_parity,
)
from kiwoom_stock.application.swing_replay import ChronologicalSplit
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.domain.accounting import AccountingPolicy
from kiwoom_stock.infrastructure.point_in_time_replay import (
    CsvPITReplaySource,
    SwingContextReplayAdapter,
)


def run_csv_swing_staging_hash_parity(
    *,
    source: CsvPITReplaySource,
    strategy_semantics_version: str,
    candidate_enabled: bool,
    candidate_database_path: str | Path | None = None,
    candidate_portfolio_id: str | None = None,
    candidate_episode_id: str | None = None,
    accounting_policy: AccountingPolicy | None = None,
    legacy_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None = None,
    chronological_split: ChronologicalSplit | None = None,
) -> SwingPITHashParityEvidence:
    """Run one CSV source through the complete offline staging composition.

    The candidate state database is opened read-only for each parity run. The
    context builder is bound to the event set by immutable snapshot identity;
    no feature is inferred from the CSV path or from a current market lookup.
    """

    adapter = SwingContextReplayAdapter(strategy_semantics_version)
    if candidate_enabled:
        if (
            candidate_database_path is None
            or candidate_portfolio_id is None
            or candidate_episode_id is None
            or accounting_policy is None
        ):
            raise ValueError(
                "enabled CSV staging requires candidate database, portfolio, episode, and policy"
            )
        candidate_path = Path(candidate_database_path)

        def provider_factory(events):
            return open_read_only_swing_candidate_state_provider(
                candidate_path,
                portfolio_id=candidate_portfolio_id,
                episode_id=candidate_episode_id,
                accounting_policy=accounting_policy,
                context_builder=adapter.builder_for_events(events),
            )

        context_provider_factory = provider_factory
        evidence_database_path: str | None = str(candidate_path)
        evidence_portfolio_id: str | None = candidate_portfolio_id
    else:
        if any(
            value is not None
            for value in (
                candidate_database_path,
                candidate_portfolio_id,
                candidate_episode_id,
                accounting_policy,
            )
        ):
            raise ValueError("disabled CSV staging cannot receive candidate dependencies")
        context_provider_factory = None
        evidence_database_path = None
        evidence_portfolio_id = None

    return run_swing_pit_hash_parity(
        events=source.events,
        dataset_id=source.dataset_id,
        strategy_semantics_version=strategy_semantics_version,
        candidate_enabled=candidate_enabled,
        context_provider_factory=context_provider_factory,
        candidate_database_path=evidence_database_path,
        candidate_portfolio_id=evidence_portfolio_id,
        legacy_evaluator=legacy_evaluator,
        chronological_split=chronological_split,
    )
