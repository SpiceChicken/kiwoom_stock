"""Static contracts for the bounded, no-order shadow activation plane."""

import json
from contextlib import nullcontext
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

from kiwoom_stock.application.shadow_worker import (
    SHADOW_CONTINUOUS_EVIDENCE_SCHEMA_VERSION,
    SHADOW_EVIDENCE_SCHEMA_VERSION,
    CalendarDecision,
    ShadowExecutionReceipt,
    ShadowRunResult,
    run_shadow_continuous,
)
from kiwoom_stock.application.execution import (
    ActivationTuple,
    ExecutionMode,
    ExecutionPolicy,
)
from kiwoom_stock.domain.models import PhysicalContinuityEvidence
from kiwoom_stock.domain.models import ShadowDecisionTelemetry
from kiwoom_stock.application.swing_shadow import SwingShadowEvidence
from deploy.shadow_schedule_observation import CRON_CONTRACT


SCRIPT = Path("deploy/ec2/shadow_worker_control.sh")
VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")
DOCUMENT = Path("deploy/ssm/shadow-worker-document.yaml")
WORKFLOW = Path(".github/workflows/cd-shadow-worker-activation.yml")
POLICY = Path("deploy/iam/github-shadow-activation-policy.json.example")
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "continuous-test"


def _continuity(source="initial", previous=None, depth=0):
    return PhysicalContinuityEvidence(1, source, previous, depth)


def _api_counts():
    return {
        "token": 1,
        "stock_basic": 1,
        "stock_chart_5m": 1,
        "proxy_chart_60m": 1,
        "stock_strength": 1,
        "stock_orderbook": 1,
    }


def _local_counts():
    return {
        "status": 1,
        "paper_buy": 0,
        "paper_sell": 0,
        "error": 0,
        "critical": 0,
    }


def _decision_telemetry():
    return ShadowDecisionTelemetry(
        market_regime="NEUTRAL",
        strategy_reason_code="JERK_NON_POSITIVE",
        strategy_intent="NO_ENTRY_SIGNAL",
        paper_action="HOLD",
        position_before="FLAT",
        trading_window="OPEN",
        session_phase="ENTRY",
        net_force_band="POSITIVE",
        current_velocity_band="POSITIVE",
        thrust_band="FROM_1_0_TO_1_5",
        jerk_band="NEUTRAL",
        strength_band="ABOVE_100",
        trend_rsi_band="NEUTRAL",
        price_vwap_relation="ABOVE",
    )


def _oneshot_evidence(**updates):
    swing_shadow_evidence = updates.pop("swing_shadow_evidence", None)
    result = ShadowRunResult(
        status="PASS",
        mode="shadow-once",
        kst_date="2026-08-08",
        calendar="OPEN",
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        stock_code="005930",
        proxy_code="069500",
        cycles=1,
        http_attempts=6,
        api_counts=_api_counts(),
        db_identity="/var/lib/kiwoom/shadow-trades.db",
        resources_closed=True,
        side_effects={
            "broker_orders": False,
            "account": False,
            "oauth_revoke": False,
            "slack": False,
            "gemini": False,
            "s3": False,
            "reports": False,
        },
        local_counts=_local_counts(),
        continuity=_continuity(),
        decision_telemetry=_decision_telemetry(),
        swing_shadow_evidence=swing_shadow_evidence,
    ).to_safe_dict()
    result.update(updates)
    return result


def test_producers_match_literal_evidence_keysets_including_optional_swing():
    oneshot = _oneshot_evidence()
    assert set(oneshot) == {
        "schema_version", "status", "mode", "kst_date", "calendar",
        "source_sha", "image_digest", "activation_id", "stock_code",
        "proxy_code", "cycles", "http_attempts", "api_counts", "db_identity",
        "resources_closed", "side_effects", "local_counts", "continuity",
        "decision_telemetry",
    }

    swing = SwingShadowEvidence(
        snapshot_id="snapshot-1",
        input_hash="1" * 64,
        legacy_output_hash="2" * 64,
        candidate_output_hash="3" * 64,
        candidate_enabled=True,
        candidate_database_path="/var/lib/kiwoom/swing-candidate.sqlite3",
        candidate_portfolio_id="swing-paper-v1",
    )
    with_swing = _oneshot_evidence(swing_shadow_evidence=swing)
    assert set(with_swing) == set(oneshot) | {"swing_shadow_evidence"}
    assert set(with_swing["swing_shadow_evidence"]) == {
        "snapshot_id", "input_hash", "legacy_output_hash",
        "candidate_output_hash", "candidate_enabled",
        "candidate_database_path", "candidate_portfolio_id", "side_effects",
    }


def _cycle_evidence(**updates):
    result = {
        "schema_version": SHADOW_CONTINUOUS_EVIDENCE_SCHEMA_VERSION,
        "event": "cycle",
        "status": "PASS",
        "mode": "shadow-continuous",
        "kst_date": "2026-08-08",
        "calendar": "OPEN",
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "activation_id": ACTIVATION_ID,
        "stock_code": "005930",
        "proxy_code": "069500",
        "cycle_index": 1,
        "cycles": 1,
        "http_attempts": 6,
        "api_counts": _api_counts(),
        "local_counts": _local_counts(),
        "db_identity": "/var/lib/kiwoom/shadow-trades.db",
        "elapsed_seconds": 0.25,
        "interval_seconds": 60.0,
        "cycle_start_elapsed_seconds": 0.0,
        "observed_interval_seconds": None,
        "db_reopened": False,
        "db_reopens": 0,
        "continuity": _continuity().to_safe_dict(),
        "decision_telemetry": _decision_telemetry().to_safe_dict(),
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
    }
    result.update(updates)
    return result


def _terminal_evidence(**updates):
    result = {
        "schema_version": SHADOW_CONTINUOUS_EVIDENCE_SCHEMA_VERSION,
        "event": "terminal", "status": "DEADLINE",
        "mode": "shadow-continuous", "source_sha": SOURCE_SHA,
        "image_digest": IMAGE, "activation_id": ACTIVATION_ID,
        "cycles": 15, "elapsed_seconds": 900.0,
        "first_cycle_start_elapsed_seconds": 0.0,
        "second_cycle_start_elapsed_seconds": 60.0,
        "second_cycle_interval_seconds": 60.0,
        "minimum_cycle_interval_seconds": 60.0, "db_reopens": 14,
        "resources_closed": True,
        "side_effects": {
            "broker_orders": False, "account": False, "oauth_revoke": False,
            "slack": False, "gemini": False, "s3": False, "reports": False,
        },
        "reason": "run-deadline",
    }
    result.update(updates)
    return result


