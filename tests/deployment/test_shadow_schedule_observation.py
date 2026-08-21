"""Contracts for bounded scheduled-run delay observations."""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from deploy.shadow_schedule_observation import (
    CRON_CONTRACT,
    MAX_RUN_INPUT_BYTES,
    ObservationError,
    build_observation,
    render_summary,
    validate_observation,
)


SCRIPT = Path("deploy/shadow_schedule_observation.py")


def _run_value(**updates):
    value = {
        "event": "schedule",
        "id": 123,
        "created_at": "2026-08-20T23:53:00Z",
        "run_started_at": "2026-08-20T23:54:00Z",
        "head_branch": "main",
    }
    value.update(updates)
    return value


def _build(run=None, **updates):
    arguments = {
        "run_id": "123",
        "cron": "50 23 * * 0-4",
        "desired_state": "continuous",
    }
    arguments.update(updates)
    return build_observation(run or _run_value(), **arguments)


def test_start_observation_handles_utc_to_kst_date_boundary_exactly():
    assert _build() == {
        "schema_version": 1,
        "run_id": "123",
        "cron": "50 23 * * 0-4",
        "desired_state": "continuous",
        "expected_at_utc": "2026-08-20T23:50:00Z",
        "created_at_utc": "2026-08-20T23:53:00Z",
        "run_started_at_utc": "2026-08-20T23:54:00Z",
        "delivery_delay_seconds": 180,
        "queue_delay_seconds": 60,
        "total_start_delay_seconds": 240,
    }


def test_stop_observation_uses_the_second_exact_cron():
    observation = _build(
        _run_value(
            created_at="2026-08-21T06:40:00Z",
            run_started_at="2026-08-21T06:42:00Z",
        ),
        cron="35 6 * * 1-5",
        desired_state="stop",
    )
    assert observation["expected_at_utc"] == "2026-08-21T06:35:00Z"
    assert observation["delivery_delay_seconds"] == 300
    assert observation["queue_delay_seconds"] == 120
    assert observation["total_start_delay_seconds"] == 420


def test_literal_cron_meanings_match_all_helper_timing_fields_exactly():
    assert set(CRON_CONTRACT) == {
        "50 23 * * 0-4",
        "35 6 * * 1-5",
    }

    for cron, contract in CRON_CONTRACT.items():
        minute, hour, day_of_month, month, weekday_range = cron.split()
        weekday_start, weekday_end = map(int, weekday_range.split("-"))
        weekdays = {
            (cron_weekday - 1) % 7
            for cron_weekday in range(weekday_start, weekday_end + 1)
        }

        assert day_of_month == month == "*"
        assert set(contract) == {
            "desired_state", "hour", "minute", "weekdays",
        }
        assert contract["hour"] == int(hour)
        assert contract["minute"] == int(minute)
        assert contract["weekdays"] == weekdays


def test_summary_contains_only_bounded_operational_fields():
    summary = render_summary(_build())
    assert summary == (
        "### Shadow schedule observation\n\n"
        "- action: `continuous`\n"
        "- expected UTC: `2026-08-20T23:50:00Z`\n"
        "- total start delay: `240s`\n"
    )
    assert "created_at" not in summary
    assert "run_id" not in summary


@pytest.mark.parametrize(
    "updates",
    [
        {"event": "workflow_dispatch"},
        {"id": True},
        {"id": 124},
        {"head_branch": "feature"},
        {"created_at": "2026-08-20T23:53:00+00:00"},
        {"created_at": "2026-08-20T23:53:00.000Z"},
        {"run_started_at": "2026-08-20T23:52:59Z"},
        {"extra": "unsafe"},
    ],
)
def test_current_run_envelope_is_exact_and_fail_closed(updates):
    with pytest.raises(ObservationError, match="invalid"):
        _build(_run_value(**updates))


@pytest.mark.parametrize(
    ("cron", "desired_state"),
    [
        ("50 23 * * *", "continuous"),
        ("50 23 * * 0-4", "stop"),
        ("35 6 * * 1-5", "continuous"),
    ],
)
def test_only_exact_cron_action_pairs_are_allowed(cron, desired_state):
    with pytest.raises(ObservationError, match="invalid"):
        _build(cron=cron, desired_state=desired_state)


