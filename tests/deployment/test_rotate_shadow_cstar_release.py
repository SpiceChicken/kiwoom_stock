"""Regression tests for immutable C* release rotation."""

from datetime import datetime
import json

import pytest

from deploy.bootstrap_shadow_cstar_ledger import (
    BootstrapConfig,
    _generation,
    _release,
    _release_item,
)
from deploy.rotate_shadow_cstar_release import (
    RotationError,
    RotationState,
    _assert_safe_rotation_time,
    _prepare_rotation,
    _rotation_transactions,
    _write_audit,
)


def test_rotation_audit_is_bounded_json(tmp_path):
    path = tmp_path / "rotation.json"
    _write_audit(path.as_posix(), {"status": "success", "new_release_id": "b" * 64})

    value = json.loads(path.read_text(encoding="utf-8"))
    assert value == {"new_release_id": "b" * 64, "status": "success"}
from deploy.shadow_cstar_contract import release_id_for


def _state(*, exists: bool = False) -> RotationState:
    return RotationState(
        old_release_id="a" * 64,
        new_release_id="b" * 64,
        new_release={
            "compose_shadow_sha256": "c" * 64,
            "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "d" * 64,
            "rollout_attempt_id": "1",
            "shadow_document_sha256": "e" * 64,
            "source_sha": "f" * 40,
            "validator_sha256": "0" * 64,
            "worker_sha256": "1" * 64,
        },
        new_release_exists=exists,
    )


def _config() -> BootstrapConfig:
    return BootstrapConfig(
        table_name="table",
        generation="cstar-g000001",
        protocol_sha256="a" * 64,
        source_sha="b" * 40,
        image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
        compose_shadow_sha256="d" * 64,
        worker_sha256="e" * 64,
        validator_sha256="f" * 64,
        shadow_document_sha256="0" * 64,
        rollout_attempt_id="1",
    )


def test_rotation_adds_immutable_release_and_conditionally_moves_pointer():
    transactions = _rotation_transactions("table", _state())

    assert len(transactions) == 2
    put = transactions[0]["Put"]
    update = transactions[1]["Update"]
    assert put["Item"]["PK"] == "RELEASE#" + "b" * 64
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"
    assert update["Key"] == {"PK": "CONTROL#CSTAR", "SK": "RELEASE"}
    assert "release_id = :old_release_id" in update["ConditionExpression"]
    assert update["ExpressionAttributeValues"] == {
        ":new_release_id": "b" * 64,
        ":old_release_id": "a" * 64,
        ":active": "ACTIVE",
    }


def test_rotation_reuses_existing_exact_release_without_second_put():
    transactions = _rotation_transactions("table", _state(exists=True))

    assert len(transactions) == 1
    assert "Update" in transactions[0]


def test_prepare_rotation_allows_check_when_candidate_is_already_active(monkeypatch):
    config = _config()
    start_arn = "arn:aws:scheduler:start"
    stop_arn = "arn:aws:scheduler:stop"
    release = _release(config)
    release_id = release_id_for(release)
    generation = _generation(config, start_arn, stop_arn)
    release_item = _release_item(config, release)
    items = {
        (str(generation["PK"]), str(generation["SK"])): generation,
        ("CONTROL#CSTAR", "RELEASE"): {
            "release_id": release_id,
            "state": "ACTIVE",
        },
        (str(release_item["PK"]), str(release_item["SK"])): release_item,
    }

    def fake_read(_table, key):
        return items.get((str(key["PK"]), str(key["SK"])))

    monkeypatch.setattr("deploy.rotate_shadow_cstar_release._read", fake_read)
    state = _prepare_rotation(object(), config, start_arn, stop_arn)

    assert state.old_release_id == release_id
    assert state.new_release_id == release_id
    assert state.new_release_exists is True


def test_rotation_is_blocked_during_weekday_market_window():
    with pytest.raises(RotationError, match="09:00-16:00 KST"):
        _assert_safe_rotation_time(datetime.fromisoformat("2026-08-28T09:00:00+09:00"))


def test_rotation_is_allowed_before_next_weekday_market_open():
    _assert_safe_rotation_time(
        datetime.fromisoformat("2026-09-01T00:15:00+09:00")
    )


def test_rotation_is_allowed_after_weekday_market_window():
    _assert_safe_rotation_time(datetime.fromisoformat("2026-08-28T16:00:00+09:00"))