def _run_sourced(command, *args):
    validator = VALIDATOR.resolve()
    adapter = (
        f'validate_safe_evidence() {{ python3 "{validator}" '
        '--mode "$1" --event "$2" --source-sha "$3" --image-digest "$4" '
        '--activation-id "$5" --input-format json-lines --output accepted-record; }; '
        f'validate_safe_terminal_diagnostic() {{ python3 "{validator}" '
        '--mode shadow-continuous --event terminal --source-sha "$1" '
        '--image-digest "$2" --activation-id "$3" --input-format json-lines '
        '--output accepted-record --terminal-policy diagnostic; }; '
    )
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {adapter}{command}', "test", str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_host_cycle_parser(evidence, tmp_path):
    del tmp_path
    return _run_validator(evidence, "shadow-continuous", "cycle", "json-lines")


def _run_host_oneshot_parser(evidence, tmp_path):
    del tmp_path
    return _run_validator(evidence, "shadow-once", "oneshot", "json-lines")


def _run_validator(evidence, mode, event, input_format, output="accepted-record"):
    payload = evidence if isinstance(evidence, str) else json.dumps(evidence)
    if input_format == "ssm-invocation" and not isinstance(evidence, str):
        payload = json.dumps({
            "Status": "Success", "ResponseCode": 0,
            "StandardOutputContent": payload,
        })
    return subprocess.run(
        [sys.executable, str(VALIDATOR), "--mode", mode, "--event", event,
         "--source-sha", SOURCE_SHA, "--image-digest", IMAGE,
         "--activation-id", ACTIVATION_ID, "--input-format", input_format,
         "--output", output],
        input=payload, check=False, capture_output=True, text=True,
    )


def _run_workflow_parser(evidence, desired_state):
    mode = "shadow-once" if desired_state == "oneshot" else "shadow-continuous"
    event = {"oneshot": "oneshot", "continuous": "cycle", "stop": "terminal"}[desired_state]
    return _run_validator(
        evidence, mode, event, "ssm-invocation", "activation-summary"
    )


def _run_workflow_cycle_parser(evidence):
    return _run_workflow_parser(evidence, "continuous")


def _run_activation_evidence_builder(
    document_version="7", worker_hash="c" * 64, validator_hash="e" * 64
):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item.get("name") == "Execute bounded shadow action"
    )
    parser = step["run"].rsplit("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    return subprocess.run(
        [sys.executable, "-", SOURCE_SHA, IMAGE, "123", ACTIVATION_ID,
         "oneshot", "00000000-0000-0000-0000-000000000001",
         document_version, worker_hash, validator_hash, "d" * 64,
         '{"runtime_status":"PASS","ssm_status":"Success",'
         '"ssm_response_code":0}'],
        input=parser, check=False, capture_output=True, text=True,
    )


def _run_activation_poll(
    statuses: Path, tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item.get("name") == "Execute bounded shadow action"
    )
    script = step["run"]
    start = script.index("status=Pending")
    end = script.index('invocation_file="${RUNNER_TEMP}', start)
    poll = script[start:end].replace("sleep 10", ":")
    poll += 'printf \'%s\\n\' "${status}"; cat "${COUNT_FILE}"\n'
    tools = tmp_path / "tools"
    tools.mkdir()
    aws = tools / "aws"
    aws.write_text(
        "#!/usr/bin/env bash\n"
        'count="$(cat "${COUNT_FILE}")"\n'
        'count="$((count + 1))"\n'
        'printf \'%s\\n\' "${count}" >"${COUNT_FILE}"\n'
        'sed -n "${count}p" "${STATUS_FILE}"\n',
        encoding="utf-8",
    )
    aws.chmod(0o755)
    count_file = tmp_path / "count"
    count_file.write_text("0\n", encoding="utf-8")
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "STATUS_FILE": str(statuses), "COUNT_FILE": str(count_file),
        "EC2_INSTANCE_ID": "i-test", "command_id": "unused",
    })
    return subprocess.run(
        ["bash", "-c", poll], env=environment, check=False,
        capture_output=True, text=True,
    )


def test_shadow_host_executor_is_shell_valid_and_bounded():
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    text = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "validate_image_revision",
        "validate_secret_metadata",
        "validate_instance_identity",
        "--abort-on-container-exit",
        "--detach --no-build app",
        "validate_safe_evidence",
        "validate_container_identity",
        "docker stop --time 30",
        "--exit-code-from app",
        "side_effects=none",
        "volume=preserved",
        "flock -n",
    ):
        assert marker in text
    for forbidden in (
        "docker compose down",
        "docker volume rm",
        "docker volume prune",
        "docker rm -f",
        "--remove-orphans",
        "/api/dostk/ordr",
        "AccountService",
        "OAuth revoke",
        "slack-sdk",
        "google.generativeai",
        "boto3",
    ):
        assert forbidden not in text.casefold()


def test_shadow_host_executor_keeps_unvalidated_docker_progress_off_ssm_stdout():
    text = SCRIPT.read_text(encoding="utf-8")

    assert (
        'timeout "${PULL_TIMEOUT_SECONDS}" docker pull "${image}" \\\n'
        '            >"${PULL_LOG}" 2>&1'
    ) in text
    assert (
        '--file "${compose_file}" up --abort-on-container-exit --exit-code-from app \\\n'
        '        >/dev/null 2>&1 \\'
    ) in text
    assert (
        '--file "${compose_file}" up --detach --no-build app \\\n'
        '        >/dev/null 2>&1 \\'
    ) in text

    progress = json.dumps({
        "Status": "Success",
        "ResponseCode": 0,
        "StandardOutputContent": (
            "[+] Running 1/1\n" + json.dumps(_oneshot_evidence())
        ),
    })
    rejected = _run_validator(
        progress, "shadow-once", "oneshot", "ssm-invocation"
    )
    assert rejected.returncode != 0
    assert rejected.stderr == "shadow evidence invalid: record_json_invalid\n"