def test_delay_of_24_hours_or_more_is_ambiguous_and_rejected():
    with pytest.raises(ObservationError, match="invalid"):
        _build(_run_value(
            created_at="2026-08-22T23:50:00Z",
            run_started_at="2026-08-22T23:50:00Z",
        ))


@pytest.mark.parametrize(
    "updates",
    [
        {"delivery_delay_seconds": 181},
        {"queue_delay_seconds": -1},
        {"total_start_delay_seconds": 0},
        {"expected_at_utc": "2026-08-20T23:49:00Z"},
        {"cron": "35 6 * * 1-5"},
        {"desired_state": "stop"},
        {"unexpected": "value"},
    ],
)
def test_observation_schema_and_arithmetic_are_revalidated(updates):
    value = {**_build(), **updates}
    with pytest.raises(ObservationError, match="invalid"):
        validate_observation(value, desired_state="continuous")


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_schema_version_requires_exact_integer_type(schema_version):
    value = {**_build(), "schema_version": schema_version}
    with pytest.raises(ObservationError, match="invalid"):
        validate_observation(value)


@pytest.mark.parametrize(
    "field",
    [
        "delivery_delay_seconds",
        "queue_delay_seconds",
        "total_start_delay_seconds",
    ],
)
def test_delay_fields_reject_boolean_even_when_equal_to_zero(field):
    zero_delay = _build(_run_value(
        created_at="2026-08-20T23:50:00Z",
        run_started_at="2026-08-20T23:50:00Z",
    ))
    zero_delay[field] = False
    with pytest.raises(ObservationError, match="invalid"):
        validate_observation(zero_delay)


@pytest.mark.parametrize(
    "field",
    [
        "delivery_delay_seconds",
        "queue_delay_seconds",
        "total_start_delay_seconds",
    ],
)
def test_delay_fields_reject_integral_float(field):
    value = _build()
    value[field] = float(value[field])
    with pytest.raises(ObservationError, match="invalid"):
        validate_observation(value)


@pytest.mark.parametrize(
    ("expected_run_id", "expected_cron"),
    [
        ("124", "50 23 * * 0-4"),
        ("123", "35 6 * * 1-5"),
    ],
)
def test_expected_run_and_cron_binding_mismatch_is_invalid(
    expected_run_id, expected_cron,
):
    with pytest.raises(ObservationError, match="invalid"):
        validate_observation(
            _build(),
            desired_state="continuous",
            expected_run_id=expected_run_id,
            expected_cron=expected_cron,
        )


def test_cli_writes_exact_artifact_and_summary(tmp_path):
    run_path = tmp_path / "run.json"
    output_path = tmp_path / "observation.json"
    summary_path = tmp_path / "summary.md"
    run_path.write_text(json.dumps(_run_value()), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-json", str(run_path),
            "--run-id", "123",
            "--cron", "50 23 * * 0-4",
            "--desired-state", "continuous",
            "--output", str(output_path),
            "--summary", str(summary_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(output_path.read_text(encoding="utf-8")) == _build()
    assert summary_path.read_text(encoding="utf-8") == render_summary(_build())


@pytest.mark.parametrize(
    "payload",
    [
        '{"event":"schedule","event":"schedule"}',
        '{"event":NaN}',
        "{",
        "x" * (MAX_RUN_INPUT_BYTES + 1),
    ],
)
def test_cli_rejects_invalid_or_oversize_input_without_artifact(
    tmp_path, payload,
):
    run_path = tmp_path / "run.json"
    output_path = tmp_path / "observation.json"
    summary_path = tmp_path / "summary.md"
    run_path.write_text(payload, encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--run-json", str(run_path),
            "--run-id", "123",
            "--cron", "50 23 * * 0-4",
            "--desired-state", "continuous",
            "--output", str(output_path),
            "--summary", str(summary_path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "shadow schedule observation failed\n"
    assert not output_path.exists()
