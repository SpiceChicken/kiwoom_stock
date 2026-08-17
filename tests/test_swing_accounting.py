from datetime import date, datetime, timezone

import pytest

from kiwoom_stock.domain.accounting import (
    AccountingInvariantError, AccountingPolicy, ApplyDailyMark,
    ApplyFill, CostPolicy, CostScenario, Fill, IncompleteGate, PortfolioSnapshot,
    UnitValidationError, apply_corporate_action, apply_fill, apply_mark, initial_state,
    reduce_portfolio,
)
from kiwoom_stock.domain.swing_contracts import (
    AdmissionResult, ContractError, CorporateAction, CorporateActionKind, FillTiming, InsufficientDataError,
    Mark, MarkCompleteness, MarkQuality, TemporalCausalityError,
)

T = datetime(2026, 8, 16, 9, tzinfo=timezone.utc)


def timing(fill_at=T.replace(day=17, hour=9)):
    return FillTiming(
        T,
        fill_at,
        "bar-next",
        T.date(),
        fill_at.date(),
        fill_at,
        "bar-prev",
        date(
            2026,
            8,
            15),
        10,
        11,
        True,
        T.replace(
            day=15,
            hour=15))


def policy():
    return AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy(
            "base-v1",
            commission_bps=10),
        CostPolicy(
            "stress-v1",
            commission_bps=20,
            tax_bps=10,
            slippage_bps=10))


def fill(side="BUY", quantity=10, portfolio="paper", position="pos-1"):
    later = T.replace(day=17, hour=9)
    return Fill(f"{side}-{position}", portfolio, "005930", side, quantity, 10_000, T,
                later, CostScenario.BASE, timing=timing(later), position_id=position)


def mark(
        quality=MarkQuality.OFFICIAL_CLOSE,
        *,
        position="pos-1",
        portfolio="paper",
        revision=1,
        supersedes=None,
        symbol="005930"):
    return Mark(date(2026, 8, 17), 11_000 if quality is not MarkQuality.MISSING else None, quality,
                "source", T, T.replace(day=17, hour=11), revision, supersedes, portfolio, position, symbol)


def test_buy_sell_identity_and_cost_bundle():
    p = policy()
    bought = apply_fill(initial_state(p), fill(), p)
    assert bought.state.cash_krw == 899_900
    assert bought.cost_bundle.stress.total_krw >= bought.cost_bundle.base.total_krw >= bought.cost_bundle.gross.total_krw
    sold = apply_fill(bought.state, fill("SELL"), p)
    assert sold.state.cash_krw == 999_800 and not sold.state.lots


def test_cross_portfolio_fill_mark_action_is_typed_and_side_effect_free():
    p = policy()
    state = initial_state(p)
    with pytest.raises(AccountingInvariantError):
        apply_fill(state, fill(portfolio="other"), p)
    with pytest.raises(AccountingInvariantError):
        reduce_portfolio(state, (ApplyFill(fill(portfolio="other")),), p, portfolio_id="paper")
    with pytest.raises(AccountingInvariantError):
        apply_mark(state, mark(portfolio="other"))
    action = CorporateAction(
        "a",
        "005930",
        T.date(),
        T,
        CorporateActionKind.SPLIT,
        portfolio_id="other",
        position_id="pos-1")
    with pytest.raises(AccountingInvariantError):
        apply_corporate_action(state, action, decision_at=T, session_date=T.date())
    assert state == initial_state(p)


def test_mark_identity_revision_and_all_lots_gate():
    p = policy()
    bought = apply_fill(initial_state(p), fill(), p).state
    second = fill(position="pos-2")
    second = Fill(
        second.fill_id,
        second.portfolio_id,
        "000660",
        second.side,
        second.quantity,
        second.raw_price_krw,
        second.decision_at,
        second.fill_at,
        second.cost_scenario,
        timing=second.timing,
        position_id=second.position_id)
    bought = apply_fill(bought, second, p).state
    incomplete = mark(MarkQuality.PROVISIONAL_LAST_VALID_REGULAR)
    unrelated = mark(position="pos-2", symbol="000660")
    r = reduce_portfolio(bought, (ApplyDailyMark(incomplete), ApplyDailyMark(unrelated)), p, portfolio_id="paper")
    assert r.snapshot.completeness is MarkCompleteness.INCOMPLETE
    assert r.state.gate is IncompleteGate.INCOMPLETE_MARK
    with pytest.raises(InsufficientDataError):
        apply_fill(r.state, fill("BUY", position="pos-2"), p)
    with pytest.raises(AccountingInvariantError):
        apply_mark(bought, mark(position="wrong"))