def test_shadow_ssm_document_has_exact_bounded_actions_and_no_secret_parameters():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    parameters = document["parameters"]
    assert parameters["DesiredState"]["allowedValues"] == [
        "oneshot", "continuous", "stop", "telemetry-export-page",
    ]
    assert set(parameters) == {
        "DesiredState",
        "ImageDigest",
        "SourceSha",
        "ActivationId",
        "ComposeShadowSha256",
        "ExpectedWorkerSha256",
        "ExpectedValidatorSha256",
        "ExpectedShadowDocumentSha256",
        "ExpectedInstanceId",
        "Region",
        "TelemetrySessionDateKst",
        "TelemetryOffset",
        "TelemetryLength",
    }
    text = DOCUMENT.read_text(encoding="utf-8")
    assert "/usr/local/sbin/kiwoom-shadow-worker" in text
    assert "AWS-RunShellScript" not in text
    assert "SecureString" not in text
    assert "AppKey" not in text
    assert "SecretKey" not in text
    command = document["mainSteps"][0]["inputs"]["runCommand"][0]
    assert command.index("exec 9>/run/lock/kiwoom-stock-shadow.lock") < command.index(
        "flock -x -w 240 9"
    ) < command.index("exec /usr/local/sbin/kiwoom-shadow-worker")
    assert command.count("--inherited-lock-fd 9") == 3


