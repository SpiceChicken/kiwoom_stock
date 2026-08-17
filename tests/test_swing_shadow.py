from datetime import datetime, timezone

import pytest

from kiwoom_stock.application.swing_shadow import SwingShadowInput, run_same_input_shadow


def test_disabled_candidate_fanout_is_legacy_only_and_side_effect_free():
    snapshot = SwingShadowInput("snapshot-1", datetime(2026, 8, 18, 1, tzinfo=timezone.utc), {"price": 70_000})
    calls = []

    def legacy(value):
        calls.append(("legacy", id(value)))
        return {"action": "HOLD"}

    def candidate(value):
        calls.append(("candidate", id(value)))
        return {"action": "ADMIT_ENTRY"}

    run = run_same_input_shadow(
        snapshot=snapshot,
        legacy_evaluator=legacy,
        candidate_evaluator=candidate,
        candidate_enabled=False,
    )
    assert run.candidate_output is None
    assert run.evidence.candidate_enabled is False
    assert calls == [("legacy", id(snapshot))]
    assert run.evidence.side_effects is False


def test_disabled_candidate_matches_legacy_receipt_and_evidence():
    snapshot = SwingShadowInput(
        "snapshot-parity",
        datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        {"price": 70_000, "regime": "NEUTRAL"},
    )

    def legacy(value):
        return {
            "action": "HOLD",
            "snapshot_id": value.snapshot_id,
            "input_hash": value.input_hash,
        }

    baseline = run_same_input_shadow(
        snapshot=snapshot,
        legacy_evaluator=legacy,
    )
    candidate_calls = []
    disabled = run_same_input_shadow(
        snapshot=snapshot,
        legacy_evaluator=legacy,
        candidate_evaluator=lambda value: candidate_calls.append(value) or {"action": "BUY"},
        candidate_enabled=False,
    )

    assert candidate_calls == []
    assert disabled.legacy_output == baseline.legacy_output
    assert disabled.candidate_output == baseline.candidate_output
    assert disabled.evidence.to_safe_dict() == baseline.evidence.to_safe_dict()


def test_enabled_shadow_uses_same_object_and_hashes_both_outputs():
    snapshot = SwingShadowInput("snapshot-1", datetime(2026, 8, 18, 1, tzinfo=timezone.utc), {"price": 70_000})
    seen = []

    def evaluate(value):
        seen.append(id(value))
        return {"action": "HOLD", "snapshot_id": value.snapshot_id}

    run = run_same_input_shadow(
        snapshot=snapshot,
        legacy_evaluator=evaluate,
        candidate_evaluator=evaluate,
        candidate_enabled=True,
    )
    assert seen == [id(snapshot), id(snapshot)]
    assert run.evidence.legacy_output_hash is not None
    assert run.evidence.candidate_output_hash == run.evidence.legacy_output_hash
    assert run.evidence.input_hash == snapshot.input_hash


def test_shadow_rejects_evaluator_side_contracts():
    snapshot = SwingShadowInput("snapshot-1", datetime(2026, 8, 18, 1, tzinfo=timezone.utc), {})
    with pytest.raises(TypeError):
        run_same_input_shadow(snapshot=snapshot, legacy_evaluator=lambda _: "write")  # type: ignore[arg-type]


def test_shadow_rejects_nested_payload_mutation():
    snapshot = SwingShadowInput(
        "snapshot-1",
        datetime(2026, 8, 18, 1, tzinfo=timezone.utc),
        {"nested": {"items": [1]}},
    )

    def mutate(value):
        value.payload["nested"]["items"][0] = 2
        return {"action": "HOLD"}

    with pytest.raises(TypeError):
        run_same_input_shadow(snapshot=snapshot, legacy_evaluator=mutate)
