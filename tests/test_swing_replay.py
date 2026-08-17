from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kiwoom_stock.application.swing_replay import (
    ChronologicalSplit,
    CsvArtifactLocator,
    ReplayDataError,
    ReplayEvent,
    run_replay,
)
from kiwoom_stock.application.swing_candidate import build_swing_candidate_evaluator
from kiwoom_stock.application.swing_pit_evidence import run_swing_pit_hash_parity
from kiwoom_stock.application.swing_shadow import SwingShadowInput
from kiwoom_stock.application.swing_candidate_state import (
    open_read_only_swing_candidate_state_provider,
)
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy
from kiwoom_stock.infrastructure.point_in_time_replay import (
    PointInTimeReplaySource,
    SwingContextReplayAdapter,
)


UTC = timezone.utc
T0 = datetime(2026, 8, 18, 1, tzinfo=UTC)


def event(event_id, offset=0, available_offset=-2):
    decision = T0 + timedelta(minutes=offset)
    return ReplayEvent(
        event_id,
        date(2026, 8, 18),
        decision,
        decision + timedelta(minutes=available_offset),
        "snapshot-1",
        {"value": offset},
    )


def context_event(payload=None):
    context = payload or {
        "schema_version": "swing-context-v1",
        "strategy_policy": {
            "semantic_version": "swing-v1",
            "hard_risk_threshold_version": "risk-v1",
            "minimum_holding_session": 2,
            "maximum_holding_session": 20,
        },
        "episode_id": "episode-1",
        "slow": {
            "session_date": "2026-08-17",
            "bar_closed_at": (T0 - timedelta(days=1)).isoformat(),
            "available_at": (T0 - timedelta(days=1)).isoformat(),
            "computed_at": (T0 - timedelta(days=1)).isoformat(),
            "source_snapshot_id": "slow-1",
            "strategy_semantics_version": "swing-v1",
            "lookback_sessions": 20,
            "warmup_complete": True,
            "thesis_valid": True,
            "entry_eligible": True,
            "score": "1.0",
        },
        "fast": {
            "bar_id": "fast-1",
            "bar_closed_at": (T0 - timedelta(minutes=1)).isoformat(),
            "available_at": (T0 - timedelta(minutes=1)).isoformat(),
            "computed_at": (T0 - timedelta(minutes=1)).isoformat(),
            "source_snapshot_id": "fast-1",
            "strategy_semantics_version": "swing-v1",
            "trigger_rising": True,
            "entry_eligible": True,
            "score": "1.0",
        },
        "risk": {
            "raw_executable_price_krw": 70_000,
            "holding_session_number": 1,
            "mark_complete": True,
            "entry_capacity_available": True,
            "hard_risk_reason": None,
            "target_hit": False,
            "stop_hit": False,
            "same_bar_ambiguous": False,
        },
        "position": {"active": False, "position_id": "", "symbol": ""},
        "episode": {
            "state": "ARMED",
            "semantic_version": "swing-v1",
            "consumed_event_ids": [],
            "admission_results": [],
        },
        "rearm_evidence": None,
    }
    return ReplayEvent(
        "context",
        date(2026, 8, 18),
        T0,
        T0 - timedelta(minutes=2),
        "snapshot-ctx",
        {"swing_context": context},
    )


def test_point_in_time_source_excludes_equal_and_future_availability():
    source = PointInTimeReplaySource((event("one"), event("two", 10)))
    assert tuple(item.event_id for item in source.for_decision("two")) == ("one", "two")
    assert source.available_before(T0 - timedelta(minutes=3)) == ()
    with pytest.raises(ReplayDataError):
        ReplayEvent("bad", date(2026, 8, 18), T0, T0, "snapshot", {})


def test_replay_is_deterministic_and_side_effect_free():
    events = (event("one"), event("two", 10))

    def evaluator(item):
        return {"decision": "HOLD", "offset": item.payload["value"]}

    first = run_replay(
        events=events,
        dataset_id="synthetic-v1",
        strategy_semantics_version="swing-v1",
        evaluator=evaluator,
        artifact_paths=("/tmp/output/20260818/report.csv",),
    )
    second = run_replay(
        events=events,
        dataset_id="synthetic-v1",
        strategy_semantics_version="swing-v1",
        evaluator=evaluator,
        artifact_paths=first.artifact_paths,
    )
    assert first.input_hash == second.input_hash
    assert first.output_hash == second.output_hash
    assert first.side_effects is False
    assert not Path(first.artifact_paths[0]).exists()


def test_replay_rejects_unordered_or_duplicate_events():
    with pytest.raises(ReplayDataError):
        run_replay(
            events=(event("two", 10), event("one")),
            dataset_id="d",
            strategy_semantics_version="s",
            evaluator=lambda _: {},
        )
    with pytest.raises(ReplayDataError):
        run_replay(
            events=(event("one"), event("one")),
            dataset_id="d",
            strategy_semantics_version="s",
            evaluator=lambda _: {},
        )