def test_activation_prelock_prevents_old_inode_execution(tmp_path):
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    command = document["mainSteps"][0]["inputs"]["runCommand"][0]
    lock = tmp_path / "shadow.lock"
    worker = tmp_path / "worker"
    replacement = tmp_path / "replacement"
    worker.write_text("#!/usr/bin/env bash\necho OLD\n", encoding="utf-8")
    replacement.write_text("#!/usr/bin/env bash\necho NEW\n", encoding="utf-8")
    worker.chmod(0o755)
    replacement.chmod(0o755)
    command = command.replace(
        "/run/lock/kiwoom-stock-shadow.lock", str(lock)
    ).replace("/usr/local/sbin/kiwoom-shadow-worker", str(worker))
    holder = subprocess.Popen(
        ["bash", "-c", 'exec 8>"$1"; flock -x 8; echo READY; read -r _',
         "holder", str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    assert holder.stdout is not None and holder.stdout.readline().strip() == "READY"
    environment = dict(**__import__("os").environ)
    environment.update({
        "SSM_DesiredState": "stop", "SSM_ImageDigest": IMAGE,
        "SSM_SourceSha": SOURCE_SHA, "SSM_ActivationId": ACTIVATION_ID,
        "SSM_ComposeShadowSha256": "0" * 64,
        "SSM_ExpectedWorkerSha256": "c" * 64,
        "SSM_ExpectedValidatorSha256": "e" * 64,
        "SSM_ExpectedShadowDocumentSha256": "d" * 64,
        "SSM_ExpectedInstanceId": "i-0e42e09d6c087ba29",
        "SSM_Region": "ap-northeast-2",
    })
    activation = subprocess.Popen(
        ["bash", "-c", command], env=environment,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    replacement.replace(worker)
    assert holder.stdin is not None
    holder.stdin.write("release\n")
    holder.stdin.flush()
    holder.stdin.close()
    assert holder.wait(timeout=5) == 0
    stdout, stderr = activation.communicate(timeout=5)
    assert activation.returncode == 0, stderr
    assert stdout.strip() == "NEW"


def test_worker_rejects_spoofed_inherited_lock_fd(tmp_path):
    approved = tmp_path / "approved.lock"
    wrong = tmp_path / "wrong.lock"
    environment = dict(**__import__("os").environ)
    environment["KIWOOM_SHADOW_LOCK_FILE"] = str(approved)
    valid = subprocess.run(
        ["bash", "-c", 'source "$1"; exec 8>"$2"; flock -x 8; acquire_activation_lock 8',
         "test", str(SCRIPT), str(approved)],
        env=environment, capture_output=True, text=True,
    )
    assert valid.returncode == 0, valid.stderr
    spoofed = subprocess.run(
        ["bash", "-c", 'source "$1"; exec 8>"$2"; flock -x 8; acquire_activation_lock 8',
         "test", str(SCRIPT), str(wrong)],
        env=environment, capture_output=True, text=True,
    )
    assert spoofed.returncode != 0
    assert "does not reference the approved lock" in spoofed.stderr


def test_shadow_workflow_is_protected_and_never_receives_kiwoom_secrets():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [
        {"cron": "50 23 * * 0-4"},
        {"cron": "35 6 * * 1-5"},
    ]
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "source_sha",
        "image_digest",
        "build_run_id",
        "compose_shadow_sha256",
        "activation_id",
        "desired_state",
        "worker_sha256",
        "validator_sha256",
        "shadow_document_sha256",
        "status_notification",
    }
    assert triggers["workflow_dispatch"]["inputs"]["build_run_id"]["required"] is False
    assert triggers["workflow_dispatch"]["inputs"]["compose_shadow_sha256"]["required"] is False
    assert triggers["workflow_dispatch"]["inputs"]["worker_sha256"]["required"] is True
    assert triggers["workflow_dispatch"]["inputs"]["validator_sha256"]["required"] is True
    assert triggers["workflow_dispatch"]["inputs"]["shadow_document_sha256"]["required"] is True
    assert workflow["permissions"] == {}
    assert workflow["concurrency"]["cancel-in-progress"] is False
    job = workflow["jobs"]["activate"]
    assert job["environment"] == "production-shadow"
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    text = WORKFLOW.read_text(encoding="utf-8")
    serialized_workflow = json.dumps(workflow)
    assert text.count("secrets.KIWOOM_SHADOW_SLACK_WEBHOOK_URL") == 2
    assert text.count("secrets.CONFIG_JSON") == 2
    assert serialized_workflow.count(
        "secrets.KIWOOM_SHADOW_SLACK_WEBHOOK_URL"
    ) == 2
    assert serialized_workflow.count("secrets.CONFIG_JSON") == 2
    assert re.search(r"secrets\s*\[", serialized_workflow) is None
    assert text.count("CONFIG_JSON: ${{ secrets.CONFIG_JSON }}") == 2
    slack_steps = [
        step for step in job["steps"]
        if step.get("name") in {
            "Preflight protected Slack status boundary",
            "Notify protected shadow status",
        }
    ]
    assert len(slack_steps) == 2
    assert all(
        step["env"]["CONFIG_JSON"] == "${{ secrets.CONFIG_JSON }}"
        for step in slack_steps
    )
    assert all(
        "CONFIG_JSON" not in step.get("env", {})
        for step in job["steps"] if step not in slack_steps
    )

    def config_reference_paths(value, path=()):
        if isinstance(value, str):
            return [path] if "secrets.CONFIG_JSON" in value else []
        if isinstance(value, dict):
            return [
                reference
                for key, nested in value.items()
                for reference in config_reference_paths(nested, (*path, key))
            ]
        if isinstance(value, list):
            return [
                reference
                for index, nested in enumerate(value)
                for reference in config_reference_paths(nested, (*path, index))
            ]
        return []

    config_references = config_reference_paths(workflow)
    expected_config_references = [
        (
            "jobs", "activate", "steps", job["steps"].index(step),
            "env", "CONFIG_JSON",
        )
        for step in slack_steps
    ]
    assert config_references == expected_config_references
    assert "secrets.STRATEGY_CONFIG_JSON" not in text
    assert "KIWOOM_APP_KEY" not in text
    assert "KIWOOM_SECRET_KEY" not in text
    assert "ssm get-parameter" not in text.lower()
    assert "ssm get-parameters" not in text.lower()
    assert "DesiredState=${DESIRED_STATE}" in text
    assert "ExpectedWorkerSha256=${WORKER_SHA256}" in text
    assert "ExpectedValidatorSha256=${VALIDATOR_SHA256}" in text
    assert "ExpectedShadowDocumentSha256=${SHADOW_DOCUMENT_SHA256}" in text
    assert "deploy/ec2/shadow_runtime_evidence.py" in text
    assert text.count("ref: ${{ github.sha }}") == 1
    assert text.count("path: .shadow-control-plane") == 1
    assert (
        "python3 .shadow-control-plane/deploy/ec2/"
        "shadow_invocation_diagnostic.py" in text
    )
    assert text.count(
        ".shadow-control-plane/deploy/notify_shadow_status.py"
    ) == 3
    assert "--input-format ssm-invocation" in text
    assert "def valid_continuity" not in text
    assert 'if [[ "${DESIRED_STATE}" == stop ]]' in text
    assert 'actual_compose_shadow_sha256="$(sha256sum compose.shadow.yaml' in text
    assert '[[ -z "${BUILD_RUN_ID}${COMPOSE_SHADOW_SHA256}" ]]' in text
    assert "shadow_activation_preflight_failed category=" in text
    assert "compose_shadow_hash_mismatch" in text
    assert "image_entrypoint_mismatch" in text
    assert '"${command_id}" "${document_version}" \\' in text
    assert '"${WORKER_SHA256}" "${VALIDATOR_SHA256}" \\' in text
    assert '"document_version": document_version' in text
    assert '"worker_sha256": worker_sha256' in text
    assert '"validator_sha256": validator_sha256' in text
    assert '"shadow_document_sha256": shadow_document_sha256' in text
    assert 're.fullmatch(r"[1-9][0-9]*", document_version)' in text
    assert 'Success|Failed|Cancelled|TimedOut) break' in text
    assert 'TimedOut|Cancelling) break' not in text
    assert '--query ResponseCode --output text' not in text
    assert '[[ "${status}" == Success' not in text

    schedule_steps = [
        step for step in job["steps"]
        if step.get("name") == "Observe scheduled shadow run start"
    ]
    assert len(schedule_steps) == 1
    schedule_step = schedule_steps[0]
    assert schedule_step["if"] == "github.event_name == 'schedule'"
    assert schedule_step["continue-on-error"] is True
    assert schedule_step["timeout-minutes"] == 1
    assert schedule_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "EVENT_SCHEDULE": "${{ github.event.schedule }}",
        "DESIRED_STATE": "${{ steps.resolve.outputs.desired_state }}",
    }
    schedule_run = schedule_step["run"]
    assert (
        '"/repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}"'
        in schedule_run
    )
    assert schedule_run.count("gh api --method GET") == 1
    assert "actor" not in schedule_run
    assert "html_url" not in schedule_run
    assert "head_commit" not in schedule_run
    assert "head_branch: .head_branch" in schedule_run
    assert (
        ".shadow-control-plane/deploy/shadow_schedule_observation.py"
        in schedule_run
    )
    current_run_path = (
        'current_run_file="${RUNNER_TEMP}/'
        'shadow-current-run-${GITHUB_RUN_ID}.json"'
    )
    observation_path = (
        'observation_file="${RUNNER_TEMP}/'
        'shadow-schedule-observation-${GITHUB_RUN_ID}.json"'
    )
    remove_stale = 'rm -f -- "${current_run_file}" "${observation_file}"'
    assert current_run_path in schedule_run
    assert observation_path in schedule_run
    assert remove_stale in schedule_run
    assert schedule_run.index(remove_stale) < schedule_run.index(
        "gh api --method GET"
    )
    assert '--output "${observation_file}"' in schedule_run
    assert '--summary "${GITHUB_STEP_SUMMARY}"' in schedule_run
    assert "- observation: `invalid`" in schedule_run

    notify = next(
        step for step in job["steps"]
        if step.get("name") == "Notify protected shadow status"
    )
    assert notify["env"]["EVENT_SCHEDULE"] == "${{ github.event.schedule }}"
    assert '[[ "${GITHUB_EVENT_NAME}" == schedule ]]' in notify["run"]
    assert (
        '--schedule-observation "${RUNNER_TEMP}/'
        'shadow-schedule-observation-${GITHUB_RUN_ID}.json"'
        in notify["run"]
    )
    assert '--expected-run-id "${GITHUB_RUN_ID}"' in notify["run"]
    assert '--expected-cron "${EVENT_SCHEDULE}"' in notify["run"]
    upload = next(
        step for step in job["steps"]
        if step.get("name") == "Upload bounded shadow evidence"
    )
    assert (
        "${{ runner.temp }}/shadow-schedule-observation-"
        "${{ github.run_id }}.json"
    ) in upload["with"]["path"]
    assert upload["with"]["retention-days"] == 14


def test_schedule_cron_action_contract_is_in_exact_parity():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    workflow_crons = {item["cron"] for item in triggers["schedule"]}
    resolve = next(
        step for step in workflow["jobs"]["activate"]["steps"]
        if step.get("name") == "Resolve effective shadow activation tuple"
    )
    resolve_pairs = re.findall(
        r"'([^']+)'\) desired_state=(oneshot|continuous|stop) ;;",
        resolve["run"],
    )
    resolve_contract = dict(resolve_pairs)
    helper_contract = {
        cron: str(contract["desired_state"])
        for cron, contract in CRON_CONTRACT.items()
    }

    assert len(resolve_pairs) == len(resolve_contract)
    assert workflow_crons == set(resolve_contract) == set(helper_contract)
    assert resolve_contract == helper_contract


def test_activation_poll_treats_cancelling_as_nonterminal_until_success(tmp_path):
    statuses = tmp_path / "statuses"
    statuses.write_text("Cancelling\nSuccess\n", encoding="utf-8")
    completed = _run_activation_poll(statuses, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["Success", "2"]


def test_activation_poll_reaches_cancelled_and_validator_rejects_failure(tmp_path):
    statuses = tmp_path / "statuses"
    statuses.write_text("Cancelling\nCancelled\n", encoding="utf-8")
    completed = _run_activation_poll(statuses, tmp_path)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == ["Cancelled", "2"]
    invocation = json.dumps({
        "Status": "Cancelled", "ResponseCode": 1,
        "StandardOutputContent": json.dumps(_oneshot_evidence()),
    })
    rejected = _run_validator(
        invocation, "shadow-once", "oneshot", "ssm-invocation"
    )
    assert rejected.returncode == 1
    assert rejected.stderr == "shadow evidence invalid: invocation_status_invalid\n"


def test_stop_pre_oidc_rejects_non_main_source_before_checkout_python(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item["name"] == "Validate immutable shadow tuple and candidate run"
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    marker = tmp_path / "executed"
    for name, body in {
        "gh": "#!/usr/bin/env bash\nprintf 'diverged\\t%s\\t%s\\n' \"$SOURCE_SHA\" \"$SOURCE_SHA\"\n",
        "git": "#!/usr/bin/env bash\ntouch \"$EXECUTION_MARKER\"\nprintf '%s\\n' \"$SOURCE_SHA\"\n",
        "sha256sum": "#!/usr/bin/env bash\ntouch \"$EXECUTION_MARKER\"\nprintf '%s  file\\n' \"$VALIDATOR_SHA256\"\n",
        "python3": "#!/usr/bin/env bash\ntouch \"$EXECUTION_MARKER\"\nexit 0\n",
    }.items():
        path = tools / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "EXECUTION_MARKER": str(marker),
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "f" * 40,
        "GITHUB_REPOSITORY": "SpiceChicken/kiwoom_stock",
        "SOURCE_SHA": SOURCE_SHA, "IMAGE_DIGEST": IMAGE,
        "BUILD_RUN_ID": "", "COMPOSE_SHADOW_SHA256": "",
        "ACTIVATION_ID": ACTIVATION_ID, "DESIRED_STATE": "stop",
        "WORKER_SHA256": "c" * 64, "VALIDATOR_SHA256": "d" * 64,
        "SHADOW_DOCUMENT_SHA256": "e" * 64,
    })
    completed = subprocess.run(
        ["bash", "-c", step["run"]], env=environment, check=False,
        capture_output=True, text=True,
    )
    assert completed.returncode != 0
    assert (
        "shadow_activation_preflight_failed "
        "category=github_compare_status_invalid"
    ) in completed.stderr
    assert not marker.exists()


def test_stop_pre_oidc_accepts_proven_old_main_ancestor(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item["name"] == "Validate immutable shadow tuple and candidate run"
    )
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in {
        "gh": "#!/usr/bin/env bash\nprintf 'ahead\\t%s\\t%s\\n' \"$SOURCE_SHA\" \"$SOURCE_SHA\"\n",
        "git": (
            "#!/usr/bin/env bash\n"
            "if [[ \"$*\" == '-C .shadow-control-plane rev-parse HEAD' ]]; then\n"
            "  printf '%s\\n' \"$GITHUB_SHA\"\n"
            "elif [[ \"$*\" == '-C .shadow-control-plane status --short' ]]; then\n"
            "  exit 0\n"
            "else\n"
            "  printf '%s\\n' \"$SOURCE_SHA\"\n"
            "fi\n"
        ),
        "sha256sum": "#!/usr/bin/env bash\nprintf '%s  file\\n' \"$VALIDATOR_SHA256\"\n",
        "python3": "#!/usr/bin/env bash\nexit 0\n",
    }.items():
        path = tools / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": "f" * 40,
        "GITHUB_REPOSITORY": "SpiceChicken/kiwoom_stock",
        "SOURCE_SHA": SOURCE_SHA, "IMAGE_DIGEST": IMAGE,
        "BUILD_RUN_ID": "", "COMPOSE_SHADOW_SHA256": "",
        "ACTIVATION_ID": ACTIVATION_ID, "DESIRED_STATE": "stop",
        "WORKER_SHA256": "c" * 64, "VALIDATOR_SHA256": "d" * 64,
        "SHADOW_DOCUMENT_SHA256": "e" * 64,
    })
    completed = subprocess.run(
        ["bash", "-c", step["run"]], env=environment, check=False,
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_activation_evidence_binds_attested_numeric_version_and_pair_hashes():
    completed = _run_activation_evidence_builder()
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["document_version"] == "7"
    assert evidence["worker_sha256"] == "c" * 64
    assert evidence["validator_sha256"] == "e" * 64
    assert evidence["shadow_document_sha256"] == "d" * 64
    assert evidence["ssm_status"] == "Success"
    assert type(evidence["ssm_response_code"]) is int
    assert evidence["ssm_response_code"] == 0
    assert _run_activation_evidence_builder(document_version="$LATEST").returncode != 0
    assert _run_activation_evidence_builder(worker_hash="bad").returncode != 0
    assert _run_activation_evidence_builder(validator_hash="bad").returncode != 0


def test_one_shot_schema_3_production_serializer_round_trips_both_consumers(
    tmp_path,
):
    evidence = _oneshot_evidence()
    assert SHADOW_EVIDENCE_SCHEMA_VERSION == 3
    assert evidence["schema_version"] == 3
    host = _run_host_oneshot_parser(evidence, tmp_path)
    workflow = _run_workflow_parser(evidence, "oneshot")
    assert host.returncode == 0, host.stderr
    assert workflow.returncode == 0, workflow.stderr

    continuity = evidence["continuity"]
    assert isinstance(continuity, dict)
    malformed = (
        _oneshot_evidence(schema_version=1),
        _oneshot_evidence(continuity=None),
        _oneshot_evidence(continuity={**continuity, "hydration_source": "unknown"}),
        _oneshot_evidence(continuity={**continuity, "history_depth": True}),
        _oneshot_evidence(continuity={**continuity, "baseline_sample_index": 3}),
    )
    for candidate in malformed:
        assert _run_host_oneshot_parser(candidate, tmp_path).returncode != 0
        assert _run_workflow_parser(candidate, "oneshot").returncode != 0


def test_one_shot_closed_serializer_has_exact_zero_cycle_contract(tmp_path):
    closed = ShadowRunResult(
        status="CLOSED",
        mode="shadow-once",
        kst_date="2026-08-08",
        calendar="CLOSED",
        source_sha=SOURCE_SHA,
        image_digest=IMAGE,
        activation_id=ACTIVATION_ID,
        stock_code="005930",
        proxy_code="069500",
        cycles=0,
        http_attempts=0,
        api_counts={},
        db_identity=None,
        resources_closed=True,
        side_effects={
            "broker_orders": False,
            "account": False,
            "oauth_revoke": False,
            "slack": False,
            "gemini": False,
            "s3": False,
            "reports": False,
        },
        local_counts={},
        continuity=None,
    ).to_safe_dict()
    assert _run_host_oneshot_parser(closed, tmp_path).returncode == 0
    assert _run_workflow_parser(closed, "oneshot").returncode == 0


def test_host_evidence_parser_rejects_stale_and_malformed_first_ticks(tmp_path):
    del tmp_path
    valid = _run_validator(
        _cycle_evidence(), "shadow-continuous", "cycle", "json-lines"
    )
    assert valid.returncode == 0, valid.stderr

    stale = _run_validator(
        _cycle_evidence(source_sha="c" * 40),
        "shadow-continuous", "cycle", "json-lines",
    )
    assert stale.returncode != 0

    malformed = _run_validator(
        _cycle_evidence(http_attempts=24),
        "shadow-continuous", "cycle", "json-lines",
    )
    assert malformed.returncode != 0

    for api_counts, local_counts in (
        ({}, _cycle_evidence()["local_counts"]),
        ({**_cycle_evidence()["api_counts"], "extra": 1}, _cycle_evidence()["local_counts"]),
        (_cycle_evidence()["api_counts"], {}),
        (_cycle_evidence()["api_counts"], {**_cycle_evidence()["local_counts"], "extra": 0}),
    ):
        rejected = _run_validator(
            _cycle_evidence(api_counts=api_counts, local_counts=local_counts),
            "shadow-continuous", "cycle", "json-lines",
        )
        assert rejected.returncode != 0


def test_host_and_workflow_accept_all_safe_local_cycle_outcomes(tmp_path):
    for paper_buy, paper_sell in ((0, 0), (1, 0), (0, 1)):
        local_counts = {
            **_cycle_evidence()["local_counts"],
            "paper_buy": paper_buy,
            "paper_sell": paper_sell,
        }
        evidence = _cycle_evidence(local_counts=local_counts)
        host = _run_host_cycle_parser(evidence, tmp_path)
        workflow = _run_workflow_cycle_parser(evidence)
        assert host.returncode == 0, host.stderr
        assert workflow.returncode == 0, workflow.stderr


def test_host_and_workflow_reject_non_integer_or_invalid_cycle_schema(tmp_path):
    valid = _cycle_evidence()
    invalid = []
    for field in ("schema_version", "cycle_index", "cycles", "http_attempts", "db_reopens"):
        invalid.append(_cycle_evidence(**{field: True}))
        invalid.append(_cycle_evidence(**{field: 1.0}))
    invalid.extend(
        (
            _cycle_evidence(schema_version=3),
            _cycle_evidence(api_counts={**valid["api_counts"], "token": True}),
            _cycle_evidence(api_counts={**valid["api_counts"], "token": 1.0}),
            _cycle_evidence(local_counts={**valid["local_counts"], "status": True}),
            _cycle_evidence(local_counts={**valid["local_counts"], "paper_buy": 1.0}),
            _cycle_evidence(local_counts={**valid["local_counts"], "paper_buy": 2}),
            _cycle_evidence(local_counts={**valid["local_counts"], "paper_buy": 1, "paper_sell": 1}),
            _cycle_evidence(local_counts={key: value for key, value in valid["local_counts"].items() if key != "critical"}),
            _cycle_evidence(local_counts={**valid["local_counts"], "extra": 0}),
        )
    )
    for evidence in invalid:
        assert _run_host_cycle_parser(evidence, tmp_path).returncode != 0
        assert _run_workflow_cycle_parser(evidence).returncode != 0


def test_host_and_workflow_validate_nested_continuity_schema_3(tmp_path):
    persisted = PhysicalContinuityEvidence(
        schema_version=1,
        hydration_source="persisted",
        previous_observed_at=datetime(2026, 8, 8, 10, 0, tzinfo=timezone.utc),
        history_depth=2,
    ).to_safe_dict()
    valid = _cycle_evidence(continuity=persisted)
    assert _run_host_cycle_parser(valid, tmp_path).returncode == 0
    assert _run_workflow_cycle_parser(valid).returncode == 0

    invalid_continuity = (
        None,
        {**persisted, "schema_version": True},
        {**persisted, "schema_version": 2},
        {**persisted, "hydration_source": "unknown"},
        {**persisted, "previous_observed_at": "2026-08-08T10:00:00"},
        {**persisted, "previous_observed_at": True},
        {**persisted, "history_depth": True},
        {**persisted, "history_depth": -1},
        {**persisted, "baseline_source": "synthetic_event_time"},
        {**persisted, "baseline_sample_index": 3},
        {**persisted, "baseline_sample_index": True},
        {**persisted, "baseline_time_estimated": False},
        {**persisted, "extra": 1},
    )
    for continuity in invalid_continuity:
        evidence = _cycle_evidence(continuity=continuity)
        assert _run_host_cycle_parser(evidence, tmp_path).returncode != 0
        assert _run_workflow_cycle_parser(evidence).returncode != 0


def test_host_and_workflow_validate_continuous_terminal_reopen_evidence(tmp_path):
    del tmp_path
    terminal = _terminal_evidence()
    host = _run_validator(
        terminal, "shadow-continuous", "terminal", "json-lines"
    )
    workflow_result = _run_workflow_parser(terminal, "stop")
    assert host.returncode == 0, host.stderr
    assert workflow_result.returncode == 0, workflow_result.stderr

    for updates in (
        {"db_reopens": 13},
        {"second_cycle_interval_seconds": 59.0},
        {"minimum_cycle_interval_seconds": 59.0},
        {"second_cycle_start_elapsed_seconds": 30.0},
        {"schema_version": 1},
    ):
        malformed = {**terminal, **updates}
        assert _run_validator(
            malformed, "shadow-continuous", "terminal", "json-lines"
        ).returncode != 0
        failed_workflow = _run_workflow_parser(malformed, "stop")
        assert failed_workflow.returncode != 0


def test_host_accepts_calendar_closed_continuous_terminal(tmp_path):
    closed = _terminal_evidence(
        status="CLOSED",
        reason="calendar-closed",
        cycles=0,
        elapsed_seconds=1.0,
        first_cycle_start_elapsed_seconds=None,
        second_cycle_start_elapsed_seconds=None,
        second_cycle_interval_seconds=None,
        minimum_cycle_interval_seconds=None,
        db_reopens=0,
    )
    payload = tmp_path / "closed-terminal.json"
    payload.write_text(json.dumps(closed), encoding="utf-8")
    completed = _run_sourced(
        '''
        docker() {
            [[ "$*" == *".State.ExitCode"* ]] && echo 0 || return 1
        }
        confirm_continuous_calendar_closed "$(cat "$2")" "$3" "$4" "$5"
        ''',
        str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == closed


def test_actual_continuous_emitter_cycle_and_terminal_round_trip_consumers(
    tmp_path,
):
    now = [0.0]
    emitted = []

    class StopAfterFirstCycle:
        def __init__(self):
            self.stopped = False

        def set(self):
            self.stopped = True

        def is_set(self):
            return self.stopped

        def wait(self, timeout=None):
            now[0] += timeout or 0.0
            self.stopped = True
            return True

    receipt = ShadowExecutionReceipt(
        cycles=1,
        http_attempts=6,
        api_counts=_api_counts(),
        db_identity="/var/lib/kiwoom/shadow-trades.db",
        resources_closed=True,
        local_counts=_local_counts(),
        continuity=_continuity(),
        decision_telemetry=_decision_telemetry(),
    )
    policy = ExecutionPolicy.for_request(
        ExecutionMode.SHADOW_CONTINUOUS,
        ActivationTuple(
            source_sha=SOURCE_SHA,
            image_digest=IMAGE,
            activation_id=ACTIVATION_ID,
        ),
    )
    terminal_result = run_shadow_continuous(
        policy,
        runtime_factory=lambda *_args: type(
            "Runtime",
            (),
            {"execute_once": lambda self: receipt},
        )(),
        emit=emitted.append,
        lock_path=tmp_path / "emitter.lock",
        clock=lambda: datetime(2026, 8, 8, 1, 0, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=StopAfterFirstCycle(),
        monotonic=lambda: now[0],
        lock_factory=lambda _path: nullcontext(),
    )
    assert len(emitted) == 1
    cycle = emitted[0]
    terminal = terminal_result.to_safe_dict()

    assert set(cycle) == {
        "schema_version", "event", "status", "mode", "kst_date", "calendar",
        "source_sha", "image_digest", "activation_id", "stock_code",
        "proxy_code", "cycle_index", "cycles", "http_attempts", "api_counts",
        "local_counts", "db_identity", "elapsed_seconds", "interval_seconds",
        "cycle_start_elapsed_seconds", "observed_interval_seconds",
        "db_reopened", "db_reopens", "continuity", "decision_telemetry",
        "resources_closed", "side_effects",
    }
    assert set(terminal) == {
        "schema_version", "event", "status", "mode", "source_sha",
        "image_digest", "activation_id", "cycles", "elapsed_seconds",
        "first_cycle_start_elapsed_seconds", "second_cycle_start_elapsed_seconds",
        "second_cycle_interval_seconds", "minimum_cycle_interval_seconds",
        "db_reopens", "resources_closed", "side_effects", "reason",
    }

    assert _run_host_cycle_parser(cycle, tmp_path).returncode == 0
    assert _run_workflow_cycle_parser(cycle).returncode == 0
    host_terminal = _run_validator(
        terminal, "shadow-continuous", "terminal", "json-lines"
    )
    workflow_terminal = _run_workflow_parser(terminal, "stop")
    assert host_terminal.returncode == 0, host_terminal.stderr
    assert workflow_terminal.returncode == 0, workflow_terminal.stderr


def test_host_stop_fails_when_container_is_absent():
    completed = _run_sourced(
        'docker() { return 1; }; stop_shadow "$2" "$3" "$4"',
        IMAGE,
        SOURCE_SHA,
        ACTIVATION_ID,
    )
    assert completed.returncode != 0
    assert "container is absent" in completed.stderr


def test_host_stop_rejects_running_container_tuple_mismatch():
    completed = _run_sourced(
        '''
        docker() {
          if [[ "$1 $2" == "container inspect" ]]; then return 0; fi
          if [[ "$*" == *".Config.Image"* ]]; then echo "wrong-image"; return 0; fi
          return 1
        }
        stop_shadow "$2" "$3" "$4"
        ''',
        IMAGE,
        SOURCE_SHA,
        ACTIVATION_ID,
    )
    assert completed.returncode != 0
    assert "image mismatch" in completed.stderr


def test_host_first_tick_recheck_rejects_immediate_container_crash(tmp_path):
    payload = tmp_path / "tick.json"
    payload.write_text(json.dumps(_cycle_evidence()), encoding="utf-8")
    completed = _run_sourced(
        '''
        expected_source="$3"
        expected_image="$4"
        expected_activation="$5"
        docker() {
          if [[ "$1 $2" == "container inspect" ]]; then return 0; fi
          case "$*" in
            *".Config.Image"*) echo "$expected_image" ;;
            *"source-sha"*) echo "$expected_source" ;;
            *"image-digest"*) echo "$expected_image" ;;
            *"activation-id"*) echo "$expected_activation" ;;
            *"shadow.mode"*) echo "shadow-continuous" ;;
            *".Config.Cmd"*) printf '["python","-m","kiwoom_stock","shadow-worker","--source-sha","%s","--image-digest","%s","--activation-id","%s"]\n' "$expected_source" "$expected_image" "$expected_activation" ;;
            *".State.Running"*) echo "false" ;;
            *".State.ExitCode"*) echo "137" ;;
            *) return 1 ;;
          esac
        }
        confirm_continuous_tick "$(cat "$2")" "$3" "$4" "$5"
        ''',
        str(payload),
        SOURCE_SHA,
        IMAGE,
        ACTIVATION_ID,
    )
    assert completed.returncode != 0
    assert "not running" in completed.stderr


def test_host_cleans_exact_natural_deadline_container_for_next_activation(tmp_path):
    terminal = tmp_path / "terminal.json"
    removed = tmp_path / "removed"
    terminal.write_text(json.dumps(_terminal_evidence()), encoding="utf-8")
    completed = _run_sourced(
        '''
        marker="$2"; expected_source="$3"; expected_image="$4"; expected_activation="$5"; terminal_file="$6"
        removed=false
        docker() {
          if [[ "$1 $2" == "container inspect" ]]; then
            [[ "$removed" == false ]]; return
          fi
          case "$*" in
            *".Config.Image"*) echo "$expected_image" ;;
            *"source-sha"*) echo "$expected_source" ;;
            *"image-digest"*) echo "$expected_image" ;;
            *"activation-id"*) echo "$expected_activation" ;;
            *"shadow.mode"*) echo "shadow-continuous" ;;
            *".Config.Cmd"*) printf '["python","-m","kiwoom_stock","shadow-worker","--source-sha","%s","--image-digest","%s","--activation-id","%s"]\n' "$expected_source" "$expected_image" "$expected_activation" ;;
            *".State.Running"*) echo "false" ;;
            *".State.ExitCode"*) echo "0" ;;
            "logs "*) cat "$terminal_file" ;;
            "rm "*) removed=true; : >"$marker" ;;
            "stop "*) return 99 ;;
            *) return 1 ;;
          esac
        }
        stop_shadow "$4" "$3" "$5"
        ''',
        str(removed), SOURCE_SHA, IMAGE, ACTIVATION_ID, str(terminal),
    )
    assert completed.returncode == 0, completed.stderr
    assert removed.exists()
    assert "terminal_status=DEADLINE" in completed.stdout


def _run_running_stop(tmp_path, evidence, exit_code):
    terminal = tmp_path / f"running-terminal-{exit_code}.json"
    terminal.write_text(json.dumps(evidence), encoding="utf-8")
    return _run_sourced(
        '''
        terminal_file="$2"; exit_value="$3"; expected_source="$4"; expected_image="$5"; expected_activation="$6"
        removed=false
        docker() {
          if [[ "$1 $2" == "container inspect" ]]; then
            [[ "$removed" == false ]]; return
          fi
          case "$*" in
            *".Config.Image"*) echo "$expected_image" ;;
            *"source-sha"*) echo "$expected_source" ;;
            *"image-digest"*) echo "$expected_image" ;;
            *"activation-id"*) echo "$expected_activation" ;;
            *"shadow.mode"*) echo "shadow-continuous" ;;
            *".Config.Cmd"*) printf '["python","-m","kiwoom_stock","shadow-worker","--source-sha","%s","--image-digest","%s","--activation-id","%s"]\n' "$expected_source" "$expected_image" "$expected_activation" ;;
            *".State.Running"*) echo "true" ;;
            *".State.ExitCode"*) echo "$exit_value" ;;
            "logs "*) cat "$terminal_file" ;;
            "stop "*) return 0 ;;
            "rm "*) removed=true ;;
            *) return 1 ;;
          esac
        }
        stop_shadow "$5" "$4" "$6"
        ''',
        str(terminal), str(exit_code), SOURCE_SHA, IMAGE, ACTIVATION_ID,
    )


def test_host_running_stop_requires_exact_clean_terminal_and_zero_exit(tmp_path):
    stopped = _terminal_evidence(status="STOPPED", reason="stop-requested")
    assert _run_running_stop(tmp_path, stopped, 0).returncode == 0
    for exit_code in (1, 137):
        assert _run_running_stop(tmp_path, stopped, exit_code).returncode != 0

    failure = _terminal_evidence(status="FAILED", reason="failure")
    mismatch = _terminal_evidence(status="DEADLINE", reason="run-deadline")
    assert _run_running_stop(tmp_path, failure, 0).returncode != 0
    assert _run_running_stop(tmp_path, mismatch, 0).returncode != 0


def test_host_failed_terminal_is_safely_emitted_and_container_is_preserved(tmp_path):
    failed = _terminal_evidence(
        status="FAILED",
        reason="failure",
        error_type="ReadOnlyBoundaryError",
    )
    completed = _run_running_stop(tmp_path, failed, 0)
    assert completed.returncode != 0
    emitted = [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.startswith("{")
    ]
    assert emitted == [failed]
    assert "container and logs preserved" in completed.stderr


def test_shadow_iam_policy_is_document_and_instance_scoped():
    import json

    document = json.loads(POLICY.read_text(encoding="utf-8"))
    statements = document["Statement"]
    assert statements[0]["Action"] == ["ssm:SendCommand"]
    assert "KiwoomStock-ShadowWorker" in statements[0]["Resource"][0]
    assert "instance/<EC2_INSTANCE_ID>" in statements[0]["Resource"][1]
    assert statements[1] == {
        "Sid": "ReadShadowCommandResult",
        "Effect": "Allow",
        "Action": ["ssm:GetCommandInvocation"],
        "Resource": "*",
    }
    assert statements[2] == {
        "Sid": "AttestExactShadowDocument",
        "Effect": "Allow",
        "Action": ["ssm:DescribeDocument", "ssm:GetDocument"],
        "Resource": (
            "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:document/"
            "KiwoomStock-ShadowWorker"
        ),
    }
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    assert "attest_activation_document" in workflow_text
    assert "--document-version \"${document_version}\"" in workflow_text
    assert "$LATEST" not in workflow_text
