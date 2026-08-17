from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.ports import SwingPersistenceError
from kiwoom_stock.application.swing_candidate_state import (
    SwingCandidateStateError,
    open_read_only_swing_candidate_state_provider,
)
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy
from kiwoom_stock.domain.swing_strategy import (
    FastContext,
    PositionContext,
    RiskContext,
    SlowContext,
    SwingEvaluationContext,
    SwingStrategyPolicy,
)


KST = ZoneInfo("Asia/Seoul")


def _policy() -> AccountingPolicy:
    return AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy("base-v1"),
        CostPolicy("stress-v1"),
    )


def _context_builder(snapshot, hydration):
    decision_at = snapshot.decision_at
    return SwingEvaluationContext(
        slow=SlowContext(
            date(2026, 8, 17),
            decision_at - timedelta(days=1),
            decision_at - timedelta(days=1),
            decision_at - timedelta(days=1),
            "slow-1",
            "swing-v1",
            20,
            True,
            True,
            True,
            Decimal("1.0"),
        ),
        fast=FastContext(
            "fast-1",
            decision_at - timedelta(minutes=1),
            decision_at - timedelta(minutes=1),
            decision_at - timedelta(minutes=1),
            "fast-1",
            "swing-v1",
            True,
            True,
            Decimal("1.0"),
        ),
        risk=RiskContext(70_000, 1, True, True),
        position=PositionContext(False),
        episode=hydration.episode.snapshot,
        policy=SwingStrategyPolicy("swing-v1", "risk-v1"),
        episode_id=hydration.episode.episode_id,
    )


def _snapshot() -> SwingShadowInput:
    return SwingShadowInput(
        "snapshot-1",
        datetime(2026, 8, 18, 10, tzinfo=KST),
        {"market": {"price": 70_000}},
    )


def _registered_database(path: Path) -> None:
    ledger = SwingLedger(path, portfolio_id="swing-paper-v1", policy=_policy())
    ledger.register_portfolio(idempotency_key="register-1")
    ledger.close()


def test_read_only_candidate_provider_hydrates_state_and_builds_context(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    _registered_database(path)

    provider = open_read_only_swing_candidate_state_provider(
        path,
        portfolio_id="swing-paper-v1",
        episode_id="episode-1",
        accounting_policy=_policy(),
        context_builder=_context_builder,
    )
    context = provider(_snapshot())

    assert context.position.active is False
    assert context.episode.state.value == "ARMED"
    provider.close()


def test_read_only_candidate_provider_rejects_writes(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    _registered_database(path)
    provider = open_read_only_swing_candidate_state_provider(
        path,
        portfolio_id="swing-paper-v1",
        episode_id="episode-1",
        accounting_policy=_policy(),
        context_builder=_context_builder,
    )

    with pytest.raises(SwingPersistenceError):
        provider._ledger.register_portfolio(idempotency_key="must-not-write")
    provider.close()


def test_candidate_provider_rejects_context_that_invents_active_position(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    _registered_database(path)

    def invalid_builder(snapshot, hydration):
        valid = _context_builder(snapshot, hydration)
        return SwingEvaluationContext(
            valid.slow,
            valid.fast,
            valid.risk,
            PositionContext(True, "invented", "005930"),
            valid.episode,
            valid.policy,
            valid.episode_id,
        )

    provider = open_read_only_swing_candidate_state_provider(
        path,
        portfolio_id="swing-paper-v1",
        episode_id="episode-1",
        accounting_policy=_policy(),
        context_builder=invalid_builder,
    )
    with pytest.raises(SwingCandidateStateError):
        provider(_snapshot())
    provider.close()


def test_candidate_provider_rejects_context_with_mismatched_episode_identity(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    _registered_database(path)

    def invalid_builder(snapshot, hydration):
        valid = _context_builder(snapshot, hydration)
        return SwingEvaluationContext(
            slow=valid.slow,
            fast=valid.fast,
            risk=valid.risk,
            position=valid.position,
            episode=valid.episode,
            policy=valid.policy,
            episode_id="episode-invented",
        )

    provider = open_read_only_swing_candidate_state_provider(
        path,
        portfolio_id="swing-paper-v1",
        episode_id="episode-1",
        accounting_policy=_policy(),
        context_builder=invalid_builder,
    )
    with pytest.raises(SwingCandidateStateError):
        provider(_snapshot())
    provider.close()
