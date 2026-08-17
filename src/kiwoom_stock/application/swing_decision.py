"""Application boundary for pure swing decision evaluation."""

from kiwoom_stock.domain.swing_strategy import (
    FastContext,
    PositionContext,
    RiskContext,
    SlowContext,
    SwingDecision,
    SwingStrategyPolicy,
    evaluate_swing,
)
from kiwoom_stock.domain.swing_contracts import EpisodeRearmEvidence, EpisodeSnapshot
from datetime import datetime


def decide_swing(
    *,
    decision_at: datetime,
    slow: SlowContext,
    fast: FastContext,
    risk: RiskContext,
    position: PositionContext,
    episode: EpisodeSnapshot,
    policy: SwingStrategyPolicy,
    episode_id: str,
    rearm_evidence: EpisodeRearmEvidence | None = None,
) -> SwingDecision:
    return evaluate_swing(
        decision_at=decision_at,
        slow=slow,
        fast=fast,
        risk=risk,
        position=position,
        episode=episode,
        policy=policy,
        episode_id=episode_id,
        rearm_evidence=rearm_evidence,
    )
