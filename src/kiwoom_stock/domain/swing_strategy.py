"""Pure dual-timescale swing strategy contracts and evaluator."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from kiwoom_stock.domain.swing_contracts import (
    EpisodeRearmEvidence,
    EpisodeSnapshot,
    EpisodeState,
    InsufficientDataError,
    validate_hard_risk,
    validate_session_candidate,
)
from kiwoom_stock.utils.market_cal import KST


class SwingAction(StrEnum):
    HOLD = "HOLD"
    ADMIT_ENTRY = "ADMIT_ENTRY"
    EXIT = "EXIT"
    REARM = "REARM"


class SwingDecisionReason(StrEnum):
    ENTRY_SIGNAL = "ENTRY_SIGNAL"
    HARD_RISK = "HARD_RISK"
    STOP = "STOP"
    TARGET = "TARGET"
    THESIS_INVALIDATION = "THESIS_INVALIDATION"
    TIME_EXIT = "TIME_EXIT"
    INCOMPLETE_MARK = "INCOMPLETE_MARK"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    EPISODE_NOT_ARMED = "EPISODE_NOT_ARMED"
    TERMINAL_EPISODE = "TERMINAL_EPISODE"
    NO_ENTRY_SIGNAL = "NO_ENTRY_SIGNAL"
    HOLDING_MINIMUM = "HOLDING_MINIMUM"
    REARM_EVIDENCE = "REARM_EVIDENCE"


def _require_decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise InsufficientDataError(f"{name} must be a finite Decimal")
    return value


def _require_aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InsufficientDataError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class SwingStrategyPolicy:
    semantic_version: str
    hard_risk_threshold_version: str
    minimum_holding_session: int = 2
    maximum_holding_session: int = 20

    def __post_init__(self) -> None:
        if not self.semantic_version.strip() or not self.hard_risk_threshold_version.strip():
            raise ValueError("strategy and hard-risk versions are required")
        if self.minimum_holding_session < 2 or self.maximum_holding_session < self.minimum_holding_session:
            raise ValueError("holding session boundaries are invalid")


@dataclass(frozen=True)
class SlowContext:
    session_date: date
    bar_closed_at: datetime
    available_at: datetime
    computed_at: datetime
    source_snapshot_id: str
    strategy_semantics_version: str
    lookback_sessions: int
    warmup_complete: bool
    thesis_valid: bool
    entry_eligible: bool
    score: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or not self.source_snapshot_id.strip():
            raise ValueError("slow context identity is invalid")
        for value, name in ((self.bar_closed_at, "bar_closed_at"), (self.available_at, "available_at"), (self.computed_at, "computed_at")):
            _require_aware(value, name)
        if self.computed_at < self.available_at or self.lookback_sessions <= 0:
            raise ValueError("slow context timing or lookback is invalid")
        if not isinstance(self.warmup_complete, bool) or not isinstance(self.thesis_valid, bool) or not isinstance(self.entry_eligible, bool):
            raise ValueError("slow context flags are invalid")
        _require_decimal(self.score, "slow score")


@dataclass(frozen=True)
class FastContext:
    bar_id: str
    bar_closed_at: datetime
    available_at: datetime
    computed_at: datetime
    source_snapshot_id: str
    strategy_semantics_version: str
    trigger_rising: bool
    entry_eligible: bool
    score: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.bar_id.strip() or not self.source_snapshot_id.strip():
            raise ValueError("fast context identity is invalid")
        for value, name in ((self.bar_closed_at, "bar_closed_at"), (self.available_at, "available_at"), (self.computed_at, "computed_at")):
            _require_aware(value, name)
        if self.computed_at < self.available_at or not isinstance(self.trigger_rising, bool) or not isinstance(self.entry_eligible, bool):
            raise ValueError("fast context timing or flags are invalid")
        _require_decimal(self.score, "fast score")


@dataclass(frozen=True)
class RiskContext:
    raw_executable_price_krw: int | None
    holding_session_number: int
    mark_complete: bool
    entry_capacity_available: bool
    hard_risk_reason: str | None = None
    target_hit: bool = False
    stop_hit: bool = False
    same_bar_ambiguous: bool = False

    def __post_init__(self) -> None:
        if self.raw_executable_price_krw is not None and (isinstance(self.raw_executable_price_krw, bool) or self.raw_executable_price_krw <= 0):
            raise ValueError("raw executable price must be positive KRW")
        if isinstance(self.holding_session_number, bool) or self.holding_session_number <= 0:
            raise ValueError("holding session number must be positive")
        if not isinstance(self.mark_complete, bool) or not isinstance(self.entry_capacity_available, bool):
            raise ValueError("risk flags are invalid")
        if not all(isinstance(value, bool) for value in (self.target_hit, self.stop_hit, self.same_bar_ambiguous)):
            raise ValueError("risk hit flags are invalid")


@dataclass(frozen=True)
class PositionContext:
    active: bool
    position_id: str = ""
    symbol: str = ""

    def __post_init__(self) -> None:
        if self.active and (not self.position_id.strip() or not self.symbol.strip()):
            raise ValueError("active position identity is required")


@dataclass(frozen=True)
class SwingEvaluationContext:
    """Complete immutable input required by the swing strategy evaluator.

    The context is deliberately assembled outside the pure strategy function.
    A runtime may source it from an isolated candidate state provider, while
    the evaluator itself remains unable to read clocks, databases, brokers, or
    notifiers.
    """

    slow: SlowContext
    fast: FastContext
    risk: RiskContext
    position: PositionContext
    episode: EpisodeSnapshot
    policy: SwingStrategyPolicy
    episode_id: str
    rearm_evidence: EpisodeRearmEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.slow, SlowContext):
            raise TypeError("swing evaluation slow context is invalid")
        if not isinstance(self.fast, FastContext):
            raise TypeError("swing evaluation fast context is invalid")
        if not isinstance(self.risk, RiskContext):
            raise TypeError("swing evaluation risk context is invalid")
        if not isinstance(self.position, PositionContext):
            raise TypeError("swing evaluation position context is invalid")
        if not isinstance(self.episode, EpisodeSnapshot):
            raise TypeError("swing evaluation episode snapshot is invalid")
        if not isinstance(self.policy, SwingStrategyPolicy):
            raise TypeError("swing evaluation strategy policy is invalid")
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise TypeError("swing evaluation episode identity is invalid")
        if self.rearm_evidence is not None and not isinstance(
            self.rearm_evidence, EpisodeRearmEvidence
        ):
            raise TypeError("swing evaluation rearm evidence is invalid")


@dataclass(frozen=True)
class SwingDecision:
    action: SwingAction
    reason: SwingDecisionReason
    strategy_semantics_version: str
    episode_id: str
    holding_session_number: int
    raw_executable_price_krw: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.episode_id, str) or not self.episode_id.strip():
            raise TypeError("swing decision episode identity is invalid")


def _validate_as_of(
    slow: SlowContext,
    fast: FastContext,
    decision_at: datetime,
    policy: SwingStrategyPolicy,
) -> None:
    _require_aware(decision_at, "decision_at")
    decision_session = decision_at.astimezone(KST).date()
    if slow.strategy_semantics_version != policy.semantic_version or fast.strategy_semantics_version != policy.semantic_version:
        raise InsufficientDataError("strategy semantic version is not current")
    if slow.session_date >= decision_session:
        raise InsufficientDataError("slow daily context is not previous-close data")
    for context_name, values in (
        ("slow", (slow.bar_closed_at, slow.available_at, slow.computed_at)),
        ("fast", (fast.bar_closed_at, fast.available_at, fast.computed_at)),
    ):
        if any(value >= decision_at for value in values):
            raise InsufficientDataError(f"{context_name} context contains future data")


def evaluate_swing(
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
    """Evaluate one immutable input set; never reads clock, DB, broker, or notifier."""

    _validate_as_of(slow, fast, decision_at, policy)
    if not isinstance(episode_id, str):
        raise InsufficientDataError("episode_id must be text")
    if episode.semantic_version != policy.semantic_version:
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.TERMINAL_EPISODE, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    if position.active:
        if risk.hard_risk_reason is not None:
            validate_hard_risk(risk.hard_risk_reason, risk.raw_executable_price_krw, policy.hard_risk_threshold_version)
            return SwingDecision(SwingAction.EXIT, SwingDecisionReason.HARD_RISK, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if not risk.mark_complete:
            return SwingDecision(SwingAction.HOLD, SwingDecisionReason.INCOMPLETE_MARK, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if risk.stop_hit or (risk.target_hit and risk.stop_hit):
            if risk.holding_session_number >= policy.minimum_holding_session:
                return SwingDecision(SwingAction.EXIT, SwingDecisionReason.STOP, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if risk.target_hit and risk.holding_session_number >= policy.minimum_holding_session:
            return SwingDecision(SwingAction.EXIT, SwingDecisionReason.TARGET, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if risk.holding_session_number >= policy.maximum_holding_session:
            validate_session_candidate(risk.holding_session_number, time_exit=True)
            return SwingDecision(SwingAction.EXIT, SwingDecisionReason.TIME_EXIT, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if not slow.thesis_valid and risk.holding_session_number >= policy.minimum_holding_session:
            return SwingDecision(SwingAction.EXIT, SwingDecisionReason.THESIS_INVALIDATION, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        if risk.holding_session_number < policy.minimum_holding_session:
            return SwingDecision(SwingAction.HOLD, SwingDecisionReason.HOLDING_MINIMUM, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.NO_ENTRY_SIGNAL, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)

    if episode.state is EpisodeState.TERMINAL:
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.TERMINAL_EPISODE, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    if episode.state is EpisodeState.COOLDOWN:
        if rearm_evidence is not None and rearm_evidence.eligible:
            return SwingDecision(SwingAction.REARM, SwingDecisionReason.REARM_EVIDENCE, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.REARM_EVIDENCE, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    if episode.state is not EpisodeState.ARMED:
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.EPISODE_NOT_ARMED, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    if not risk.entry_capacity_available or not risk.mark_complete:
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.INCOMPLETE_MARK, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    if risk.raw_executable_price_krw is None:
        return SwingDecision(SwingAction.HOLD, SwingDecisionReason.INSUFFICIENT_DATA, policy.semantic_version, episode_id, risk.holding_session_number)
    if slow.warmup_complete and slow.entry_eligible and fast.entry_eligible and fast.trigger_rising:
        return SwingDecision(SwingAction.ADMIT_ENTRY, SwingDecisionReason.ENTRY_SIGNAL, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
    return SwingDecision(SwingAction.HOLD, SwingDecisionReason.NO_ENTRY_SIGNAL, policy.semantic_version, episode_id, risk.holding_session_number, risk.raw_executable_price_krw)
