"""Contracts for redacted SSM failure diagnostics."""

import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path("deploy/ec2/shadow_invocation_diagnostic.py")
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "diagnostic-test"
COMMAND_ID = "00000000-0000-0000-0000-000000000001"


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


def _run(invocation):
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
            "stop",
            "--command-id",
            COMMAND_ID,
        ],
        input=json.dumps(invocation),
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


def test_safe_failed_terminal_is_reduced_to_an_allowlisted_summary():
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
        "error_type": "ReadOnlyBoundaryError",
        "cycles": 2,
        "db_reopens": 1,
        "resources_closed": True,
        "elapsed_seconds": 121.0,
    }
    assert "side_effects" not in diagnostic["terminal"]


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
