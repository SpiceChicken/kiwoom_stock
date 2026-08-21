"""Contracts for redacted SSM failure diagnostics."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path("deploy/ec2/shadow_invocation_diagnostic.py")
WORKER_SCRIPT = Path("deploy/ec2/shadow_worker_control.sh")
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "diagnostic-test"
COMMAND_ID = "00000000-0000-0000-0000-000000000001"
STOP_TARGET_ABSENT_LINE = (
    "shadow worker failed: shadow container is absent; "
    "stop identity cannot be proven"
)


def _terminal(**updates):
    value = {
        "schema_version": 4,
        "event": "terminal",
        "status": "FAILED",
        "mode": "shadow-continuous",
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "activation_id": ACTIVATION_ID,
        "cycles": 2,
        "elapsed_seconds": 121.0,
        "first_cycle_start_elapsed_seconds": 0.0,
        "second_cycle_start_elapsed_seconds": 60.0,
        "second_cycle_interval_seconds": 60.0,
        "minimum_cycle_interval_seconds": 60.0,
        "db_reopens": 1,
        "resources_closed": True,
        "side_effects": {
            "broker_orders": False,
            "account": False,
            "oauth_revoke": False,
            "slack": False,
            "gemini": False,
            "s3": False,
            "reports": False,
        },
        "reason": "failure",
        "error_type": "ReadOnlyBoundaryError",
    }
    value.update(updates)
    return value


def _run(invocation, *, desired_state="stop"):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source-sha",
            SOURCE_SHA,
            "--image-digest",
            IMAGE,
            "--activation-id",
            ACTIVATION_ID,
            "--desired-state",
            desired_state,
            "--command-id",
            COMMAND_ID,
        ],
        input=json.dumps(invocation),
        text=True,
        capture_output=True,
        check=False,
    )


def _actual_stop_target_absent_output():
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; fail "shadow container is absent; '
            'stop identity cannot be proven"',
            "test",
            str(WORKER_SCRIPT),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_failed_invocation_is_classified_without_reflecting_stderr():
    secret = "do-not-reflect-this-body"
    completed = _run(
        {
            "Status": "Failed",
            "ResponseCode": 1,
            "StandardOutputContent": "",
            "StandardErrorContent": (
                f"{secret}\ncontinuous terminal safe evidence is missing"
            ),
        }
    )
    assert completed.returncode == 0, completed.stderr
    assert secret not in completed.stdout
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["failure_category"] == "terminal_evidence_missing"
    assert diagnostic["ssm_status"] == "Failed"
    assert diagnostic["ssm_response_code"] == 1
    assert diagnostic["terminal"] is None


@pytest.mark.parametrize(
    ("marker", "category"),
    [
        ("image_pull_no_space", "image_pull_no_space"),
        ("image_pull_auth", "image_pull_auth"),
        ("image_pull_not_found", "image_pull_not_found"),
        ("image_pull_network", "image_pull_network"),
        ("image_pull_failed", "image_pull_failed"),
    ],
)
def test_image_pull_failure_is_classified_without_reflecting_docker_output(
    marker, category,
):
    secret = "registry response must not be reflected"
    completed = _run(
        {
            "Status": "Failed",
            "ResponseCode": 1,
            "StandardOutputContent": "",
            "StandardErrorContent": (
                f"shadow worker failed: image_pull_category={marker}\n"
                f"{secret}\n"
            ),
        }
    )
    assert completed.returncode == 0, completed.stderr
    assert secret not in completed.stdout
    assert json.loads(completed.stdout)["failure_category"] == category


def test_safe_failed_terminal_collapses_non_allowlisted_error_type():
    terminal = _terminal()
    completed = _run(
        {
            "Status": "Failed",
            "ResponseCode": 1,
            "StandardOutputContent": json.dumps(terminal),
            "StandardErrorContent": (
                "shadow runtime terminal state is non-operational"
            ),
        }
    )
    assert completed.returncode == 0, completed.stderr
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["failure_category"] == "runtime_terminal_nonoperational"
    assert diagnostic["terminal"] == {
        "status": "FAILED",
        "reason": "failure",
        "error_type": None,
        "cycles": 2,
        "db_reopens": 1,
        "resources_closed": True,
        "elapsed_seconds": 121.0,
    }
    assert "ReadOnlyBoundaryError" not in completed.stdout
    assert "side_effects" not in diagnostic["terminal"]


@pytest.mark.parametrize(
    ("stream", "other_stream"),
    [
        ("StandardOutputContent", "StandardErrorContent"),
        ("StandardErrorContent", "StandardOutputContent"),
    ],
)
def test_exact_physical_state_sentinel_is_classified_from_either_stream(
    stream, other_stream,
):
    secret = "physical validation detail must not be reflected"
    invocation = {
        "Status": "Failed",
        "ResponseCode": 1,
        stream: (
            "shadow worker failed: "
            "error_type=PhysicalStateValidationError\n"
        ),
        other_stream: secret,
    }
    completed = _run(invocation)
    assert completed.returncode == 0, completed.stderr
    assert secret not in completed.stdout
    assert json.loads(completed.stdout)["failure_category"] == (
        "physical_state_validation_error"
    )


def test_validated_physical_state_terminal_uses_specific_category():
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": json.dumps(_terminal(
            error_type="PhysicalStateValidationError",
        )),
        "StandardErrorContent": "opaque failure",
    })
    assert completed.returncode == 0, completed.stderr
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["failure_category"] == (
        "physical_state_validation_error"
    )
    assert diagnostic["terminal"]["error_type"] == (
        "PhysicalStateValidationError"
    )


@pytest.mark.parametrize(
    "sentinel",
    [
        "prefix shadow worker failed: error_type=PhysicalStateValidationError",
        "shadow worker failed: error_type=PhysicalStateValidationError suffix",
        "shadow worker failed: error_type=UnknownError",
    ],
)
def test_near_match_and_unknown_error_sentinels_fail_closed(sentinel):
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": sentinel,
        "StandardErrorContent": "opaque failure",
    })
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["failure_category"] == (
        "ssm_failed_unclassified"
    )


def test_oversize_sentinel_stream_does_not_promote_specific_category():
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": (
            "x" * 65_537
            + "\nshadow worker failed: "
            "error_type=PhysicalStateValidationError"
        ),
        "StandardErrorContent": "opaque failure",
    })
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["failure_category"] == (
        "ssm_failed_unclassified"
    )


def test_conflicting_specific_markers_fail_closed_to_generic_category():
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": (
            "shadow worker failed: "
            "error_type=PhysicalStateValidationError"
        ),
        "StandardErrorContent": (
            "shadow worker failed: image_pull_category=image_pull_network"
        ),
    })
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["failure_category"] == (
        "ssm_failed_unclassified"
    )


def test_actual_stop_absent_producer_line_is_exact_and_stop_specific():
    producer = _actual_stop_target_absent_output()
    assert producer.returncode == 1
    assert producer.stdout == f"{STOP_TARGET_ABSENT_LINE}\n"
    assert producer.stderr == f"{STOP_TARGET_ABSENT_LINE}\n"
    invocation = {
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": producer.stdout,
        "StandardErrorContent": producer.stderr,
    }
    stopped = _run(invocation, desired_state="stop")
    assert stopped.returncode == 0, stopped.stderr
    assert json.loads(stopped.stdout)["failure_category"] == (
        "stop_target_absent"
    )


@pytest.mark.parametrize("desired_state", ["oneshot", "continuous"])
def test_actual_stop_absent_line_is_rejected_for_non_stop_action(desired_state):
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": STOP_TARGET_ABSENT_LINE,
        "StandardErrorContent": STOP_TARGET_ABSENT_LINE,
    }, desired_state=desired_state)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["failure_category"] == (
        "ssm_failed_unclassified"
    )


@pytest.mark.parametrize(
    "line",
    [
        "shadow container is absent",
        "shadow container is absent; stop identity cannot be proven",
        "shadow worker failed: shadow container is absent",
        f"prefix {STOP_TARGET_ABSENT_LINE}",
        f"{STOP_TARGET_ABSENT_LINE} suffix",
    ],
)
def test_bare_and_near_match_absent_lines_are_rejected(line):
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": "",
        "StandardErrorContent": line,
    })
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["failure_category"] == (
        "ssm_failed_unclassified"
    )


def test_unsafe_or_wrong_tuple_terminal_is_not_exposed():
    terminal = _terminal(
        source_sha="c" * 40,
        error_type="SecretBodyShouldNotAppear",
    )
    completed = _run(
        {
            "Status": "Failed",
            "ResponseCode": 1,
            "StandardOutputContent": json.dumps(terminal),
            "StandardErrorContent": "opaque failure",
        }
    )
    assert completed.returncode == 0, completed.stderr
    assert "SecretBodyShouldNotAppear" not in completed.stdout
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["terminal"] is None
    assert diagnostic["failure_category"] == "ssm_failed_unclassified"


def test_validated_unknown_terminal_error_type_is_not_reflected():
    unsafe_error_type = "SecretBodyShouldNotAppear"
    completed = _run({
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardOutputContent": json.dumps(_terminal(
            error_type=unsafe_error_type,
        )),
        "StandardErrorContent": "opaque failure",
    })
    assert completed.returncode == 0, completed.stderr
    assert unsafe_error_type not in completed.stdout
    diagnostic = json.loads(completed.stdout)
    assert diagnostic["terminal"]["error_type"] is None
    assert diagnostic["failure_category"] == (
        "runtime_terminal_nonoperational"
    )
