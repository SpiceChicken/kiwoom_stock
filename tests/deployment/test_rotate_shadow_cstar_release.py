"""Regression tests for immutable C* release rotation."""

from datetime import datetime

import pytest

from deploy.rotate_shadow_cstar_release import (
    RotationError,
    RotationState,
    _assert_safe_rotation_time,
    _rotation_transactions,
)


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


def test_rotation_is_blocked_during_weekday_market_window():
    with pytest.raises(RotationError, match="before 16:00 KST"):
        _assert_safe_rotation_time(datetime.fromisoformat("2026-08-28T09:00:00+09:00"))


def test_rotation_is_allowed_after_weekday_market_window():
    _assert_safe_rotation_time(datetime.fromisoformat("2026-08-28T16:00:00+09:00"))