def test_split_and_csv_location_are_explicit():
    split = ChronologicalSplit(date(2026, 8, 18), date(2026, 8, 22), 2)
    assert not split.allows_test_session(date(2026, 8, 21))
    assert split.allows_test_session(date(2026, 8, 22))
    locator = CsvArtifactLocator(Path("/var/lib/kiwoom"), date(2026, 8, 18))
    assert locator.resolve("physics_trade_analysis_20260818.csv") == Path(
        "/var/lib/kiwoom/output/20260818/physics_trade_analysis_20260818.csv"
    )
    with pytest.raises(ValueError):
        locator.resolve("../secret.csv")


def test_swing_context_replay_adapter_builds_the_real_candidate_decision():
    replay_event = context_event()
    adapter = SwingContextReplayAdapter("swing-v1")
    context = adapter(replay_event)
    shadow_input = SwingShadowInput(
        replay_event.source_snapshot_id,
        replay_event.decision_at,
        {"market": "synthetic"},
        context,
    )
    result = build_swing_candidate_evaluator()(shadow_input)
    bound_context = adapter.builder_for(replay_event)(shadow_input, object())

    assert context.policy.semantic_version == "swing-v1"
    assert bound_context == context
    assert result["action"] == "ADMIT_ENTRY"
    assert result["reason"] == "ENTRY_SIGNAL"
    assert result["episode_id"] == "episode-1"

    with pytest.raises(ReplayDataError):
        adapter.builder_for(replay_event)(
            SwingShadowInput("different", replay_event.decision_at, {}),
            object(),
        )


def test_swing_context_replay_adapter_rejects_future_context_and_non_decimal_score():
    future_payload = context_event().payload["swing_context"]
    future_payload["fast"]["available_at"] = T0.isoformat()
    with pytest.raises(ReplayDataError):
        SwingContextReplayAdapter("swing-v1")(context_event(future_payload))

    decimal_payload = context_event().payload["swing_context"]
    decimal_payload["slow"]["score"] = 1.0
    with pytest.raises(ReplayDataError):
        SwingContextReplayAdapter("swing-v1")(context_event(decimal_payload))


def test_multi_event_pit_hash_parity_uses_one_read_only_provider_per_run(tmp_path):
    first_event = context_event()
    second_payload = context_event().payload["swing_context"]
    second_event = ReplayEvent(
        "context-2",
        date(2026, 8, 18),
        T0 + timedelta(minutes=10),
        T0 + timedelta(minutes=8),
        "snapshot-ctx-2",
        {"swing_context": second_payload},
    )
    events = (first_event, second_event)
    adapter = SwingContextReplayAdapter("swing-v1")
    candidate_path = tmp_path / "candidate.sqlite3"
    accounting_policy = AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy("base-v1"),
        CostPolicy("stress-v1"),
    )
    writable = SwingLedger(
        candidate_path,
        portfolio_id="portfolio-v1",
        policy=accounting_policy,
    )
    writable.register_portfolio(idempotency_key="register-1")
    writable.close()

    def provider_factory(replay_events):
        return open_read_only_swing_candidate_state_provider(
            candidate_path,
            portfolio_id="portfolio-v1",
            episode_id="episode-1",
            accounting_policy=accounting_policy,
            context_builder=adapter.builder_for_events(replay_events),
        )

    evidence = run_swing_pit_hash_parity(
        events=events,
        dataset_id="synthetic-pit-v1",
        strategy_semantics_version="swing-v1",
        candidate_enabled=True,
        context_provider_factory=provider_factory,
        candidate_database_path=str(candidate_path),
        candidate_portfolio_id="portfolio-v1",
        legacy_evaluator=lambda snapshot: {
            "action": "HOLD",
            "snapshot_id": snapshot.snapshot_id,
        },
    )

    assert evidence.first_run.input_hash == evidence.second_run.input_hash
    assert evidence.first_run.output_hash == evidence.second_run.output_hash
    assert evidence.candidate_call_count_first == 2
    assert evidence.candidate_call_count_second == 2
    assert evidence.side_effects is False


def test_pit_hash_parity_disabled_candidate_is_never_called():
    calls = []
    evidence = run_swing_pit_hash_parity(
        events=(context_event(),),
        dataset_id="synthetic-pit-disabled-v1",
        strategy_semantics_version="swing-v1",
        candidate_enabled=False,
        legacy_evaluator=lambda snapshot: calls.append(snapshot.snapshot_id) or {"action": "HOLD"},
    )

    assert calls == ["snapshot-ctx", "snapshot-ctx"]
    assert evidence.candidate_call_count_first == 0
    assert evidence.candidate_call_count_second == 0
    assert evidence.first_run.output_hash == evidence.second_run.output_hash
