from datetime import date, datetime, timedelta, timezone

import pytest

from kiwoom_stock.application import swing_replay as replay_module
from kiwoom_stock.application.swing_replay import (
    ChronologicalSplit,
    ReplayDataError,
    ReplayEvent,
    run_replay,
)
from kiwoom_stock.application.swing_pit_evidence import run_swing_pit_hash_parity


UTC = timezone.utc


def _events():
    sessions = [
        date(2026, 8, 17),
        date(2026, 8, 18),
        date(2026, 8, 20),
        date(2026, 8, 21),
        date(2026, 8, 22),
    ]
    events = []
    for index, session in enumerate(sessions):
        decision = datetime(2026, 8, 17 + index, tzinfo=UTC)
        events.append(
            ReplayEvent(
                f"event-{index}",
                session,
                decision,
                decision - timedelta(minutes=1),
                f"snapshot-{index}",
                {"index": index},
            )
        )
    return tuple(events)


def _session_ordinals(_start, _end):
    return {
        date(2026, 8, 17): 10,
        date(2026, 8, 18): 11,
        date(2026, 8, 20): 12,
        date(2026, 8, 21): 13,
        date(2026, 8, 22): 14,
    }


def test_purge_policy_excludes_train_tail_and_records_xkrx_receipt(monkeypatch):
    monkeypatch.setattr(replay_module, "krx_session_ordinals", _session_ordinals)

    seen = []
    result = run_replay(
        events=_events(),
        dataset_id="purge-v1",
        strategy_semantics_version="swing-v1",
        evaluator=lambda event: seen.append(event.event_id) or {"action": "HOLD"},
        split=ChronologicalSplit(
            date(2026, 8, 18), date(2026, 8, 22), purge_sessions=3
        ),
    )

    assert seen == ["event-0", "event-4"]
    assert result.decision_ids == ("event-0", "event-4")
    assert result.selection_receipt is not None
    receipt = result.selection_receipt
    assert receipt.train_event_ids == ("event-0",)
    assert receipt.purged_event_ids == ("event-1", "event-2", "event-3")
    assert receipt.test_event_ids == ("event-4",)
    assert receipt.purged_session_ordinals == (
        ("2026-08-18", 11),
        ("2026-08-20", 12),
        ("2026-08-21", 13),
    )
    assert receipt.selection_hash == result.to_dict()["selection_receipt"]["selection_hash"]


def test_purge_policy_uses_session_gap_and_fails_closed_when_insufficient(monkeypatch):
    monkeypatch.setattr(replay_module, "krx_session_ordinals", _session_ordinals)

    with pytest.raises(ReplayDataError, match="insufficient"):
        run_replay(
            events=_events(),
            dataset_id="purge-v1",
            strategy_semantics_version="swing-v1",
            evaluator=lambda _: {"action": "HOLD"},
            split=ChronologicalSplit(
                date(2026, 8, 21), date(2026, 8, 22), purge_sessions=2
            ),
        )


def test_pit_parity_applies_the_same_selection_receipt_twice(monkeypatch):
    monkeypatch.setattr(replay_module, "krx_session_ordinals", _session_ordinals)
    calls = []

    evidence = run_swing_pit_hash_parity(
        events=_events(),
        dataset_id="purge-parity-v1",
        strategy_semantics_version="swing-v1",
        candidate_enabled=False,
        legacy_evaluator=lambda snapshot: calls.append(snapshot.snapshot_id)
        or {"action": "HOLD"},
        chronological_split=ChronologicalSplit(
            date(2026, 8, 18), date(2026, 8, 22), purge_sessions=3
        ),
    )

    assert calls == ["snapshot-0", "snapshot-4"] * 2
    assert evidence.first_run.selection_receipt is not None
    assert (
        evidence.first_run.selection_receipt.selection_hash
        == evidence.second_run.selection_receipt.selection_hash
    )
