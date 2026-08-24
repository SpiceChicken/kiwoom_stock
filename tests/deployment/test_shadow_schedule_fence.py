"""Failure-mode tests for the C* host durable fence."""

from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from deploy.ec2.shadow_schedule_fence import (
    FenceError,
    ShadowScheduleFence,
)
from deploy.shadow_cstar_contract import release_id_for


ON_TIME_START = datetime(2026, 8, 23, 23, 51, tzinfo=timezone.utc)


def _payload(**updates):
    value = {
        "schema_version": 1,
        "phase": "start",
        "schedule_generation": "cstar-g000001",
        "schedule_arn": "arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/start",
        "scheduled_time": "2026-08-23T23:50:00Z",
        "execution_id": "execution-1",
        "attempt_number": "0",
    }
    value.update(updates)
    return value


def _release_id():
    return release_id_for(
        {
            "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "e" * 64,
            "source_sha": "a" * 40,
            "compose_shadow_sha256": "f" * 64,
            "worker_sha256": "b" * 64,
            "validator_sha256": "c" * 64,
            "shadow_document_sha256": "d" * 64,
            "rollout_attempt_id": "rollout-1",
        }
    )


def _fence(tmp_path: Path):
    fence = ShadowScheduleFence(
        tmp_path / "fence.json",
        lock_path=tmp_path / "fence.lock",
        incumbent_lock_path=tmp_path / "worker.lock",
    )
    release_id = _release_id()
    fence.configure_authority(
        generation="cstar-g000001",
        protocol_sha256="e" * 64,
        armed_at="2026-08-23T00:00:00Z",
    )
    fence.pin_session(
        {
            "schema_version": 1,
            "session_date_kst": "2026-08-24",
            "activation_id": "shadow-session-20260824",
            "release_id": release_id,
            "schedule_generation": "cstar-g000001",
        }
    )
    return fence, release_id


def test_claim_persists_before_apply_and_duplicate_terminal_has_no_effect(tmp_path):
    fence, release_id = _fence(tmp_path)
    first = fence.claim(
        _payload(),
        release_id=release_id,
        observed_at=ON_TIME_START,
    )
    assert first["state"] == "CLAIMED"
    applying = fence.apply(first["occurrence_id"])
    assert applying["state"] == "APPLYING"
    observed = fence.effect_observed(first["occurrence_id"], effect_id="container-1")
    assert observed["effect_id"] == "container-1"
    terminal = fence.terminal(first["occurrence_id"])
    assert terminal["state"] == "TERMINAL"
    duplicate = fence.claim(
        {**_payload(), "execution_id": "retry", "attempt_number": "2"},
        release_id=release_id,
        observed_at=datetime(2026, 8, 23, 23, 52, tzinfo=timezone.utc),
    )
    assert duplicate["duplicate"] is True
    assert duplicate["state"] == "TERMINAL"
    assert fence.read()["occurrences"][first["occurrence_id"]]["effect_id"] == "container-1"


def test_applying_duplicate_is_not_replayed_and_can_only_be_marked_ambiguous(tmp_path):
    fence, release_id = _fence(tmp_path)
    claimed = fence.claim(_payload(), release_id=release_id, observed_at=ON_TIME_START)
    fence.apply(claimed["occurrence_id"])
    again = fence.claim(
        _payload(execution_id="retry"),
        release_id=release_id,
        observed_at=ON_TIME_START,
    )
    assert again["state"] == "APPLYING"
    ambiguous = fence.ambiguous(claimed["occurrence_id"], reason="reboot during apply")
    assert ambiguous["state"] == "AMBIGUOUS"
    with pytest.raises(FenceError, match="effect not claimable"):
        fence.apply(claimed["occurrence_id"])


def test_worker_effect_holds_protocol_and_incumbent_boundary_until_effect(tmp_path):
    fence, release_id = _fence(tmp_path)
    claimed = fence.claim(_payload(), release_id=release_id, observed_at=ON_TIME_START)
    with fence.worker_effect(claimed["occurrence_id"], phase="start"):
        pass
    assert fence.read()["occurrences"][claimed["occurrence_id"]]["state"] == "EFFECT_OBSERVED"


def test_worker_effect_failure_is_ambiguous_not_success(tmp_path):
    fence, release_id = _fence(tmp_path)
    claimed = fence.claim(_payload(), release_id=release_id, observed_at=ON_TIME_START)
    with pytest.raises(RuntimeError):
        with fence.worker_effect(claimed["occurrence_id"], phase="start"):
            raise RuntimeError("worker failed")
    assert fence.read()["occurrences"][claimed["occurrence_id"]]["state"] == "AMBIGUOUS"


@pytest.mark.parametrize("reason", ["stale", "no_session", "lease"])
def test_invalid_generation_or_lease_is_durable_rejection(tmp_path, reason):
    fence, release_id = _fence(tmp_path)
    payload = _payload(
        schedule_generation="cstar-g000002" if reason == "stale" else "cstar-g000001"
    )
    actual_release = "f" * 64 if reason == "lease" else release_id
    observed_at = ON_TIME_START
    if reason == "no_session":
        payload = _payload(scheduled_time="2026-08-24T23:50:00Z")
        observed_at = datetime(2026, 8, 24, 23, 51, tzinfo=timezone.utc)
    rejected = fence.claim(payload, release_id=actual_release, observed_at=observed_at)
    assert rejected["state"] == "REJECTED"
    assert fence.read()["occurrences"][rejected["occurrence_id"]]["state"] == "REJECTED"


def test_late_trigger_is_rejected_before_state_side_effect(tmp_path):
    fence, release_id = _fence(tmp_path)
    with pytest.raises(FenceError, match="late trigger"):
        fence.claim(
            _payload(),
            release_id=release_id,
            observed_at=datetime(2026, 8, 23, 23, 59, tzinfo=timezone.utc),
        )
    assert fence.read()["occurrences"] == {}


def test_authority_cannot_be_rearmed_to_a_different_generation(tmp_path):
    fence, _ = _fence(tmp_path)
    with pytest.raises(FenceError, match="authority mismatch"):
        fence.configure_authority(
            generation="cstar-g000002",
            protocol_sha256="e" * 64,
            armed_at="2026-08-23T00:00:00Z",
        )


def test_state_file_is_canonical_single_link_and_newline_terminated(tmp_path):
    fence, _ = _fence(tmp_path)
    raw = (tmp_path / "fence.json").read_bytes()
    assert raw.endswith(b"\n")
    assert json.loads(raw) == json.loads(raw)
    assert (tmp_path / "fence.json").stat().st_nlink == 1
    assert (tmp_path / "fence.json").stat().st_mode & 0o777 == 0o600


def test_symlinked_state_is_rejected_fail_closed(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    state = tmp_path / "fence.json"
    state.symlink_to(target)
    fence = ShadowScheduleFence(state, lock_path=tmp_path / "fence.lock")
    with pytest.raises(FenceError, match="invalid"):
        fence.read()
