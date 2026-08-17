"""Bounded, deterministic P3 session coordination (no scheduler or runtime wiring)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from kiwoom_stock.domain.accounting import IncompleteGate
from kiwoom_stock.domain.swing_contracts import (
    CorporateAction, FillTiming, Mark, MarkCompleteness, MarkQuality,
    SessionMarkEvidence,
)
from kiwoom_stock.utils.market_cal import KrxCalendarError, require_krx_session_range


@dataclass(frozen=True)
class MarkAssessment:
    mark: Mark
    stale_age_sessions: int
    entry_allowed: bool
    nav_allowed: bool
    gate: IncompleteGate | None = None


@dataclass(frozen=True)
class CorporateActionAssessment:
    action: CorporateAction
    feature_allowed: bool
    accounting_allowed: bool
    gate: IncompleteGate | None = None


class SwingSessionCoordinator:
    """Validate only injected evidence over a bounded XKRX session range."""

    def __init__(self, *, max_stale_sessions: int = 1) -> None:
        if type(max_stale_sessions) is not int or max_stale_sessions < 0:
            raise ValueError("max_stale_sessions must be a non-negative integer")
        self.max_stale_sessions = max_stale_sessions

    def ordinals(self, start: date, end: date) -> dict[date, int]:
        return require_krx_session_range(start, end)

    def assess_mark(
        self,
        mark: Mark,
        *,
        current_session: date,
        evidence: SessionMarkEvidence,
    ) -> MarkAssessment:
        try:
            if evidence.previous_session is None or evidence.previous_ordinal is None:
                raise ValueError("mark predecessor evidence is required")
            start = min(mark.session_date, current_session, evidence.previous_session)
            end = max(mark.session_date, current_session, evidence.previous_session)
            ordinals = self.ordinals(start, end)
            if evidence.session_date != mark.session_date or ordinals.get(
                    mark.session_date) != evidence.session_ordinal:
                raise ValueError("mark session evidence mismatch")
            if ordinals.get(evidence.previous_session) != evidence.previous_ordinal:
                raise ValueError("mark predecessor evidence mismatch")
            if evidence.previous_ordinal + 1 != evidence.session_ordinal:
                raise ValueError("mark session evidence mismatch")
            if evidence.previous_session is None or evidence.previous_ordinal is None:
                raise ValueError("mark predecessor session evidence is required")
            predecessor_ordinals = self.ordinals(evidence.previous_session, mark.session_date)
            if predecessor_ordinals.get(
                    evidence.previous_session) != evidence.previous_ordinal or evidence.previous_ordinal + 1 != evidence.session_ordinal:
                raise ValueError("mark predecessor session evidence mismatch")
            current_ordinal = ordinals[current_session]
            age = mark.stale_age(current_ordinal, evidence.session_ordinal)
        except (KrxCalendarError, KeyError, ValueError):
            return MarkAssessment(mark, 0, False, False, IncompleteGate.SESSION_EVIDENCE)
        valid_quality = mark.quality in MarkQuality
        complete = mark.completeness is MarkCompleteness.COMPLETE
        allowed = valid_quality and complete and age <= self.max_stale_sessions
        gate = None if allowed else (IncompleteGate.STALE_MARK if complete else IncompleteGate.INCOMPLETE_MARK)
        return MarkAssessment(mark, age, allowed, allowed, gate)

    def assess_corporate_action(
        self,
        action: CorporateAction,
        *,
        decision_at: datetime,
        effective_session: date,
        decision_session: date,
    ) -> CorporateActionAssessment:
        if not action.known:
            return CorporateActionAssessment(action, False, False, IncompleteGate.UNKNOWN_CORPORATE_ACTION)
        usable = action.usable_at(decision_at, effective_session, decision_session=decision_session)
        return CorporateActionAssessment(
            action, usable, usable, None if usable else IncompleteGate.PIT_CORPORATE_ACTION)

    def validate_fill_timing(self, timing: FillTiming) -> None:
        """Validate timing against calendar ordinals, not self-asserted dates alone."""
        evidence = timing.session_evidence
        if evidence is None:
            raise ValueError("calendar-backed fill session evidence is required")
        previous_completed = timing.previous_completed_session
        if previous_completed is None:
            raise ValueError("previous completed session is required")
        eligible_session = timing.eligible_session
        ordinals = self.ordinals(previous_completed, eligible_session)
        for session in (timing.previous_completed_session, timing.decision_session, timing.eligible_session):
            if session not in ordinals:
                raise ValueError("fill timing contains a non-session date")
        if evidence.previous_completed_session_ordinal != ordinals[previous_completed]:
            raise ValueError("previous session ordinal evidence mismatch")
        if evidence.decision_session_ordinal != ordinals[timing.decision_session] or evidence.eligible_session_ordinal != ordinals[timing.eligible_session]:
            raise ValueError("decision/eligible session ordinal evidence mismatch")
        if ordinals[eligible_session] != ordinals[timing.decision_session] + 1:
            raise ValueError("eligible session is not the next regular session after decision")
        if timing.bar_open_at.date() != timing.eligible_session:
            raise ValueError("bar open is outside the eligible session")

    def validate_episode_rearm(self, *, previous_session: date, current_session: date) -> None:
        """Require one real XKRX cooldown session between exit and re-arm."""

        ordinals = self.ordinals(previous_session, current_session)
        if ordinals[current_session] != ordinals[previous_session] + 1:
            raise ValueError("episode re-arm is not on the next regular session")
