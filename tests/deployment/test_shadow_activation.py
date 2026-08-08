"""Static contracts for the bounded, no-order shadow activation plane."""

import json
from pathlib import Path
import subprocess
import sys

import yaml


SCRIPT = Path("deploy/ec2/shadow_worker_control.sh")
DOCUMENT = Path("deploy/ssm/shadow-worker-document.yaml")
WORKFLOW = Path(".github/workflows/cd-shadow-worker-activation.yml")
POLICY = Path("deploy/iam/github-shadow-activation-policy.json.example")
SOURCE_SHA = "a" * 40
IMAGE = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
ACTIVATION_ID = "continuous-test"


def _cycle_evidence(**updates):
    result = {
        "schema_version": 2,
        "event": "cycle",
        "status": "PASS",
        "mode": "shadow-continuous",
        "source_sha": SOURCE_SHA,
        "image_digest": IMAGE,
        "activation_id": ACTIVATION_ID,
        "cycle_index": 1,
        "cycles": 1,
        "http_attempts": 6,
        "api_counts": {
            "token": 1,
            "stock_basic": 1,
            "stock_chart_5m": 1,
            "proxy_chart_60m": 1,
            "stock_strength": 1,
            "stock_orderbook": 1,
        },
        "local_counts": {
            "status": 1,
            "paper_buy": 0,
            "paper_sell": 0,
            "error": 0,
            "critical": 0,
        },
        "db_identity": "/var/lib/kiwoom/shadow-trades.db",
        "interval_seconds": 60.0,
        "cycle_start_elapsed_seconds": 0.0,
        "observed_interval_seconds": None,
        "db_reopened": False,
        "db_reopens": 0,
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
    result = _cycle_evidence()
    result.update(
        event="terminal",
        status="DEADLINE",
        reason="run-deadline",
        cycles=15,
        first_cycle_start_elapsed_seconds=0.0,
        second_cycle_start_elapsed_seconds=60.0,
        second_cycle_interval_seconds=60.0,
        minimum_cycle_interval_seconds=60.0,
        db_reopens=14,
    )
    result.pop("cycle_index", None)
    result.pop("interval_seconds", None)
    result.pop("cycle_start_elapsed_seconds", None)
    result.pop("observed_interval_seconds", None)
    result.pop("db_reopened", None)
    result.update(updates)
    return result


def _run_sourced(command, *args):
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {command}', "test", str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _run_host_cycle_parser(evidence, tmp_path):
    payload = tmp_path / "cycle-evidence.json"
    payload.write_text(json.dumps(evidence), encoding="utf-8")
    return _run_sourced(
        'validate_safe_evidence shadow-continuous cycle "$3" "$4" "$5" <"$2"',
        str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID,
    )


def _run_workflow_cycle_parser(evidence):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item.get("name") == "Execute bounded shadow action"
    )
    parser = step["run"].split("runtime_evidence=", 1)[1]
    parser = parser.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    invocation = {"StandardOutputContent": json.dumps(evidence)}
    return subprocess.run(
        [
            sys.executable,
            "-",
            json.dumps(invocation),
            "continuous",
            SOURCE_SHA,
            IMAGE,
            ACTIVATION_ID,
        ],
        input=parser,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_activation_evidence_builder(document_version="7", worker_hash="c" * 64):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item.get("name") == "Execute bounded shadow action"
    )
    parser = step["run"].rsplit("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    return subprocess.run(
        [sys.executable, "-", SOURCE_SHA, IMAGE, "123", ACTIVATION_ID,
         "oneshot", "00000000-0000-0000-0000-000000000001", "Success", "0",
         document_version, worker_hash, "d" * 64, '{"runtime_status":"PASS"}'],
        input=parser, check=False, capture_output=True, text=True,
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


def test_shadow_ssm_document_has_exact_bounded_actions_and_no_secret_parameters():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    parameters = document["parameters"]
    assert parameters["DesiredState"]["allowedValues"] == ["oneshot", "continuous", "stop"]
    assert set(parameters) == {
        "DesiredState",
        "ImageDigest",
        "SourceSha",
        "ActivationId",
        "ComposeShadowSha256",
        "ExpectedWorkerSha256",
        "ExpectedShadowDocumentSha256",
        "ExpectedInstanceId",
        "Region",
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
    assert command.count("--inherited-lock-fd 9") == 2


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
        "SSM_ExpectedShadowDocumentSha256": "d" * 64,
        "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
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
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "source_sha",
        "image_digest",
        "build_run_id",
        "compose_shadow_sha256",
        "activation_id",
        "desired_state",
        "worker_sha256",
        "shadow_document_sha256",
    }
    assert triggers["workflow_dispatch"]["inputs"]["build_run_id"]["required"] is False
    assert triggers["workflow_dispatch"]["inputs"]["compose_shadow_sha256"]["required"] is False
    assert triggers["workflow_dispatch"]["inputs"]["worker_sha256"]["required"] is True
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
    assert "secrets." not in text
    assert "KIWOOM_APP_KEY" not in text
    assert "KIWOOM_SECRET_KEY" not in text
    assert "ssm get-parameter" not in text.lower()
    assert "ssm get-parameters" not in text.lower()
    assert "DesiredState=${DESIRED_STATE}" in text
    assert "ExpectedWorkerSha256=${WORKER_SHA256}" in text
    assert "ExpectedShadowDocumentSha256=${SHADOW_DOCUMENT_SHA256}" in text
    assert "runtime safe result was not found" in text
    assert '"orders": side_effects["broker_orders"]' in text
    assert '"database": bool(result.get("db_identity"))' in text
    assert 'if [[ "${DESIRED_STATE}" == stop ]]' in text
    assert '[[ -z "${BUILD_RUN_ID}${COMPOSE_SHADOW_SHA256}" ]]' in text
    assert '"${document_version}" "${WORKER_SHA256}" "${SHADOW_DOCUMENT_SHA256}"' in text
    assert '"document_version": document_version' in text
    assert '"worker_sha256": worker_sha256' in text
    assert '"shadow_document_sha256": shadow_document_sha256' in text
    assert 're.fullmatch(r"[1-9][0-9]*", document_version)' in text


def test_activation_evidence_binds_attested_numeric_version_and_pair_hashes():
    completed = _run_activation_evidence_builder()
    assert completed.returncode == 0, completed.stderr
    evidence = json.loads(completed.stdout)
    assert evidence["document_version"] == "7"
    assert evidence["worker_sha256"] == "c" * 64
    assert evidence["shadow_document_sha256"] == "d" * 64
    assert _run_activation_evidence_builder(document_version="$LATEST").returncode != 0
    assert _run_activation_evidence_builder(worker_hash="bad").returncode != 0


def test_host_evidence_parser_rejects_stale_and_malformed_first_ticks(tmp_path):
    payload = tmp_path / "tick.json"
    command = (
        'validate_safe_evidence shadow-continuous cycle "$3" "$4" "$5" <"$2"'
    )
    payload.write_text(json.dumps(_cycle_evidence()), encoding="utf-8")
    valid = _run_sourced(
        command,
        str(payload),
        SOURCE_SHA,
        IMAGE,
        ACTIVATION_ID,
    )
    assert valid.returncode == 0, valid.stderr

    payload.write_text(
        json.dumps(_cycle_evidence(source_sha="c" * 40)),
        encoding="utf-8",
    )
    stale = _run_sourced(command, str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID)
    assert stale.returncode != 0

    payload.write_text(
        json.dumps(_cycle_evidence(http_attempts=24)),
        encoding="utf-8",
    )
    malformed = _run_sourced(command, str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID)
    assert malformed.returncode != 0

    for api_counts, local_counts in (
        ({}, _cycle_evidence()["local_counts"]),
        ({**_cycle_evidence()["api_counts"], "extra": 1}, _cycle_evidence()["local_counts"]),
        (_cycle_evidence()["api_counts"], {}),
        (_cycle_evidence()["api_counts"], {**_cycle_evidence()["local_counts"], "extra": 0}),
    ):
        payload.write_text(
            json.dumps(_cycle_evidence(api_counts=api_counts, local_counts=local_counts)),
            encoding="utf-8",
        )
        rejected = _run_sourced(command, str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID)
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


def test_host_and_workflow_validate_continuous_terminal_reopen_evidence(tmp_path):
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(
        item for item in workflow["jobs"]["activate"]["steps"]
        if item.get("name") == "Execute bounded shadow action"
    )
    parser = step["run"].split("runtime_evidence=", 1)[1]
    parser = parser.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]

    terminal = _terminal_evidence()
    payload = tmp_path / "terminal-evidence.json"
    payload.write_text(json.dumps(terminal), encoding="utf-8")
    host = _run_sourced(
        'validate_safe_evidence shadow-continuous terminal "$3" "$4" "$5" <"$2"',
        str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID,
    )
    workflow_result = subprocess.run(
        [
            sys.executable, "-", json.dumps({"StandardOutputContent": json.dumps(terminal)}),
            "stop", SOURCE_SHA, IMAGE, ACTIVATION_ID,
        ],
        input=parser, check=False, capture_output=True, text=True,
    )
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
        payload.write_text(json.dumps(malformed), encoding="utf-8")
        assert _run_sourced(
            'validate_safe_evidence shadow-continuous terminal "$3" "$4" "$5" <"$2"',
            str(payload), SOURCE_SHA, IMAGE, ACTIVATION_ID,
        ).returncode != 0
        failed_workflow = subprocess.run(
            [
                sys.executable, "-", json.dumps({"StandardOutputContent": json.dumps(malformed)}),
                "stop", SOURCE_SHA, IMAGE, ACTIVATION_ID,
            ],
            input=parser, check=False, capture_output=True, text=True,
        )
        assert failed_workflow.returncode != 0


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
