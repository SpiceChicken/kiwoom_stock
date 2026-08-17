"""Composition boundary for the pure swing strategy candidate evaluator."""

from __future__ import annotations

from typing import Any, Callable, Mapping

from kiwoom_stock.application.swing_decision import decide_swing
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.domain.swing_strategy import (
    SwingDecision,
    SwingEvaluationContext,
)


class SwingCandidateCompositionError(RuntimeError):
    """The candidate context cannot be safely composed for one shadow input."""


SwingCandidateContextFactory = Callable[
    [SwingShadowInput], SwingEvaluationContext
]


def candidate_decision_payload(
    decision: SwingDecision,
) -> Mapping[str, Any]:
    """Expose only the typed strategy decision needed by shadow evidence."""

    return {
        "decision_schema": "swing-decision-v1",
        "action": decision.action.value,
        "reason": decision.reason.value,
        "strategy_semantics_version": decision.strategy_semantics_version,
        "episode_id": decision.episode_id,
        "holding_session_number": decision.holding_session_number,
        "raw_executable_price_krw": decision.raw_executable_price_krw,
    }


def build_swing_candidate_evaluator(
    *,
    expected_strategy_semantics_version: str | None = None,
) -> Callable[[SwingShadowInput], Mapping[str, Any]]:
    """Build an evaluator that invokes the real pure ``evaluate_swing`` path.

    The context must already be assembled by the caller. This keeps state
    hydration and market acquisition outside the strategy while ensuring the
    runtime cannot silently substitute a legacy or unrelated evaluator.
    """

    if expected_strategy_semantics_version is not None and not expected_strategy_semantics_version.strip():
        raise ValueError("expected strategy semantics version is required")

    def evaluate_candidate(snapshot: SwingShadowInput) -> Mapping[str, Any]:
        context = snapshot.strategy_context
        if not isinstance(context, SwingEvaluationContext):
            raise SwingCandidateCompositionError(
                "swing candidate input lacks an immutable evaluation context"
            )
        if (
            expected_strategy_semantics_version is not None
            and context.policy.semantic_version != expected_strategy_semantics_version
        ):
            raise SwingCandidateCompositionError(
                "swing candidate strategy semantics version drifted"
            )
        decision = decide_swing(
            decision_at=snapshot.decision_at,
            slow=context.slow,
            fast=context.fast,
            risk=context.risk,
            position=context.position,
            episode=context.episode,
            policy=context.policy,
            episode_id=context.episode_id,
            rearm_evidence=context.rearm_evidence,
        )
        return candidate_decision_payload(decision)

    return evaluate_candidate
