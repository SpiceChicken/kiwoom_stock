"""Deterministic tests for the pure C* scheduling contract."""

from datetime import datetime, timezone

import pytest

from deploy.shadow_cstar_contract import (
    ContractError,
    DIAGNOSTIC_CATEGORIES,
    METRIC_NAMES,
    activation_id_for_session,
    allowed_state_transitions,
    canonical_json_bytes,
    canonical_sha256,
    diagnostic_category,
    make_release_intent,
    make_session_lease,
    metric_name,
    occurrence_id_for,
    occurrence_identity,
    occurrence_identity_payload,
    parse_utc_timestamp,
    project_scheduler_context,
    release_id_for,
    retention_ttl_epoch,
    validate_scheduled_slot,
    validate_scheduler_payload,
    validate_session_lease,
    validate_state_transition,
)


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


def _release(**updates):
    value = {
        "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "e" * 64,
        "source_sha": "a" * 40,
        "compose_shadow_sha256": "f" * 64,
        "worker_sha256": "b" * 64,
        "validator_sha256": "c" * 64,
        "shadow_document_sha256": "d" * 64,
        "rollout_attempt_id": "rollout-1",
    }
    value.update(updates)
    return value


def test_canonical_json_is_compact_sorted_and_hash_stable():
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_sha256({"b": 2, "a": 1}) == canonical_sha256({"a": 1, "b": 2})
    with pytest.raises(ContractError, match="invalid"):
        canonical_json_bytes({"bad": float("nan")})


def test_scheduler_payload_is_exact_and_context_projection_ignores_extra_context():
    payload = validate_scheduler_payload(_payload())
    assert payload == _payload()
    projected = project_scheduler_context(
        {
            "aws.scheduler.schedule-arn": payload["schedule_arn"],
            "aws.scheduler.scheduled-time": payload["scheduled_time"],
            "aws.scheduler.execution-id": payload["execution_id"],
            "aws.scheduler.attempt-number": "0",
            "unexpected_context": "ignored",
        },
        phase="start",
        schedule_generation="cstar-g000001",
    )
    assert projected == payload


@pytest.mark.parametrize(
    "updates",
    [
        {"extra": "field"},
        {"schema_version": True},
        {"phase": "continuous"},
        {"schedule_generation": "g1"},
        {"scheduled_time": "2026-08-24T08:50:00+09:00"},
        {"attempt_number": 1.0},
        {"attempt_number": -1},
    ],
)
def test_scheduler_payload_fails_closed(updates):
    with pytest.raises(ContractError, match="invalid"):
        validate_scheduler_payload({**_payload(), **updates})


def test_occurrence_identity_excludes_delivery_attempt_fields():
    first = occurrence_id_for(_payload(execution_id="execution-1", attempt_number="0"))
    second = occurrence_id_for(_payload(execution_id="execution-2", attempt_number="2"))
    assert first == second
    identity = occurrence_identity_payload(_payload())
    assert set(identity) == {
        "schema_version",
        "schedule_generation",
        "schedule_arn",
        "scheduled_time",
        "phase",
        "session_date_kst",
    }
    assert identity["session_date_kst"] == "2026-08-24"
    assert occurrence_identity(_payload()).activation_id == "shadow-session-20260824"


@pytest.mark.parametrize(
    ("phase", "scheduled_time"),
    [
        ("start", "2026-08-23T23:50:00Z"),
        ("stop", "2026-08-24T06:35:00Z"),
    ],
)
def test_exact_weekday_kst_slots_are_accepted(phase, scheduled_time):
    value = _payload(phase=phase, scheduled_time=scheduled_time)
    assert validate_scheduled_slot(value).tzinfo is not None


@pytest.mark.parametrize(
    "updates",
    [
        {"scheduled_time": "2026-08-23T23:51:00Z"},
        {"scheduled_time": "2026-08-22T23:50:00Z"},
        {"phase": "stop"},
    ],
)
def test_non_contract_schedule_slots_are_rejected(updates):
    with pytest.raises(ContractError, match="invalid"):
        validate_scheduled_slot({**_payload(), **updates})


