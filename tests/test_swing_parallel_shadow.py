from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from kiwoom_stock.application.execution import (
    ActivationTuple,
    ExecutionMode,
    ExecutionPolicy,
    SHADOW_DATABASE_PATH,
)
from kiwoom_stock.application.runtime import ShadowExecutionFailure, ShadowRuntime
from kiwoom_stock.application.runtime import create_shadow_runtime
from kiwoom_stock.application.shadow_worker import CalendarDecision, ShadowAdmission
from kiwoom_stock.application.swing_candidate_state import (
    open_read_only_swing_candidate_state_provider,
)
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.application.credentials import KiwoomClientCredentials, SensitiveText
from kiwoom_stock.domain.models import PhysicalContinuityEvidence, ShadowDecisionTelemetry
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
from kiwoom_stock.infrastructure.kiwoom_market_only import MarketSnapshot
from kiwoom_stock.settings import Settings
from zoneinfo import ZoneInfo


def _activation() -> ActivationTuple:
    return ActivationTuple(
        source_sha="a" * 40,
        image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
        activation_id="parallel-shadow-test",
    )


def _telemetry() -> ShadowDecisionTelemetry:
    return ShadowDecisionTelemetry(
        market_regime="NEUTRAL",
        strategy_reason_code="JERK_NON_POSITIVE",
        strategy_intent="NO_ENTRY_SIGNAL",
        paper_action="HOLD",
        position_before="FLAT",
        trading_window="OPEN",
        session_phase="ENTRY",
        net_force_band="NEUTRAL",
        current_velocity_band="NEUTRAL",
        thrust_band="FROM_0_8_TO_1_0",
        jerk_band="NEUTRAL",
        strength_band="AT_100",
        trend_rsi_band="NEUTRAL",
        price_vwap_relation="AT",
    )


class _Client:
    attempt_count = 5

    def safe_counts(self):
        return {"token": 1}

    def close(self):
        return None


class _Monitor:
    def __init__(self):
        self.closed = False

    def run_shadow_cycle(self, _stock_code):
        return {
            "cycles": 1,
            "continuity": PhysicalContinuityEvidence(1, "initial", None, 0),
            "decision_telemetry": _telemetry(),
        }

    def close(self):
        self.closed = True


def _runtime(*, enabled: bool, evaluator=None, owner=None):
    candidate_path = Path("/tmp/swing-candidate.sqlite3").resolve()
    policy = ExecutionPolicy.for_request(
        ExecutionMode.SHADOW_ONCE,
        _activation(),
        swing_candidate_enabled=enabled,
        swing_candidate_database_path=candidate_path if enabled else None,
        swing_candidate_portfolio_id="portfolio-v1" if enabled else None,
    )
    return ShadowRuntime(
        policy=policy,
        client=cast(Any, _Client()),
        monitor=cast(Any, _Monitor()),
        notifier=cast(Any, SimpleNamespace(
            safe_counts=lambda: {
                "status": 1,
                "paper_buy": 0,
                "paper_sell": 0,
                "error": 0,
                "critical": 0,
            }
        )),
        db_path=SHADOW_DATABASE_PATH,
        session=cast(Any, SimpleNamespace(close=lambda: None)),
        shadow_input=SwingShadowInput(
            "market-snapshot-1",
            datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
            {"nested": {"price": 70_000}},
        ),
        swing_candidate_enabled=enabled,
        swing_candidate_evaluator=evaluator,
        swing_candidate_database_path=candidate_path if enabled else None,
        swing_candidate_portfolio_id="portfolio-v1" if enabled else None,
        swing_candidate_context_owner=owner,
    )


def test_bounded_runtime_fans_out_one_immutable_input_to_candidate():
    seen = []

    class CandidateOwner:
        closed = False

        def close(self):
            self.closed = True

    owner = CandidateOwner()

    def candidate(snapshot):
        seen.append(id(snapshot))
        return {"action": "HOLD", "input_hash": snapshot.input_hash}

    runtime = _runtime(enabled=True, evaluator=candidate, owner=owner)
    receipt = runtime.execute_once()

    assert len(seen) == 1
    assert receipt.swing_shadow_evidence is not None
    assert receipt.swing_shadow_evidence.candidate_enabled is True
    assert receipt.swing_shadow_evidence.candidate_database_path == "/tmp/swing-candidate.sqlite3"
    assert receipt.swing_shadow_evidence.candidate_portfolio_id == "portfolio-v1"
    assert receipt.swing_shadow_evidence.legacy_output_hash is not None
    assert receipt.swing_shadow_evidence.candidate_output_hash is not None
    assert owner.closed is True


def test_bounded_runtime_disabled_candidate_does_not_call_evaluator():
    runtime = _runtime(enabled=False, evaluator=None)
    receipt = runtime.execute_once()

    assert receipt.swing_shadow_evidence is not None
    assert receipt.swing_shadow_evidence.candidate_enabled is False
    assert receipt.swing_shadow_evidence.candidate_output_hash is None


