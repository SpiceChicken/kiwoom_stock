"""Strict, immutable contracts for the swing accounting domain."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Final


class ContractError(ValueError):
    """Invalid or incomplete domain evidence."""


class TemporalCausalityError(ContractError):
    pass


class InsufficientDataError(ContractError):
    pass


class MarkQuality(StrEnum):
    OFFICIAL_CLOSE = "OFFICIAL_CLOSE"
    PROVISIONAL_LAST_VALID_REGULAR = "PROVISIONAL_LAST_VALID_REGULAR"
    SUSPENDED_CARRY_FORWARD = "SUSPENDED_CARRY_FORWARD"
    MISSING = "MISSING"


class MarkCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class SessionEvidenceError(ContractError):
    pass


class CorporateActionKind(StrEnum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"


class HardRiskReason(StrEnum):
    CATASTROPHIC_PRICE_RISK = "CATASTROPHIC_PRICE_RISK"
    PORTFOLIO_RISK_LIMIT = "PORTFOLIO_RISK_LIMIT"


class AdmissionResult(StrEnum):
    FILLED = "FILLED"
    UNFILLED = "UNFILLED"
    REJECTED = "REJECTED"


class EpisodeState(StrEnum):
    ARMED = "ARMED"
    ACTIVE = "ACTIVE"
    CONSUMED = "CONSUMED"
    COOLDOWN = "COOLDOWN"
    TERMINAL = "TERMINAL"


def _identity(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{name} must be a non-empty identity")
    return value


def require_aware(value: datetime, name: str = "datetime") -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ContractError(f"{name} must be timezone-aware")
    return value


def require_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ContractError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class Mark:
    session_date: date
    price_krw: int | None
    quality: MarkQuality
    source_id: str
    available_at: datetime
    computed_at: datetime
    revision: int
    supersedes_id: str | None = None
    portfolio_id: str = ""
    position_id: str = ""
    symbol: str = ""
    session_evidence: SessionMarkEvidence | None = None

    def __post_init__(self) -> None:
        for value, name in ((self.portfolio_id, "portfolio_id"), (self.position_id, "position_id"),
                            (self.symbol, "symbol"), (self.source_id, "source_id")):
            _identity(value, name)
        if not isinstance(self.quality, MarkQuality):
            raise ContractError("mark quality must be MarkQuality")
        require_aware(self.available_at, "available_at")
        require_aware(self.computed_at, "computed_at")
        if self.computed_at < self.available_at:
            raise TemporalCausalityError("computed_at cannot precede available_at")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ContractError("mark revision must be positive")
        if self.revision == 1 and self.supersedes_id is not None:
            raise ContractError("revision 1 cannot supersede another mark")
        if self.revision > 1 and not self.supersedes_id:
            raise ContractError("revision > 1 requires supersedes_id")
        if self.quality is MarkQuality.MISSING:
            if self.price_krw is not None:
                raise ContractError("MISSING mark cannot contain a price")
        elif isinstance(self.price_krw, bool) or not isinstance(self.price_krw, int) or self.price_krw <= 0:
            raise ContractError("complete mark price must be a positive KRW integer")
        if self.session_evidence is not None and not isinstance(self.session_evidence, SessionMarkEvidence):
            raise ContractError("mark session evidence must be typed")

    @property
    def mark_id(self) -> str:
        return ":".join(
            (
                self.portfolio_id,
                self.position_id,
                self.symbol,
                str(self.session_date),
                self.source_id,
                str(self.revision),
            )
        )

    @property
    def completeness(self) -> MarkCompleteness:
        return MarkCompleteness.COMPLETE if self.quality is MarkQuality.OFFICIAL_CLOSE else MarkCompleteness.INCOMPLETE

    @property
    def permits_new_entry(self) -> bool:
        return self.completeness is MarkCompleteness.COMPLETE

    def stale_age(self, current_session_ordinal: int, mark_session_ordinal: int) -> int:
        if current_session_ordinal < mark_session_ordinal:
            raise SessionEvidenceError("mark session is after current session")
        return current_session_ordinal - mark_session_ordinal


@dataclass(frozen=True)
class CorporateAction:
    action_id: str
    symbol: str
    effective_session: date
    available_at: datetime | None
    kind: CorporateActionKind
    quantity_numerator: int = 1
    quantity_denominator: int = 1
    cash_per_share_krw: int = 0
    known: bool = True
    portfolio_id: str = ""
    position_id: str = ""

    def __post_init__(self) -> None:
        for value, name in ((self.action_id, "action_id"), (self.symbol, "symbol"),
                            (self.portfolio_id, "portfolio_id"), (self.position_id, "position_id")):
            _identity(value, name)
        if not isinstance(self.kind, CorporateActionKind):
            raise ContractError("corporate action kind must be CorporateActionKind")
        if self.available_at is not None:
            require_aware(self.available_at, "available_at")
        require_positive_int(self.quantity_numerator, "quantity_numerator")
        require_positive_int(self.quantity_denominator, "quantity_denominator")
        if isinstance(
                self.cash_per_share_krw,
                bool) or not isinstance(
                self.cash_per_share_krw,
                int) or self.cash_per_share_krw < 0:
            raise ContractError("cash_per_share_krw must be a non-negative KRW integer")

    def usable_at(self, decision_at: datetime, session_date: date, *, decision_session: date | None = None) -> bool:
        require_aware(decision_at, "decision_at")
        if decision_session is None:
            decision_session = decision_at.date()
        if not isinstance(session_date, date) or not isinstance(decision_session, date):
            raise ContractError("corporate-action sessions must be dates")
        return self.known and self.available_at is not None and self.available_at <= decision_at and self.effective_session == session_date and decision_session >= self.effective_session


@dataclass(frozen=True)
class SessionMarkEvidence:
    """Synthetic/session evidence persisted beside a mark; gaps are invalid."""
    session_date: date
    session_ordinal: int
    previous_session: date | None = None
    previous_ordinal: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or type(self.session_ordinal) is not int or self.session_ordinal < 0:
            raise SessionEvidenceError("session evidence is invalid")
        if (self.previous_session is None) != (self.previous_ordinal is None):
            raise SessionEvidenceError("session predecessor evidence is incomplete")
        if self.previous_ordinal is not None and self.previous_ordinal >= self.session_ordinal:
            raise SessionEvidenceError("session ordinals are not increasing")


@dataclass(frozen=True)
class FillTimingEvidence:
    decision_session_ordinal: int
    previous_completed_session_ordinal: int
    eligible_session_ordinal: int

    def __post_init__(self) -> None:
        values = (self.decision_session_ordinal, self.previous_completed_session_ordinal, self.eligible_session_ordinal)
        if any(type(value) is not int or value < 0 for value in values):
            raise SessionEvidenceError("fill session ordinal evidence is invalid")
        if not (self.previous_completed_session_ordinal < self.decision_session_ordinal < self.eligible_session_ordinal):
            raise SessionEvidenceError("fill session ordinal ordering is invalid")


@dataclass(frozen=True)
class FillTiming:
    decision_at: datetime
    fill_at: datetime
    eligible_regular_bar_id: str
    decision_session: date
    eligible_session: date
    bar_open_at: datetime
    previous_completed_bar_id: str = ""
    previous_completed_session: date | None = None
    previous_completed_bar_ordinal: int = 0
    eligible_regular_bar_ordinal: int = 0
    regular_session: bool = True
    previous_completed_at: datetime | None = None
    session_evidence: FillTimingEvidence | None = None

    def __post_init__(self) -> None:
        require_aware(self.decision_at, "decision_at")
        require_aware(self.fill_at, "fill_at")
        require_aware(self.bar_open_at, "bar_open_at")
        _identity(self.eligible_regular_bar_id, "eligible_regular_bar_id")
        _identity(self.previous_completed_bar_id, "previous_completed_bar_id")
        if self.decision_at >= self.fill_at or self.fill_at != self.bar_open_at:
            raise TemporalCausalityError("decision must precede the exact eligible bar open")
        if not isinstance(self.decision_session, date) or not isinstance(self.eligible_session, date):
            raise ContractError("fill timing sessions must be dates")
        if self.eligible_session <= self.decision_session:
            raise TemporalCausalityError("eligible bar must be on a later regular session")
        if self.previous_completed_session is None or self.previous_completed_session >= self.decision_session:
            raise TemporalCausalityError("previous completed bar evidence is invalid")
        if self.bar_open_at.date() != self.eligible_session:
            raise TemporalCausalityError("bar open timestamp does not match eligible session")
        if self.previous_completed_at is None:
            raise TemporalCausalityError("previous completed bar timestamp evidence is required")
        require_aware(self.previous_completed_at, "previous_completed_at")
        if self.previous_completed_at.date() != self.previous_completed_session or self.previous_completed_at > self.decision_at:
            raise TemporalCausalityError("previous completed bar timestamp is invalid")
        require_positive_int(self.previous_completed_bar_ordinal, "previous_completed_bar_ordinal")
        require_positive_int(self.eligible_regular_bar_ordinal, "eligible_regular_bar_ordinal")
        if self.eligible_regular_bar_ordinal != self.previous_completed_bar_ordinal + 1 or not self.regular_session:
            raise TemporalCausalityError("eligible regular bar ordinal evidence is invalid")
        if self.session_evidence is not None and not isinstance(self.session_evidence, FillTimingEvidence):
            raise ContractError("fill session evidence must be typed")


HARD_RISK_REASONS: Final[frozenset[str]] = frozenset(reason.value for reason in HardRiskReason)


def validate_hard_risk(reason: str, raw_executable_price_krw: int | None, threshold_version: str | None) -> None:
    if reason not in HARD_RISK_REASONS:
        raise ContractError(f"reason is not an allowlisted hard-risk reason: {reason}")
    if raw_executable_price_krw is None or not threshold_version:
        raise InsufficientDataError("hard risk requires raw price and versioned threshold")
    require_positive_int(raw_executable_price_krw, "raw_executable_price_krw")


def holding_session_number(entry_session: date, session_date: date, session_ordinals: dict[date, int]) -> int:
    try:
        ordinal_delta = session_ordinals[session_date] - session_ordinals[entry_session]
    except KeyError as exc:
        raise InsufficientDataError("entry and current session must be known XKRX sessions") from exc
    if ordinal_delta < 0:
        raise TemporalCausalityError("session precedes entry")
    return 1 + ordinal_delta


def validate_session_candidate(holding_number: int, *, hard_risk: bool = False, time_exit: bool = False) -> None:
    require_positive_int(holding_number, "holding_session_number")
    if hard_risk:
        return
    if holding_number < 2 or (time_exit and holding_number < 20):
        raise ContractError("exit session boundary is not satisfied")


@dataclass(frozen=True)
class EpisodeRearmEvidence:
    flat: bool
    slow_predicate_false: bool
    completed_fast_false_bars: int
    cooldown_sessions: int
    signal_persistent: bool = False

    @property
    def eligible(self) -> bool:
        return self.flat and not self.signal_persistent and self.slow_predicate_false and self.completed_fast_false_bars >= 2 and self.cooldown_sessions >= 1


class EpisodeEventType(StrEnum):
    SIGNAL = "SIGNAL"
    ADMISSION = "ADMISSION"
    EXIT = "EXIT"
    REJECT = "REJECT"
    REARM = "REARM"


@dataclass(frozen=True)
class AdmissionEvent:
    episode_id: str
    semantic_version: str
    signal_rising: bool
    result: AdmissionResult
    event_id: str = "admission-1"
    event_type: EpisodeEventType = EpisodeEventType.ADMISSION

    def __post_init__(self) -> None:
        _identity(self.episode_id, "episode_id")
        _identity(self.semantic_version, "semantic_version")
        _identity(self.event_id, "event_id")
        if not isinstance(
                self.result,
                AdmissionResult) or not isinstance(
                self.event_type,
                EpisodeEventType) or not isinstance(
                self.signal_rising,
                bool):
            raise ContractError("episode event discriminants are invalid")


@dataclass(frozen=True)
class EpisodeSnapshot:
    state: EpisodeState
    semantic_version: str
    consumed_event_ids: frozenset[str] = frozenset()
    admission_results: tuple[tuple[str, AdmissionResult], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.state, EpisodeState):
            raise ContractError("episode state must be EpisodeState")
        _identity(self.semantic_version, "semantic_version")
        if not isinstance(
                self.consumed_event_ids,
                frozenset) or any(
                not isinstance(
                value,
                str) or not value.strip() for value in self.consumed_event_ids):
            raise ContractError("episode consumed event ids must be a frozenset of identities")
        result_ids = [event_id for event_id, result in self.admission_results]
        if len(result_ids) != len(
                set(result_ids)) or not all(
                isinstance(
                event_id,
                str) and event_id.strip() for event_id in result_ids):
            raise ContractError("episode admission result ids must be unique identities")
        if not all(isinstance(result, AdmissionResult) for _, result in self.admission_results):
            raise ContractError("episode admission results must be typed")
        if not set(result_ids) <= self.consumed_event_ids:
            raise ContractError("admission result ids must be consumed event ids")


def validate_episode_transition(previous: EpisodeState, new: EpisodeState, *,
                                evidence: EpisodeRearmEvidence | None = None) -> None:
    if not isinstance(previous, EpisodeState) or not isinstance(new, EpisodeState):
        raise ContractError("episode states must be EpisodeState")
    if previous is EpisodeState.TERMINAL:
        raise ContractError("terminal episode cannot transition")
    if previous is EpisodeState.COOLDOWN and new is EpisodeState.ARMED and (evidence is None or not evidence.eligible):
        raise ContractError("cooldown evidence is insufficient to rearm")
    if previous is EpisodeState.CONSUMED and new is EpisodeState.COOLDOWN:
        raise ContractError("an explicit EXIT or REJECT event is required for cooldown")
    allowed = {EpisodeState.ARMED: EpisodeState.ACTIVE,
               EpisodeState.ACTIVE: EpisodeState.CONSUMED,
               EpisodeState.COOLDOWN: EpisodeState.ARMED}
    if allowed.get(previous) is not new:
        raise ContractError(f"invalid episode transition {previous} -> {new}")


def reduce_episode(state: EpisodeSnapshot | EpisodeState, event: AdmissionEvent, *, current_version: str,
                   evidence: EpisodeRearmEvidence | None = None) -> EpisodeSnapshot | EpisodeState:
    snapshot = state if isinstance(state, EpisodeSnapshot) else EpisodeSnapshot(state, current_version)
    if snapshot.semantic_version != current_version:
        terminal = EpisodeSnapshot(
            EpisodeState.TERMINAL,
            current_version,
            snapshot.consumed_event_ids,
            snapshot.admission_results)
        return terminal if isinstance(state, EpisodeSnapshot) else EpisodeState.TERMINAL
    if event.semantic_version != current_version:
        result: EpisodeSnapshot | EpisodeState = EpisodeSnapshot(
            EpisodeState.TERMINAL,
            current_version,
            snapshot.consumed_event_ids,
            snapshot.admission_results) if isinstance(
            state,
            EpisodeSnapshot) else EpisodeState.TERMINAL
        return result
    if event.event_id in snapshot.consumed_event_ids:
        raise ContractError("episode event was already consumed")
    next_state = snapshot.state
    admission_results = snapshot.admission_results
    if snapshot.state is EpisodeState.ARMED:
        if not event.signal_rising or event.event_type is not EpisodeEventType.SIGNAL:
            raise ContractError("only a rising signal can activate an episode")
        next_state = EpisodeState.ACTIVE
    elif snapshot.state is EpisodeState.ACTIVE:
        if not event.signal_rising or event.event_type is not EpisodeEventType.ADMISSION:
            raise ContractError("one rising admission attempt is required")
        next_state = EpisodeState.CONSUMED
        admission_results = admission_results + ((event.event_id, event.result),)
    elif snapshot.state is EpisodeState.CONSUMED:
        if event.event_type not in {EpisodeEventType.EXIT, EpisodeEventType.REJECT}:
            raise ContractError("explicit exit or reject is required before cooldown")
        next_state = EpisodeState.COOLDOWN
    elif snapshot.state is EpisodeState.COOLDOWN:
        if event.event_type is not EpisodeEventType.REARM or evidence is None or not evidence.eligible:
            raise ContractError("cooldown evidence and a non-persistent rearm are required")
        next_state = EpisodeState.ARMED
    else:
        raise ContractError("terminal episode is immutable")
    updated = EpisodeSnapshot(
        next_state,
        current_version,
        snapshot.consumed_event_ids | {
            event.event_id},
        admission_results)
    return updated if isinstance(state, EpisodeSnapshot) else next_state


def legacy_unknown(
    *,
    quantity: int | None,
    cost_krw: int | None,
    episode_id: str | None,
    horizon: int | None,
        mark: Mark | None) -> None:
    try:
        if (
            isinstance(
                quantity,
                bool) or not isinstance(
                quantity,
                int) or quantity <= 0 or isinstance(
                cost_krw,
                bool) or not isinstance(
                    cost_krw,
                    int) or cost_krw < 0 or not isinstance(
                        episode_id,
                        str) or not episode_id.strip() or isinstance(
                            horizon,
                            bool) or not isinstance(
                                horizon,
                                int) or horizon <= 0 or not isinstance(
                                    mark,
                Mark) or mark.completeness is MarkCompleteness.INCOMPLETE):
            raise InsufficientDataError("legacy evidence is INSUFFICIENT_DATA")
        require_positive_int(quantity, "quantity")
    except (ContractError, TypeError, AttributeError) as exc:
        raise InsufficientDataError("legacy evidence is INSUFFICIENT_DATA") from exc