def test_release_id_is_hash_of_immutable_canonical_intent():
    intent = make_release_intent(_release())
    assert tuple(intent) == tuple(sorted(intent))
    release_id = release_id_for(intent)
    assert len(release_id) == 64
    assert release_id == canonical_sha256(intent)
    with pytest.raises(ContractError, match="invalid"):
        make_release_intent({**intent, "extra": "no"})
    with pytest.raises(ContractError, match="invalid"):
        make_release_intent({**intent, "source_sha": "not-sha"})


def test_session_lease_pins_release_and_activation_for_both_phases():
    release_id = release_id_for(_release())
    lease = make_session_lease(_payload(), release_id=release_id)
    assert lease["session_date_kst"] == "2026-08-24"
    assert lease["activation_id"] == "shadow-session-20260824"
    assert validate_session_lease(lease) == lease
    stop_lease = make_session_lease(
        _payload(phase="stop", scheduled_time="2026-08-24T06:35:00Z"),
        release_id=release_id,
    )
    assert stop_lease["session_date_kst"] == lease["session_date_kst"]
    assert stop_lease["release_id"] == lease["release_id"]
    with pytest.raises(ContractError, match="invalid"):
        validate_session_lease({**lease, "activation_id": "shadow-session-20260825"})


@pytest.mark.parametrize(
    ("dimension", "current", "target"),
    [
        ("submission", "CLAIMED", "SUBMITTING"),
        ("submission", "SUBMITTING", "AMBIGUOUS"),
        ("command", "IN_PROGRESS", "SUCCESS"),
        ("runtime", "UNKNOWN", "CLOSED_HOLIDAY"),
        ("closure", "OPEN", "EVIDENCE_PENDING"),
        ("closure", "EVIDENCE_PENDING", "ALERTED"),
        ("closure", "ALERTED", "EVIDENCE_PENDING"),
        ("closure", "CLOSED", "CLOSED"),
    ],
)
def test_state_dimensions_allow_forward_or_idempotent_transitions(dimension, current, target):
    assert validate_state_transition(dimension, current, target) == target
    assert current in allowed_state_transitions(dimension, current)


@pytest.mark.parametrize(
    ("dimension", "current", "target"),
    [
        ("submission", "SUBMITTED", "CLAIMED"),
        ("command", "SUCCESS", "IN_PROGRESS"),
        ("runtime", "CLOSED_HOLIDAY", "ACCEPTED"),
        ("closure", "CLOSED", "OPEN"),
    ],
)
def test_terminal_and_out_of_order_state_transitions_are_rejected(dimension, current, target):
    with pytest.raises(ContractError, match="invalid"):
        validate_state_transition(dimension, current, target)


def test_ttl_is_absent_before_terminal_and_positive_after_terminal():
    assert retention_ttl_epoch("OPEN", terminal_at_epoch=None) is None
    assert retention_ttl_epoch("EVIDENCE_PENDING", terminal_at_epoch=None) is None
    assert retention_ttl_epoch("CLOSED", terminal_at_epoch=1_000) == 1_000 + 400 * 86_400
    assert retention_ttl_epoch("ALERTED", terminal_at_epoch=1_000) == 1_000 + 400 * 86_400
    with pytest.raises(ContractError, match="invalid"):
        retention_ttl_epoch("CLOSED", terminal_at_epoch=0)
    with pytest.raises(ContractError, match="invalid"):
        retention_ttl_epoch("OPEN", terminal_at_epoch=1_000)


def test_diagnostics_and_metrics_are_allowlisted_and_bounded():
    assert diagnostic_category("stale_generation") in DIAGNOSTIC_CATEGORIES
    assert metric_name("cstar_activation_accepted") in METRIC_NAMES
    with pytest.raises(ContractError, match="invalid"):
        diagnostic_category("raw_exception_message")
    with pytest.raises(ContractError, match="invalid"):
        metric_name("cstar_secret_value")


def test_utc_parser_rejects_naive_or_noncanonical_values():
    assert parse_utc_timestamp("2026-08-24T08:50:00Z").tzinfo == timezone.utc
    with pytest.raises(ContractError, match="invalid"):
        parse_utc_timestamp("2026-08-24T08:50:00+00:00")
