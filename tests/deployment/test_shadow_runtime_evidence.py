"""Direct contract tests for the standalone runtime evidence validator."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "validator-test"


def _continuity():
    return {
        "schema_version": 1,
        "hydration_source": "initial",
        "previous_observed_at": None,
        "history_depth": 0,
        "baseline_source": "row_4_fixed_cadence",
        "baseline_sample_index": 4,
        "baseline_time_estimated": True,
    }


def _base():
    return {
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "activation_id": ACTIVATION_ID,
        "resources_closed": True,
        "side_effects": {
            "broker_orders": False, "account": False, "oauth_revoke": False,
            "slack": False, "gemini": False, "s3": False, "reports": False,
        },
    }


def _decision_telemetry():
    return {
        "market_regime": "NEUTRAL",
        "strategy_reason_code": "JERK_NON_POSITIVE",
        "strategy_intent": "NO_ENTRY_SIGNAL",
        "paper_action": "HOLD",
        "position_before": "FLAT",
        "trading_window": "OPEN",
        "session_phase": "ENTRY",
        "net_force_band": "POSITIVE",
        "current_velocity_band": "POSITIVE",
        "thrust_band": "FROM_1_0_TO_1_5",
        "jerk_band": "NEUTRAL",
        "strength_band": "ABOVE_100",
        "trend_rsi_band": "NEUTRAL",
        "price_vwap_relation": "ABOVE",
    }


def _oneshot():
    return {
        **_base(), "schema_version": 3, "status": "PASS",
        "mode": "shadow-once", "kst_date": "2026-08-09", "calendar": "OPEN",
        "stock_code": "005930", "proxy_code": "069500", "cycles": 1,
        "http_attempts": 6,
        "api_counts": {
            "token": 1, "stock_basic": 1, "stock_chart_5m": 1,
            "proxy_chart_60m": 1, "stock_strength": 1, "stock_orderbook": 1,
        },
        "local_counts": {
            "status": 1, "paper_buy": 0, "paper_sell": 0, "error": 0,
            "critical": 0,
        },
        "db_identity": "/var/lib/kiwoom/shadow-trades.db",
        "continuity": _continuity(),
        "decision_telemetry": _decision_telemetry(),
    }


def _swing_shadow_evidence(*, enabled=False):
    return {
        "snapshot_id": "parallel-shadow-test:market-snapshot",
        "input_hash": "1" * 64,
        "legacy_output_hash": "2" * 64,
        "candidate_output_hash": "3" * 64 if enabled else None,
        "candidate_enabled": enabled,
        "candidate_database_path": "/var/lib/kiwoom/swing-candidate.sqlite3" if enabled else None,
        "candidate_portfolio_id": "swing-paper-v1" if enabled else None,
        "side_effects": False,
    }


def test_optional_swing_shadow_evidence_is_validated_and_round_tripped():
    value = {**_oneshot(), "swing_shadow_evidence": _swing_shadow_evidence()}
    result = _run(value)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == value

    enabled = {**_oneshot(), "swing_shadow_evidence": _swing_shadow_evidence(enabled=True)}
    result = _run(enabled)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == enabled

    malformed = {
        **value,
        "swing_shadow_evidence": {
            **_swing_shadow_evidence(),
            "input_hash": "not-a-hash",
        },
    }
    assert _run(malformed).returncode != 0


def test_swing_strategy_decision_evidence_is_validated_and_round_tripped():
    value = {
        **_oneshot(),
        "swing_shadow_evidence": {
            **_swing_shadow_evidence(enabled=True),
            "candidate_decision": {
                "decision_schema": "swing-decision-v1",
                "action": "ADMIT_ENTRY",
                "reason": "ENTRY_SIGNAL",
                "strategy_semantics_version": "swing-v1",
                "episode_id": "",
                "holding_session_number": 1,
                "raw_executable_price_krw": 70_000,
            },
        },
    }
    result = _run(value)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == value

    malformed = {
        **value,
        "swing_shadow_evidence": {
            **value["swing_shadow_evidence"],
            "candidate_decision": {
                **value["swing_shadow_evidence"]["candidate_decision"],
                "action": "WRITE_ORDER",
            },
        },
    }
    assert _run(malformed).returncode != 0


def test_one_shot_telemetry_hash_is_additive_and_strictly_validated():
    value = {**_oneshot(), "telemetry_row_sha256": "4" * 64}
    result = _run(value)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == value

    malformed = {**value, "telemetry_row_sha256": "not-a-hash"}
    assert _run(malformed).returncode != 0


def _cycle():
    value = _oneshot()
    value.update({
        "schema_version": 4, "event": "cycle", "mode": "shadow-continuous",
        "cycle_index": 1, "elapsed_seconds": 0.25, "interval_seconds": 60.0,
        "cycle_start_elapsed_seconds": 0.0, "observed_interval_seconds": None,
        "db_reopened": False, "db_reopens": 0,
    })
    return value


def _terminal():
    return {
        **_base(), "schema_version": 4, "event": "terminal",
        "status": "STOPPED", "mode": "shadow-continuous",
        "reason": "stop-requested", "elapsed_seconds": 120.0,
        "cycles": 2, "db_reopens": 1,
        "first_cycle_start_elapsed_seconds": 0.0,
        "second_cycle_start_elapsed_seconds": 60.0,
        "second_cycle_interval_seconds": 60.0,
        "minimum_cycle_interval_seconds": 60.0,
    }


def _run(value, *, mode="shadow-once", event="oneshot", input_format="json-lines",
         output="accepted-record", activation_id=ACTIVATION_ID):
    content = value if isinstance(value, str) else json.dumps(value)
    if input_format == "ssm-invocation" and not isinstance(value, str):
        content = json.dumps({
            "Status": "Success", "ResponseCode": 0,
            "StandardOutputContent": content,
        })
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--mode", mode, "--event", event,
         "--source-sha", SOURCE_SHA, "--image-digest", IMAGE,
         "--activation-id", activation_id, "--input-format", input_format,
         "--output", output], input=content, text=True, capture_output=True,
        check=False,
    )


@pytest.mark.parametrize("activation_id", ["shadow:colon", "a" * 65, ""])
def test_activation_id_boundary_matches_execution_policy_contract(activation_id):
    completed = _run(_oneshot(), activation_id=activation_id)
    assert completed.returncode == 2
    assert completed.stderr == "shadow evidence setup failed\n"


def test_activation_id_maximum_length_is_accepted_by_evidence_validator():
    activation_id = "a" * 64
    value = {**_oneshot(), "activation_id": activation_id}
    completed = _run(value, activation_id=activation_id)
    assert completed.returncode == 0, completed.stderr


def test_all_evidence_shapes_and_activation_summary_have_exact_keysets():
    oneshot_keys = {
        "schema_version", "status", "mode", "kst_date", "calendar",
        "source_sha", "image_digest", "activation_id", "stock_code",
        "proxy_code", "cycles", "http_attempts", "api_counts", "db_identity",
        "resources_closed", "side_effects", "local_counts", "continuity",
        "decision_telemetry",
    }
    cycle_keys = oneshot_keys | {
        "event", "cycle_index", "elapsed_seconds", "interval_seconds",
        "cycle_start_elapsed_seconds", "observed_interval_seconds",
        "db_reopened", "db_reopens",
    }
    terminal_keys = {
        "schema_version", "event", "status", "mode", "source_sha",
        "image_digest", "activation_id", "cycles", "elapsed_seconds",
        "first_cycle_start_elapsed_seconds", "second_cycle_start_elapsed_seconds",
        "second_cycle_interval_seconds", "minimum_cycle_interval_seconds",
        "db_reopens", "resources_closed", "side_effects", "reason",
    }
    assert set(_oneshot()) == oneshot_keys
    assert set(_cycle()) == cycle_keys
    assert set(_terminal()) == terminal_keys

    swing = _swing_shadow_evidence(enabled=True)
    record = {**_oneshot(), "swing_shadow_evidence": swing}
    accepted = _run(record)
    assert accepted.returncode == 0, accepted.stderr
    assert set(json.loads(accepted.stdout)) == oneshot_keys | {"swing_shadow_evidence"}

    summary = _run(record, output="activation-summary")
    assert summary.returncode == 0, summary.stderr
    assert set(json.loads(summary.stdout)) == {
        "runtime_status", "cycles", "http_attempts",
        "first_cycle_start_elapsed_seconds", "second_cycle_start_elapsed_seconds",
        "second_cycle_interval_seconds", "minimum_cycle_interval_seconds",
        "db_reopens", "database", "decision_telemetry", "side_effects",
    }


def test_full_continuous_cycle_sequence_validates_every_cycle_and_hash():
    first = {**_cycle(), "telemetry_row_sha256": "4" * 64}
    second = {
        **first,
        "cycle_index": 2,
        "cycle_start_elapsed_seconds": 60.0,
        "observed_interval_seconds": 60.0,
        "db_reopened": True,
        "db_reopens": 1,
        "telemetry_row_sha256": "5" * 64,
    }
    content = json.dumps(first) + "\n" + json.dumps(second)
    result = _run(
        content, mode="shadow-continuous", event="cycle-sequence",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["cycles"] == 2
    assert summary["hashes"] == ["4" * 64, "5" * 64]

    tampered = {
        **second,
        "side_effects": {**second["side_effects"], "s3": True},
    }
    result = _run(
        json.dumps(first) + "\n" + json.dumps(tampered),
        mode="shadow-continuous", event="cycle-sequence",
    )
    assert result.returncode != 0


def test_cycle_timing_accepts_six_decimal_serialization_rounding():
    first = {**_cycle(), "telemetry_row_sha256": "4" * 64}
    second = {
        **first,
        "cycle_index": 2,
        "cycle_start_elapsed_seconds": 60.000001,
        "observed_interval_seconds": 60.0,
        "db_reopened": True,
        "db_reopens": 1,
        "telemetry_row_sha256": "5" * 64,
    }
    result = _run(
        json.dumps(first) + "\n" + json.dumps(second),
        mode="shadow-continuous", event="cycle-sequence",
    )
    assert result.returncode == 0, result.stderr


def test_terminal_timing_accepts_six_decimal_serialization_rounding():
    terminal = _terminal()
    terminal.update({
        "second_cycle_start_elapsed_seconds": 60.000001,
        "second_cycle_interval_seconds": 60.0,
    })
    result = _run(terminal, mode="shadow-continuous", event="terminal")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("value", "mode", "event"),
    [(_oneshot(), "shadow-once", "oneshot"),
     (_cycle(), "shadow-continuous", "cycle"),
     (_terminal(), "shadow-continuous", "terminal")],
)
def test_direct_json_lines_and_ssm_invocation_have_one_contract(value, mode, event):
    direct = _run(value, mode=mode, event=event)
    invocation = _run(
        value, mode=mode, event=event, input_format="ssm-invocation",
        output="activation-summary",
    )
    assert direct.returncode == 0, direct.stderr
    assert json.loads(direct.stdout) == value
    assert invocation.returncode == 0, invocation.stderr
    summary = json.loads(invocation.stdout)
    assert summary["runtime_status"] == value["status"]
    assert summary["side_effects"]["orders"] is False
    assert summary["ssm_status"] == "Success"
    assert type(summary["ssm_response_code"]) is int
    assert summary["ssm_response_code"] == 0


@pytest.mark.parametrize("response_code", [True, 0.0, "0", None])
def test_ssm_invocation_rejects_non_exact_or_missing_response_code(response_code):
    invocation = {
        "Status": "Success", "StandardOutputContent": json.dumps(_oneshot()),
    }
    if response_code is not None:
        invocation["ResponseCode"] = response_code
    completed = _run(json.dumps(invocation), input_format="ssm-invocation")
    assert completed.returncode == 1
    assert completed.stderr == (
        "shadow evidence invalid: invocation_response_code_invalid\n"
    )


@pytest.mark.parametrize("status", ["Failed", "Cancelled", "TimedOut", None])
def test_ssm_invocation_requires_exact_success_before_stdout(status):
    invocation = {
        "ResponseCode": 0, "StandardOutputContent": json.dumps(_oneshot()),
    }
    if status is not None:
        invocation["Status"] = status
    completed = _run(json.dumps(invocation), input_format="ssm-invocation")
    assert completed.returncode == 1
    assert completed.stderr == "shadow evidence invalid: invocation_status_invalid\n"


def test_json_lines_host_input_does_not_require_an_invocation_envelope():
    completed = _run(_oneshot(), input_format="json-lines")
    assert completed.returncode == 0, completed.stderr


def test_closed_one_shot_and_single_cycle_terminal_remain_exactly_supported():
    closed = _oneshot()
    closed.update({
        "status": "CLOSED", "calendar": "CLOSED", "cycles": 0,
        "http_attempts": 0, "api_counts": {}, "local_counts": {},
        "db_identity": None, "continuity": None, "decision_telemetry": None,
    })
    terminal = _terminal()
    terminal.update({
        "cycles": 1, "db_reopens": 0,
        "second_cycle_start_elapsed_seconds": None,
        "second_cycle_interval_seconds": None,
        "minimum_cycle_interval_seconds": None,
    })
    assert _run(closed).returncode == 0
    assert _run(
        terminal, mode="shadow-continuous", event="terminal"
    ).returncode == 0


def test_calendar_closed_continuous_terminal_is_operationally_accepted():
    closed = _terminal()
    closed.update({
        "status": "CLOSED",
        "reason": "calendar-closed",
        "cycles": 0,
        "db_reopens": 0,
        "elapsed_seconds": 1.0,
        "first_cycle_start_elapsed_seconds": None,
        "second_cycle_start_elapsed_seconds": None,
        "second_cycle_interval_seconds": None,
        "minimum_cycle_interval_seconds": None,
    })
    direct = _run(
        closed, mode="shadow-continuous", event="terminal"
    )
    invocation = _run(
        closed,
        mode="shadow-continuous",
        event="terminal",
        input_format="ssm-invocation",
        output="activation-summary",
    )
    assert direct.returncode == 0, direct.stderr
    assert invocation.returncode == 0, invocation.stderr
    assert json.loads(invocation.stdout)["runtime_status"] == "CLOSED"


def test_failed_terminal_is_diagnostic_only_and_never_activation_success():
    failed = _terminal()
    failed.update({
        "status": "FAILED",
        "reason": "failure",
        "error_type": "ReadOnlyBoundaryError",
    })
    operational = _run(
        failed, mode="shadow-continuous", event="terminal"
    )
    diagnostic = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--mode",
            "shadow-continuous",
            "--event",
            "terminal",
            "--source-sha",
            SOURCE_SHA,
            "--image-digest",
            IMAGE,
            "--activation-id",
            ACTIVATION_ID,
            "--input-format",
            "json-lines",
            "--output",
            "accepted-record",
            "--terminal-policy",
            "diagnostic",
        ],
        input=json.dumps(failed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert operational.returncode == 1
    assert diagnostic.returncode == 0, diagnostic.stderr
    assert json.loads(diagnostic.stdout) == failed


def test_market_data_failure_terminal_details_are_diagnostic_only_and_bounded():
    failed = _terminal()
    failed.update({
        "status": "FAILED",
        "reason": "failure",
        "error_type": "MarketDataCollectionError",
        "error_kind": "timeout",
        "error_operation": "order_book",
    })
    operational = _run(failed, mode="shadow-continuous", event="terminal")
    diagnostic = subprocess.run(
        [
            sys.executable, str(VALIDATOR),
            "--mode", "shadow-continuous", "--event", "terminal",
            "--source-sha", SOURCE_SHA, "--image-digest", IMAGE,
            "--activation-id", ACTIVATION_ID, "--input-format", "json-lines",
            "--output", "accepted-record", "--terminal-policy", "diagnostic",
        ],
        input=json.dumps(failed), text=True, capture_output=True, check=False,
    )
    assert operational.returncode == 1
    assert diagnostic.returncode == 0, diagnostic.stderr
    assert json.loads(diagnostic.stdout) == failed


@pytest.mark.parametrize(
    ("field", "value"),
    [("error_kind", "not-a-kind"), ("error_operation", "secret-body")],
)
def test_market_data_failure_terminal_rejects_unknown_labels(field, value):
    failed = _terminal()
    failed.update({
        "status": "FAILED", "reason": "failure",
        "error_type": "MarketDataCollectionError",
        "error_kind": "timeout", "error_operation": "order_book",
    })
    failed[field] = value
    completed = subprocess.run(
        [
            sys.executable, str(VALIDATOR),
            "--mode", "shadow-continuous", "--event", "terminal",
            "--source-sha", SOURCE_SHA, "--image-digest", IMAGE,
            "--activation-id", ACTIVATION_ID, "--input-format", "json-lines",
            "--output", "accepted-record", "--terminal-policy", "diagnostic",
        ],
        input=json.dumps(failed), text=True, capture_output=True, check=False,
    )
    assert completed.returncode == 1
    assert completed.stderr == (
        "shadow evidence invalid: terminal_market_data_diagnostic_invalid\n"
    )


def test_zero_cycle_failed_terminal_is_valid_only_for_diagnostics():
    failed = _terminal()
    failed.update({
        "status": "FAILED",
        "reason": "failure",
        "cycles": 0,
        "db_reopens": 0,
        "first_cycle_start_elapsed_seconds": None,
        "second_cycle_start_elapsed_seconds": None,
        "second_cycle_interval_seconds": None,
        "minimum_cycle_interval_seconds": None,
    })
    completed = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--mode",
            "shadow-continuous",
            "--event",
            "terminal",
            "--source-sha",
            SOURCE_SHA,
            "--image-digest",
            IMAGE,
            "--activation-id",
            ACTIVATION_ID,
            "--input-format",
            "json-lines",
            "--output",
            "accepted-record",
            "--terminal-policy",
            "diagnostic",
        ],
        input=json.dumps(failed),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "updates",
    [
        {"strategy_intent": "ENTRY_SIGNAL"},
        {
            "strategy_reason_code": "UPTREND_ENTRY",
            "strategy_intent": "ENTRY_SIGNAL",
            "jerk_band": "NEUTRAL",
        },
        {
            "strategy_reason_code": "UPTREND_ENTRY",
            "strategy_intent": "ENTRY_SIGNAL",
            "jerk_band": "POSITIVE",
            "net_force_band": "NEGATIVE",
        },
        {"strategy_reason_code": "NET_FORCE_NEGATIVE"},
        {"paper_action": "BUY"},
        {"paper_action": "SELL", "position_before": "FLAT"},
    ],
)
def test_decision_telemetry_cross_field_contradictions_fail_closed(updates):
    value = _oneshot()
    value["decision_telemetry"] = {
        **value["decision_telemetry"],
        **updates,
    }
    completed = _run(value)
    assert completed.returncode == 1
    assert completed.stderr == "shadow evidence invalid: oneshot_contract_invalid\n"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "source_sha": "c" * 40},
        lambda value: {**value, "schema_version": True},
        lambda value: {**value, "resources_closed": False},
        lambda value: {**value, "side_effects": {**value["side_effects"], "account": True}},
        lambda value: {**value, "http_attempts": True},
        lambda value: {**value, "continuity": {**value["continuity"], "extra": 1}},
        lambda value: {**value, "continuity": {**value["continuity"], "previous_observed_at": "2026-08-09T10:00:00"}},
    ],
)
def test_contract_failures_are_exit_one_without_raw_reflection(mutation):
    completed = _run(mutation(_oneshot()))
    assert completed.returncode == 1
    assert completed.stderr.startswith("shadow evidence invalid: ")
    assert SOURCE_SHA not in completed.stderr
    assert IMAGE not in completed.stderr


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: {**value, "unknown_capability": True},
        lambda value: {
            key: item for key, item in value.items() if key != "calendar"
        },
        lambda value: {
            **value,
            "side_effects": {**value["side_effects"], "wire_transfer": True},
        },
        lambda value: {
            **value,
            "side_effects": {
                key: item for key, item in value["side_effects"].items()
                if key != "reports"
            },
        },
        lambda value: {**value, "kst_date": "2026-8-9"},
        lambda value: {**value, "kst_date": "not-a-date"},
    ],
)
def test_unknown_keys_and_invalid_dates_fail_closed(mutation):
    assert _run(mutation(_oneshot())).returncode == 1


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_non_standard_json_constants_and_non_finite_timings_fail(constant):
    payload = json.dumps(_cycle()).replace("0.25", constant, 1)
    assert _run(
        payload, mode="shadow-continuous", event="cycle"
    ).returncode == 1
    terminal = _terminal()
    terminal["second_cycle_interval_seconds"] = float(constant)
    assert _run(
        terminal, mode="shadow-continuous", event="terminal"
    ).returncode == 1


def test_accepted_record_is_exact_canonical_safe_projection():
    accepted = _run(_cycle(), mode="shadow-continuous", event="cycle")
    assert accepted.returncode == 0, accepted.stderr
    assert set(json.loads(accepted.stdout)) == set(_cycle())
    rejected = _run(
        {**_cycle(), "synthetic_secret": "must-not-pass"},
        mode="shadow-continuous", event="cycle",
    )
    assert rejected.returncode == 1
    assert "must-not-pass" not in rejected.stderr


@pytest.mark.parametrize("payload", ['{"broken"', "[]", "null"])
def test_malformed_and_non_object_records_are_stable_exit_one(payload):
    completed = _run(payload)
    assert completed.returncode == 1
    assert completed.stderr.startswith("shadow evidence invalid: ")


def test_bounded_input_and_usage_fail_closed_without_echoing_payload():
    secret = "secret-value-that-must-not-be-reflected"
    completed = _run("{" + secret + "x" * 1_048_576)
    assert completed.returncode == 1
    assert secret not in completed.stderr
    usage = subprocess.run(
        [sys.executable, str(VALIDATOR)], capture_output=True, text=True,
        check=False,
    )
    assert usage.returncode == 2