def test_candidate_owner_cleanup_preserves_primary_error_and_cleanup_trace():
    events = []

    class CandidateOwner:
        def close(self):
            events.append("candidate-close")
            raise RuntimeError("candidate cleanup failed")

    def candidate(_snapshot):
        raise ValueError("candidate evaluation failed")

    runtime = _runtime(
        enabled=True,
        evaluator=candidate,
        owner=CandidateOwner(),
    )

    with pytest.raises(ShadowExecutionFailure) as caught:
        runtime.execute_once()

    assert events == ["candidate-close"]
    assert caught.value.primary_type == "ValueError"
    assert caught.value.cleanup_types == ("RuntimeError",)
    assert caught.value.resources_closed is False


def test_shadow_composition_connects_one_fetched_snapshot_to_runtime_fanout(monkeypatch, tmp_path):
    candidate_path = (tmp_path / "candidate.sqlite3").resolve()
    settings = Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-once",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-once",
            "KIWOOM_CREDENTIALS_DIR": str(tmp_path / "credentials"),
            "KIWOOM_DB_PATH": str(SHADOW_DATABASE_PATH),
            "KIWOOM_SWING_CANDIDATE_ENABLED": "true",
            "KIWOOM_SWING_CANDIDATE_DB_PATH": str(candidate_path),
            "KIWOOM_SWING_CANDIDATE_PORTFOLIO_ID": "portfolio-v1",
        }
    )
    credentials = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    fetches = []

    class Client:
        market = object()
        attempt_count = 5

        def ensure_auth_ready(self):
            return None

        def safe_counts(self):
            return {"token": 1}

        def close(self):
            return None

    def fake_fetch(*_args, **_kwargs):
        fetches.append("snapshot")
        return MarketSnapshot(
            basic={"cur_prc": 70_000},
            stock_chart=({"cur_prc": 70_000},),
            proxy_chart=({"cur_prc": 70_000},),
            strength=({"cntr_str": 100.0},),
            order_book={"tot_sel_req": 1.0, "tot_buy_req": 1.0},
        )

    monkeypatch.setattr(
        "kiwoom_stock.application.runtime.fetch_market_snapshot",
        fake_fetch,
    )
    seen_contexts = []

    def context_builder(snapshot, hydration):
        seen_contexts.append(snapshot)
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

    accounting_policy = AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy("base-v1"),
        CostPolicy("stress-v1"),
    )
    candidate_ledger = SwingLedger(
        candidate_path,
        portfolio_id="portfolio-v1",
        policy=accounting_policy,
    )
    candidate_ledger.register_portfolio(idempotency_key="register-1")
    candidate_ledger.close()
    context_provider = open_read_only_swing_candidate_state_provider(
        candidate_path,
        portfolio_id="portfolio-v1",
        episode_id="episode-1",
        accounting_policy=accounting_policy,
        context_builder=context_builder,
    )

    runtime = create_shadow_runtime(
        policy=ExecutionPolicy.for_request(
            ExecutionMode.SHADOW_ONCE,
            _activation(),
            swing_candidate_enabled=True,
            swing_candidate_database_path=candidate_path,
            swing_candidate_portfolio_id="portfolio-v1",
        ),
        settings=settings,
        admission=ShadowAdmission(
            now=datetime(2026, 8, 18, 10, tzinfo=ZoneInfo("Asia/Seoul")),
            kst_date=date(2026, 8, 18),
            decision=CalendarDecision.OPEN,
        ),
        credential_provider_factory=lambda _path: SimpleNamespace(
            load=lambda: credentials
        ),
        session_factory=lambda **_kwargs: SimpleNamespace(close=lambda: None),
        market_client_factory=lambda *_args, **_kwargs: Client(),
        ledger_factory=lambda _path, _clock: SimpleNamespace(
            set_shutdown_deadline=lambda _remaining: None,
            close=lambda: None,
        ),
        physical_state_repository_factory=lambda _ledger: SimpleNamespace(
            close=lambda: None
        ),
        local_notifier_factory=lambda: SimpleNamespace(
            safe_counts=lambda: {
                "status": 1,
                "paper_buy": 0,
                "paper_sell": 0,
                "error": 0,
                "critical": 0,
            }
        ),
        engine_factory=lambda *_args, **_kwargs: _Monitor(),
        swing_candidate_context_factory=context_provider,
    )

    receipt = runtime.execute_once()
    assert fetches == ["snapshot"]
    assert len(seen_contexts) == 1
    assert receipt.swing_shadow_evidence is not None
    assert receipt.swing_shadow_evidence.candidate_enabled is True
    assert receipt.swing_shadow_evidence.candidate_decision == {
        "decision_schema": "swing-decision-v1",
        "action": "ADMIT_ENTRY",
        "reason": "ENTRY_SIGNAL",
        "strategy_semantics_version": "swing-v1",
        "episode_id": "episode-1",
        "holding_session_number": 1,
        "raw_executable_price_krw": 70_000,
    }
