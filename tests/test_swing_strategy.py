from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from kiwoom_stock.domain.swing_contracts import (
    EpisodeRearmEvidence,
    EpisodeSnapshot,
    EpisodeState,
    InsufficientDataError,
)
from kiwoom_stock.domain.swing_strategy import (
    FastContext,
    PositionContext,
    RiskContext,
    SlowContext,
    SwingAction,
    SwingDecisionReason,
    SwingStrategyPolicy,
    evaluate_swing,
)


UTC = timezone.utc
DECISION = datetime(2026, 8, 18, 1, tzinfo=UTC)
POLICY = SwingStrategyPolicy("swing-v1", "risk-v1")


def contexts(*, slow_session=None, fast_closed=None, thesis=True, entry=True, trigger=True, warmup=True):
    slow = SlowContext(
        slow_session or datetime(2026, 8, 14).date(),
        DECISION - timedelta(days=1),
        DECISION - timedelta(days=1),
        DECISION - timedelta(days=1),
        "slow-1",
        "swing-v1",
        20,
        warmup,
        thesis,
        entry,
        Decimal("1.0"),
    )
    fast = FastContext(
        "fast-1",
        fast_closed or DECISION - timedelta(minutes=1),
        DECISION - timedelta(minutes=1),
        DECISION - timedelta(minutes=1),
        "fast-1",
        "swing-v1",
        trigger,
        entry,
        Decimal("1.0"),
    )
    return slow, fast


def evaluate(*, active=False, holding=1, risk=None, episode=None, rearm_evidence=None, **kwargs):
    slow, fast = contexts(**kwargs)
    return evaluate_swing(
        decision_at=DECISION,
        slow=slow,
        fast=fast,
        risk=risk or RiskContext(70_000, holding, True, True),
        position=PositionContext(active, "pos-1" if active else "", "005930" if active else ""),
        episode=episode or EpisodeSnapshot(EpisodeState.ARMED, "swing-v1"),
        policy=POLICY,
        episode_id="episode-1",
        rearm_evidence=rearm_evidence,
    )


def test_entry_requires_previous_close_and_completed_fast_trigger():
    result = evaluate()
    assert result.action is SwingAction.ADMIT_ENTRY
    assert result.reason is SwingDecisionReason.ENTRY_SIGNAL
    with pytest.raises(InsufficientDataError):
        evaluate(slow_session=DECISION.date())
    with pytest.raises(InsufficientDataError):
        evaluate(fast_closed=DECISION + timedelta(seconds=1))
    with pytest.raises(InsufficientDataError):
        slow, fast = contexts()
        equal_time_fast = FastContext(
            fast.bar_id,
            fast.bar_closed_at,
            DECISION,
            DECISION + timedelta(seconds=1),
            fast.source_snapshot_id,
            fast.strategy_semantics_version,
            fast.trigger_rising,
            fast.entry_eligible,
            fast.score,
        )
        evaluate_swing(
            decision_at=DECISION,
            slow=slow,
            fast=equal_time_fast,
            risk=RiskContext(70_000, 1, True, True),
            position=PositionContext(False),
            episode=EpisodeSnapshot(EpisodeState.ARMED, "swing-v1"),
            policy=POLICY,
            episode_id="episode-1",
        )


def test_warmup_or_signal_gap_holds_without_entry():
    assert evaluate(warmup=False).action is SwingAction.HOLD
    assert evaluate(trigger=False).reason is SwingDecisionReason.NO_ENTRY_SIGNAL
    assert evaluate(entry=False).reason is SwingDecisionReason.NO_ENTRY_SIGNAL
    assert evaluate(risk=RiskContext(None, 1, True, True)).reason is SwingDecisionReason.INSUFFICIENT_DATA


def test_minimum_holding_blocks_thesis_exit_but_hard_risk_is_exception():
    assert evaluate(active=True, holding=1, thesis=False).reason is SwingDecisionReason.HOLDING_MINIMUM
    assert evaluate(active=True, holding=2, thesis=False).reason is SwingDecisionReason.THESIS_INVALIDATION
    result = evaluate(
        active=True,
        holding=1,
        risk=RiskContext(60_000, 1, True, True, "CATASTROPHIC_PRICE_RISK"),
    )
    assert result.action is SwingAction.EXIT
    assert result.reason is SwingDecisionReason.HARD_RISK


def test_time_exit_and_conservative_same_bar_stop_precedence():
    assert evaluate(active=True, holding=20).reason is SwingDecisionReason.TIME_EXIT
    result = evaluate(active=True, holding=2, risk=RiskContext(60_000, 2, True, True, target_hit=True, stop_hit=True))
    assert result.action is SwingAction.EXIT
    assert result.reason is SwingDecisionReason.STOP


def test_incomplete_mark_holds_active_position_and_does_not_synthesize_exit():
    result = evaluate(active=True, risk=RiskContext(60_000, 1, False, True))
    assert result.action is SwingAction.HOLD
    assert result.reason is SwingDecisionReason.INCOMPLETE_MARK
    assert evaluate(risk=RiskContext(None, 1, False, True)).reason is SwingDecisionReason.INCOMPLETE_MARK


def test_cooldown_rearm_requires_nonpersistent_evidence():
    episode = EpisodeSnapshot(EpisodeState.COOLDOWN, "swing-v1")
    eligible = EpisodeRearmEvidence(True, True, 2, 1, False)
    result = evaluate(episode=episode)
    assert result.action is SwingAction.HOLD
    result = evaluate(episode=episode, rearm_evidence=eligible)
    assert result.action is SwingAction.REARM


def test_terminal_or_version_mismatch_never_admits():
    terminal = EpisodeSnapshot(EpisodeState.TERMINAL, "swing-v1")
    assert evaluate(episode=terminal).reason is SwingDecisionReason.TERMINAL_EPISODE
    mismatch = EpisodeSnapshot(EpisodeState.ARMED, "old")
    assert evaluate(episode=mismatch).reason is SwingDecisionReason.TERMINAL_EPISODE
