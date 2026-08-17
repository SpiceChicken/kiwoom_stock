from datetime import date, datetime, timedelta, timezone

import pytest

from kiwoom_stock.application.ports import SwingCorporateActionCommand, SwingFillCommand, SwingEpisodeAppendCommand, SwingMarkCommand, SwingTransitionConflictError
from kiwoom_stock.application.swing_session import SwingSessionCoordinator
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import (
    AccountingPolicy, CostPolicy, Fill, IncompleteGate, adjusted_feature_view,
)
from kiwoom_stock.domain.swing_contracts import (
    AdmissionEvent, AdmissionResult, CorporateAction, CorporateActionKind, EpisodeEventType,
    FillTiming, FillTimingEvidence, Mark, MarkQuality, SessionMarkEvidence,
)
from kiwoom_stock.utils.market_cal import KrxCalendarError, krx_session_ordinals


def _candidate_fill() -> Fill:
    decision = datetime(2026, 8, 18, 1, tzinfo=timezone.utc)
    opened = decision + timedelta(days=1)
    ordinals = krx_session_ordinals(date(2026, 8, 14), opened.date())
    timing = FillTiming(
        decision, opened, "bar-1", decision.date(), opened.date(), opened,
        "bar-0", date(2026, 8, 14), 1, 2,
        previous_completed_at=datetime(2026, 8, 14, 7, tzinfo=timezone.utc),
        session_evidence=FillTimingEvidence(ordinals[decision.date()], ordinals[date(2026, 8, 14)], ordinals[opened.date()]),
    )
    return Fill(
        "buy", "candidate", "005930", "BUY", 1, 100, decision, opened,
        timing=timing, position_id="pos",
    )


def test_calendar_range_rejects_weekend_boundary():
    with pytest.raises(KrxCalendarError):
        SwingSessionCoordinator().ordinals(date(2026, 8, 15), date(2026, 8, 17))


def test_mark_without_matching_session_evidence_is_fail_closed():
    mark = Mark(
        date(2026, 8, 17), 70_000, MarkQuality.OFFICIAL_CLOSE, "synthetic-close",
        datetime(2026, 8, 17, 7, tzinfo=timezone.utc),
        datetime(2026, 8, 17, 8, tzinfo=timezone.utc), 1,
        portfolio_id="candidate", position_id="pos", symbol="005930",
    )
    result = SwingSessionCoordinator().assess_mark(
        mark, current_session=date(2026, 8, 17),
        evidence=SessionMarkEvidence(date(2026, 8, 17), 999),
    )
    assert result.entry_allowed is False
    assert result.gate is IncompleteGate.SESSION_EVIDENCE


def test_non_official_mark_never_opens_entry_gate():
    coordinator = SwingSessionCoordinator()
    mark = Mark(
        date(2026, 8, 18), 70_000, MarkQuality.PROVISIONAL_LAST_VALID_REGULAR, "synthetic-last",
        datetime(2026, 8, 18, 7, tzinfo=timezone.utc),
        datetime(2026, 8, 18, 8, tzinfo=timezone.utc), 1,
        portfolio_id="candidate", position_id="pos", symbol="005930",
    )
    previous = coordinator.ordinals(date(2026, 8, 14), mark.session_date)
    ordinal = previous[mark.session_date]
    result = coordinator.assess_mark(
        mark, current_session=mark.session_date,
        evidence=SessionMarkEvidence(mark.session_date, ordinal, date(2026, 8, 14), previous[date(2026, 8, 14)]),
    )
    assert result.entry_allowed is False
    assert result.nav_allowed is False
    assert result.gate is IncompleteGate.INCOMPLETE_MARK


def test_corporate_action_keeps_raw_execution_and_replays(tmp_path):
    policy = AccountingPolicy("p1", 1_000, CostPolicy("base"), CostPolicy("stress"))
    path = tmp_path / "candidate.sqlite3"
    ledger = SwingLedger(path, portfolio_id="candidate", policy=policy)
    ledger.register_portfolio(idempotency_key="register")
    ledger.append_fill(SwingFillCommand(_candidate_fill(), "buy", 0, 0))
    action = CorporateAction(
        "split-1", "005930", date(2026, 8, 19),
        datetime(2026, 8, 20, 1, tzinfo=timezone.utc), CorporateActionKind.SPLIT,
        2, 1, portfolio_id="candidate", position_id="pos",
    )
    ledger.append_corporate_action(SwingCorporateActionCommand(
        action, datetime(2026, 8, 20, 1, tzinfo=timezone.utc),
        date(2026, 8, 19), "action", 1, 1,
    ))
    assert ledger.hydrate(portfolio_id="candidate").state.lots[0].quantity == 2
    ledger.close()
    reopened = SwingLedger(path, portfolio_id="candidate", policy=policy)
    assert reopened.hydrate(portfolio_id="candidate").state.lots[0].quantity == 2
    reopened.close()
    feature = adjusted_feature_view(100, action, symbol="005930", session_date=date(2026, 8, 19))
    assert feature.raw_price_krw == 100
    assert feature.adjusted_price_krw == 50


def test_stale_mark_command_is_rejected_before_append(tmp_path):
    policy = AccountingPolicy("p1", 1_000, CostPolicy("base"), CostPolicy("stress"))
    ledger = SwingLedger(tmp_path / "candidate.sqlite3", portfolio_id="candidate", policy=policy)
    ledger.register_portfolio(idempotency_key="register")
    ledger.append_fill(SwingFillCommand(_candidate_fill(), "buy", 0, 0))
    mark = Mark(
        date(
            2026,
            8,
            18),
        101,
        MarkQuality.OFFICIAL_CLOSE,
        "close",
        datetime(
            2026,
            8,
            18,
            2,
            tzinfo=timezone.utc),
        datetime(
            2026,
            8,
            18,
            3,
            tzinfo=timezone.utc),
        1,
        portfolio_id="candidate",
        position_id="pos",
        symbol="005930",
        session_evidence=SessionMarkEvidence(
                    date(
                        2026,
                        8,
                        18),
            999,
            date(
                        2026,
                        8,
                        17),
            998))
    with pytest.raises(SwingTransitionConflictError):
        ledger.append_mark(SwingMarkCommand(mark, "stale", 1, 1, 0, date(2026, 8, 18), mark.session_evidence))
    assert ledger.hydrate(portfolio_id="candidate").verified_mark_revisions == ()
    ledger.close()


def test_episode_append_has_typed_transition_boundary(tmp_path):
    policy = AccountingPolicy("p1", 1_000, CostPolicy("base"), CostPolicy("stress"))
    ledger = SwingLedger(tmp_path / "candidate.sqlite3", portfolio_id="candidate", policy=policy)
    ledger.register_portfolio(idempotency_key="register")
    event = AdmissionEvent("e1", "swing-v1", True, AdmissionResult.FILLED, "signal", EpisodeEventType.SIGNAL)
    receipt = ledger.append_episode(SwingEpisodeAppendCommand("episode", "e1", event))
    assert receipt.command_kind.value == "APPEND_EPISODE"
    with pytest.raises(TypeError):
        ledger.append_episode(object())  # type: ignore[arg-type]
    ledger.close()