def test_symbol_and_position_cardinality_and_mismatch_action_fail_closed():
    p = policy()
    state = apply_fill(initial_state(p), fill(), p).state
    same_symbol_new_position = fill(position="pos-2")
    with pytest.raises(AccountingInvariantError):
        apply_fill(state, same_symbol_new_position, p)
    wrong_action = CorporateAction(
        "wrong-pos",
        "005930",
        T.date(),
        T,
        CorporateActionKind.SPLIT,
        portfolio_id="paper",
        position_id="pos-2")
    blocked = apply_corporate_action(state, wrong_action, decision_at=T, session_date=T.date()).state
    assert blocked.gate is IncompleteGate.CORPORATE_ACTION_IDENTITY
    with pytest.raises(InsufficientDataError):
        apply_fill(blocked, fill("SELL"), p)


def test_mark_revision_persists_across_reducer_calls_and_direct_nav_checks_chain():
    p = policy()
    state = apply_fill(initial_state(p), fill(), p).state
    first = mark()
    reduced = reduce_portfolio(state, (ApplyDailyMark(first),), p, portfolio_id="paper")
    second = mark(revision=2, supersedes=first.mark_id)
    continued = reduce_portfolio(reduced.state, (ApplyDailyMark(second),), p, portfolio_id="paper")
    assert continued.state.mark_for("005930", "pos-1") == second
    assert apply_mark(
        continued.state,
        Mark(
            date(
                2026,
                8,
                17),
            11_100,
            MarkQuality.OFFICIAL_CLOSE,
            "source",
            T,
            T.replace(
                day=17,
                hour=12),
            3,
            second.mark_id,
            "paper",
            "pos-1",
            "005930")) == 111_000
    with pytest.raises(AccountingInvariantError):
        apply_mark(state, mark(revision=9, supersedes="orphan"))


def test_pit_action_persists_gate_and_blocks_followup_paths():
    p = policy()
    state = apply_fill(initial_state(p), fill(), p).state
    future = CorporateAction(
        "future",
        "005930",
        T.date(),
        T.replace(
            day=18),
        CorporateActionKind.SPLIT,
        portfolio_id="paper",
        position_id="pos-1")
    blocked = apply_corporate_action(state, future, decision_at=T, session_date=T.date()).state
    assert blocked.gate is IncompleteGate.PIT_CORPORATE_ACTION
    with pytest.raises(InsufficientDataError):
        apply_fill(blocked, fill("SELL"), p)
    with pytest.raises(InsufficientDataError):
        apply_mark(blocked, mark())


def test_future_effective_action_is_not_applied_at_earlier_decision_session():
    p = policy()
    state = apply_fill(initial_state(p), fill(), p).state
    future_effective = CorporateAction(
        "future-effective",
        "005930",
        date(
            2026,
            8,
            17),
        T,
        CorporateActionKind.SPLIT,
        portfolio_id="paper",
        position_id="pos-1")
    blocked = apply_corporate_action(state, future_effective, decision_at=T, session_date=date(2026, 8, 17)).state
    assert blocked.gate is IncompleteGate.PIT_CORPORATE_ACTION


def test_dividend_preserves_quantity_and_split_rejects_cash_mixing():
    p = policy()
    state = apply_fill(initial_state(p), fill(quantity=10), p).state
    dividend = CorporateAction(
        "dividend", "005930", T.date(), T,
        CorporateActionKind.DIVIDEND, 1, 1, 25,
        portfolio_id="paper", position_id="pos-1",
    )
    applied = apply_corporate_action(
        state, dividend, decision_at=T, session_date=T.date(),
    )
    assert applied.state.lots[0].quantity == 10
    assert applied.state.cash_krw == state.cash_krw + 250
    mixed_split = CorporateAction(
        "mixed-split", "005930", T.date(), T,
        CorporateActionKind.SPLIT, 2, 1, 25,
        portfolio_id="paper", position_id="pos-1",
    )
    with pytest.raises(AccountingInvariantError):
        apply_corporate_action(
            state, mixed_split, decision_at=T, session_date=T.date(),
        )


def test_units_events_timing_and_no_cash_on_nonfilled():
    p = policy()
    with pytest.raises(UnitValidationError):
        PortfolioSnapshot("paper", 0, 1_000_000, 0, 1.5, 0, 1_000_001, MarkCompleteness.COMPLETE)
    with pytest.raises(TemporalCausalityError):
        FillTiming(T, T, "bar", T.date(), T.date(), T, "prev", T.date(), 1, 2)
    rejected = reduce_portfolio(
        initial_state(p),
        (ApplyFill(
            fill(),
            AdmissionResult.REJECTED),
         ),
        p,
        portfolio_id="paper")
    assert rejected.state.cash_krw == p.initial_cash_krw
    with pytest.raises(ContractError):
        ApplyFill(fill(), "FILLED")  # type: ignore[arg-type]
