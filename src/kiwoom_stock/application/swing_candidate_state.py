"""Read-only isolated candidate state hydration for swing shadow evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from kiwoom_stock.application.ports import (
    SwingEpisodeHydration,
    SwingHydration,
    SwingLedgerPort,
)
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy
from kiwoom_stock.domain.swing_strategy import (
    SwingEvaluationContext,
)


class SwingCandidateStateError(RuntimeError):
    """Candidate state is missing, inconsistent, or outside its identity."""


@dataclass(frozen=True)
class SwingCandidateHydration:
    """Verified read-only portfolio and episode state for one evaluation."""

    portfolio: SwingHydration
    episode: SwingEpisodeHydration

    def __post_init__(self) -> None:
        if not isinstance(self.portfolio, SwingHydration):
            raise TypeError("candidate portfolio hydration is invalid")
        if not isinstance(self.episode, SwingEpisodeHydration):
            raise TypeError("candidate episode hydration is invalid")
        if not self.portfolio.portfolio_id.strip() or not self.episode.episode_id.strip():
            raise SwingCandidateStateError("candidate hydration identities are required")


class SwingCandidateContextBuilder(Protocol):
    def __call__(
        self,
        snapshot: SwingShadowInput,
        hydration: SwingCandidateHydration,
    ) -> SwingEvaluationContext:
        """Build only the non-persistent strategy contexts from PIT input."""


class SwingCandidateStateProvider:
    """Hydrate candidate state and delegate feature assembly without writes."""

    def __init__(
        self,
        ledger: SwingLedgerPort,
        *,
        portfolio_id: str,
        episode_id: str,
        context_builder: SwingCandidateContextBuilder,
    ) -> None:
        if not portfolio_id.strip() or not episode_id.strip():
            raise ValueError("candidate portfolio and episode identities are required")
        self._ledger = ledger
        self._portfolio_id = portfolio_id
        self._episode_id = episode_id
        self._context_builder = context_builder

    def __call__(self, snapshot: SwingShadowInput) -> SwingEvaluationContext:
        if not isinstance(snapshot, SwingShadowInput):
            raise TypeError("candidate state provider requires SwingShadowInput")
        try:
            hydration = SwingCandidateHydration(
                self._ledger.hydrate(portfolio_id=self._portfolio_id),
                self._ledger.hydrate_episode(episode_id=self._episode_id),
            )
            context = self._context_builder(snapshot, hydration)
        except SwingCandidateStateError:
            raise
        except Exception as error:
            raise SwingCandidateStateError(
                "candidate state hydration or context assembly failed"
            ) from error
        if not isinstance(context, SwingEvaluationContext):
            raise SwingCandidateStateError("context builder returned an invalid context")
        if context.episode != hydration.episode.snapshot:
            raise SwingCandidateStateError(
                "strategy context episode is not the hydrated candidate episode"
            )
        if context.episode_id != hydration.episode.episode_id:
            raise SwingCandidateStateError(
                "strategy context episode identity differs from hydrated candidate episode"
            )
        if context.policy.semantic_version != hydration.episode.snapshot.semantic_version:
            raise SwingCandidateStateError(
                "strategy policy version differs from hydrated candidate episode"
            )
        lots = hydration.portfolio.state.lots
        if len(lots) > 1:
            raise SwingCandidateStateError(
                "candidate state contains more than one active lot"
            )
        lot = lots[0] if lots else None
        if lot is None:
            if context.position.active:
                raise SwingCandidateStateError(
                    "strategy context reports an active position without a hydrated lot"
                )
        elif (
            not context.position.active
            or context.position.position_id != lot.position_id
            or context.position.symbol != lot.symbol
        ):
            raise SwingCandidateStateError(
                "strategy position context differs from hydrated candidate lot"
            )
        return context

    def close(self) -> None:
        self._ledger.close()


def open_read_only_swing_candidate_state_provider(
    database_path: str | Path,
    *,
    portfolio_id: str,
    episode_id: str,
    accounting_policy: AccountingPolicy,
    context_builder: SwingCandidateContextBuilder,
) -> SwingCandidateStateProvider:
    """Open the isolated candidate DB read-only and return its state provider."""

    ledger = SwingLedger(
        database_path,
        portfolio_id=portfolio_id,
        policy=accounting_policy,
        read_only=True,
    )
    return SwingCandidateStateProvider(
        ledger,
        portfolio_id=portfolio_id,
        episode_id=episode_id,
        context_builder=context_builder,
    )
