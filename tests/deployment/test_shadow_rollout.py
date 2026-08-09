"""Contracts for the exact protected shadow rollout plane."""

import json
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess

import pytest
import yaml

from kiwoom_stock.deployment import shadow_rollout


WORKFLOW = Path(".github/workflows/cd-shadow-worker-rollout.yml")
ROLLOUT_DOCUMENT = Path("deploy/ssm/shadow-worker-rollout-document.yaml")
WORKER = Path("deploy/ec2/shadow_worker_control.sh")
VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")


def _host_evidence(action, rollout, installed):
    present = bool(installed)
    return {
        "action": action,
        "source_sha": rollout.source_sha if present else "",
        "worker_sha256": rollout.worker_sha256 if present else "",
        "validator_sha256": rollout.validator_sha256 if present else "",
        "shadow_document_sha256": rollout.shadow_document_sha256 if present else "",
        "rollout_attempt_id": rollout.rollout_attempt_id if present else "",
        "observed_worker_sha256": rollout.worker_sha256 if present else "",
        "observed_validator_sha256": rollout.validator_sha256 if present else "",
        "worker_present": present, "worker_owner": "0:0" if present else "",
        "worker_mode": "750" if present else "", "worker_links": 1 if present else 0,
        "worker_regular": present, "worker_metadata_valid": True,
        "validator_present": present, "validator_owner": "0:0" if present else "",
        "validator_mode": "750" if present else "",
        "validator_links": 1 if present else 0,
        "validator_regular": present, "validator_metadata_valid": True,
        "binding_present": present, "binding_owner": "0:0" if present else "",
        "binding_mode": "600" if present else "", "binding_links": 1 if present else 0,
        "binding_regular": present, "binding_metadata_valid": True,
    }


def test_rollout_tuple_and_strict_canonical_json_fail_closed():
    value, encoded = shadow_rollout.canonical_json(b'{"b":2,"a":1}')
    assert value == {"a": 1, "b": 2}
    assert encoded == b'{"a":1,"b":2}'
    with pytest.raises(shadow_rollout.RolloutError, match="document_duplicate_key"):
        shadow_rollout.strict_json(b'{"a":1,"a":2}')
    with pytest.raises(shadow_rollout.RolloutError, match="document_json_invalid"):
        shadow_rollout.strict_json(b"not-json")


def test_rollout_comment_distinguishes_workflow_reruns(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    first = shadow_rollout._rollout_comment("install", rollout)
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    second = shadow_rollout._rollout_comment("install", rollout)
    assert first == "kiwoom-shadow-rollout/123/1/install"
    assert second == "kiwoom-shadow-rollout/123/2/install"
    assert first != second


def test_rollout_workflow_is_source_only_protected_and_serialized():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"source_sha"}
    assert inputs["source_sha"]["required"] is True
    assert "default" not in inputs["source_sha"]
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "kiwoom-stock-shadow-i-02cb0a404794bd43a",
        "cancel-in-progress": False,
    }
    job = workflow["jobs"]["rollout"]
    assert job["environment"] == "production-shadow"
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["permissions"] == {"contents": "read", "id-token": "write"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "ref: ${{ inputs.source_sha }}" in text
    assert '[[ "${SOURCE_SHA}" == "${TRIGGER_SHA}" ]]' in text
    assert "KIWOOM_AWS_SHADOW_ROLLOUT_ROLE_ARN" in text
    assert "KIWOOM_AWS_SHADOW_ROLE_ARN" not in text
    assert "AWS-RunShellScript" not in text
    assert "retention-days: 14" in text
    activation = yaml.safe_load(
        Path(".github/workflows/cd-shadow-worker-activation.yml").read_text(encoding="utf-8")
    )
    assert activation["concurrency"] == workflow["concurrency"]


def test_rollout_document_has_only_anchored_tuple_and_fixed_actions():
    document = yaml.safe_load(ROLLOUT_DOCUMENT.read_text(encoding="utf-8"))
    assert set(document["parameters"]) == {
        "Action", "SourceSha", "WorkerSha256", "ValidatorSha256",
        "ShadowDocumentSha256",
        "RolloutAttemptId", "ExpectedInstanceId", "Region",
    }
    assert document["parameters"]["Action"]["allowedValues"] == [
        "install", "readback", "rollback"
    ]
    for name, parameter in document["parameters"].items():
        assert parameter["interpolationType"] == "ENV_VAR"
        if name != "Action":
            assert parameter["allowedPattern"].startswith("^")
            assert parameter["allowedPattern"].endswith("$")
    text = ROLLOUT_DOCUMENT.read_text(encoding="utf-8")
    assert "AWS-RunShellScript" not in text
    assert "raw.githubusercontent.com/SpiceChicken/kiwoom_stock/${source_sha}/deploy/ec2/shadow_worker_control.sh" in text
    assert "raw.githubusercontent.com/SpiceChicken/kiwoom_stock/${source_sha}/deploy/ec2/shadow_runtime_evidence.py" in text
    for forbidden in ("\n  Command:", "\n  Url:", "\n  TargetPath:", "\n  DocumentName:"):
        assert forbidden not in text
    assert "exec /bin/bash -s -- \\\n" in text
    assert "flock -x -w 240" in text
    assert "fsync_file" in text and "fsync_parent" in text
    assert "fsync_directory" in text
    assert 'mktemp -d "$state/.attempt-${attempt}.staging.XXXXXX"' in text
    assert 'mv -T "$staging" "$backup"' in text
    assert text.index('fsync_file "$staging/manifest.json"') < text.index(
        'fsync_directory "$staging"'
    ) < text.index('mv -T "$staging" "$backup"') < text.index(
        'fsync_directory "$state"'
    )
    assert 'mv -fT "$temporary" "$destination"' in text
    assert 'install -o root -g root -m 0750 "$tmp" "$target"' not in text
    assert 'install -o root -g root -m 0750 "$backup/worker" "$target"' not in text
    assert "rollback_set >/dev/null 2>&1" in text
    assert shadow_rollout.expected_rollout_document() == document


def _rendered_rollout_command():
    document = yaml.safe_load(ROLLOUT_DOCUMENT.read_text(encoding="utf-8"))
    return document["mainSteps"][0]["inputs"]["runCommand"][0]


def test_rendered_rollout_command_dispatches_argv_before_any_host_mutation():
    environment = dict(os.environ)
    environment.update({
        "SSM_Action": "unsupported-test-action",
        "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": "b" * 64,
        "SSM_ValidatorSha256": "d" * 64,
        "SSM_ShadowDocumentSha256": "c" * 64,
        "SSM_RolloutAttemptId": "123",
        "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", _rendered_rollout_command()], env=environment,
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 64
    assert completed.stderr == "unsupported rollout action: unsupported-test-action\n"


@pytest.mark.parametrize(
    ("lifecycle", "docker_exit"),
    [
        ("running", 0),
        ("stopped-after-deadline", 0),
        ("label-missing", 0),
        ("label-mismatch", 0),
        ("operational-error", 55),
    ],
)
def test_fixed_container_or_inventory_error_blocks_before_backup_download_publish(
    tmp_path, lifecycle, docker_exit,
):
    worker = tmp_path / "worker"
    validator = tmp_path / "validator.py"
    binding = tmp_path / "binding.json"
    state = tmp_path / "state"
    lock = tmp_path / "rollout.lock"
    worker.write_bytes(b"old-worker")
    validator.write_bytes(b"old-validator")
    binding.write_bytes(b"old-binding")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl_marker = tmp_path / "curl-called"
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        f"[[ \"$*\" == \"container ls --all --filter name=^/kiwoom-shadow-once$ --format {{{{.Names}}}}\" ]] || exit 91\n"
        f"[[ {docker_exit} -eq 0 ]] || exit {docker_exit}\n"
        "printf 'kiwoom-shadow-once\\n'\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = tools / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\ntouch '{curl_marker}'\nexit 99\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    command = _rendered_rollout_command().replace(
        "worker_target=/usr/local/sbin/kiwoom-shadow-worker",
        f"worker_target={worker}",
    ).replace(
        "validator_target=/usr/local/libexec/kiwoom-shadow-runtime-evidence.py",
        f"validator_target={validator}",
    ).replace(
        "state=/var/lib/kiwoom-stock/shadow-rollout-backups", f"state={state}"
    ).replace(
        "binding=/var/lib/kiwoom-stock/shadow-rollout-current.json",
        f"binding={binding}",
    ).replace(
        "lock=/run/lock/kiwoom-stock-shadow.lock", f"lock={lock}"
    ).replace("-o root -g root ", "")
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "SSM_Action": "install", "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": "b" * 64, "SSM_ValidatorSha256": "c" * 64,
        "SSM_ShadowDocumentSha256": "d" * 64,
        "SSM_RolloutAttemptId": "321",
        "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", command], env=environment, check=False,
        capture_output=True, text=True, timeout=10,
    )
    assert completed.returncode != 0
    if lifecycle == "operational-error":
        assert "docker fixed shadow inventory failed" in completed.stderr
    else:
        assert "fixed shadow container exists; artifact rollout blocked" in completed.stderr
    assert worker.read_bytes() == b"old-worker"
    assert validator.read_bytes() == b"old-validator"
    assert binding.read_bytes() == b"old-binding"
    assert not curl_marker.exists()
    assert not (state / "321").exists()


def test_atomic_binding_publish_failure_restores_exact_prior_artifact_set(tmp_path):
    target = tmp_path / "bin" / "kiwoom-shadow-worker"
    validator_target = tmp_path / "libexec" / "kiwoom-shadow-runtime-evidence.py"
    state = tmp_path / "state"
    binding = tmp_path / "current.json"
    lock = tmp_path / "rollout.lock"
    target.parent.mkdir()
    validator_target.parent.mkdir()
    old_worker = b"#!/usr/bin/env bash\necho old\n"
    new_worker = b"#!/usr/bin/env bash\necho new\n"
    old_validator = b"#!/usr/bin/env python3\nprint('old')\n"
    new_validator = b"#!/usr/bin/env python3\nprint('new')\n"
    target.write_bytes(old_worker)
    target.chmod(0o750)
    validator_target.write_bytes(old_validator)
    validator_target.chmod(0o750)
    old_binding = b'{"prior":"binding"}\n'
    binding.write_bytes(old_binding)
    binding.chmod(0o600)
    source = tmp_path / "new-worker"
    source.write_bytes(new_worker)
    source.chmod(0o700)
    validator_source = tmp_path / "new-validator"
    validator_source.write_bytes(new_validator)
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_curl = tools / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "destination=sys.argv[sys.argv.index('-o')+1]\n"
        "source=os.environ['FAKE_VALIDATOR'] if 'shadow_runtime_evidence.py' in ' '.join(sys.argv) else os.environ['FAKE_WORKER']\n"
        "shutil.copyfile(source,destination)\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_docker = tools / "docker"
    fake_docker.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    uid, gid = os.getuid(), os.getgid()
    base_command = _rendered_rollout_command()
    base_command = base_command.replace(
        "worker_target=/usr/local/sbin/kiwoom-shadow-worker", f"worker_target={target}"
    ).replace(
        "validator_target=/usr/local/libexec/kiwoom-shadow-runtime-evidence.py",
        f"validator_target={validator_target}",
    ).replace(
        "state=/var/lib/kiwoom-stock/shadow-rollout-backups", f"state={state}"
    ).replace(
        "binding=/var/lib/kiwoom-stock/shadow-rollout-current.json", f"binding={binding}"
    ).replace(
        "lock=/run/lock/kiwoom-stock-shadow.lock", f"lock={lock}"
    ).replace("-o root -g root ", "").replace(
        "0:0:", f"{uid}:{gid}:"
    ).replace(
        "st_uid!=0", f"st_uid!={uid}"
    ).replace(
        "st_gid!=0", f"st_gid!={gid}"
    ).replace(
        'owner=="0:0"', f'owner=="{uid}:{gid}"'
    ).replace(
        'chown root:root "$staging"', "true"
    ).replace(
        'chown root:root "$staging/manifest.json"', "true"
    )
    command = base_command.replace(
        'publish "$marker" "$binding" 600 "$marker_sha" no',
        "false # injected binding publish failure",
    )
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}", "FAKE_WORKER": str(source),
        "FAKE_VALIDATOR": str(validator_source),
        "SSM_Action": "install", "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": shadow_rollout.sha256(new_worker),
        "SSM_ValidatorSha256": shadow_rollout.sha256(new_validator),
        "SSM_ShadowDocumentSha256": "c" * 64,
        "SSM_RolloutAttemptId": "789",
        "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", command], env=environment,
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode != 0
    assert target.read_bytes() == old_worker
    assert validator_target.read_bytes() == old_validator
    assert binding.read_bytes() == old_binding
    assert target.stat().st_mode & 0o777 == 0o750
    assert validator_target.stat().st_mode & 0o777 == 0o750
    assert binding.stat().st_mode & 0o777 == 0o600
    sealed = state / "789"
    assert sealed.is_dir()
    assert (sealed / "manifest.json").is_file()
    assert not list(state.glob(".attempt-789.staging.*"))

    replay = subprocess.run(
        ["bash", "-c", base_command], env=environment,
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert replay.returncode == 0, replay.stderr
    assert target.read_bytes() == new_worker
    assert validator_target.read_bytes() == new_validator
    host_evidence = json.loads(
        next(line for line in replay.stdout.splitlines() if line.startswith("{"))
    )
    assert set(host_evidence) == shadow_rollout.HOST_EVIDENCE_KEYS
    assert host_evidence["worker_metadata_valid"] is True
    assert host_evidence["validator_metadata_valid"] is True
    assert host_evidence["binding_metadata_valid"] is True
    current = json.loads(binding.read_text(encoding="utf-8"))
    assert current["rollout_attempt_id"] == "789"
    assert current["worker_sha256"] == shadow_rollout.sha256(new_worker)
    assert current["validator_sha256"] == shadow_rollout.sha256(new_validator)
    collision_environment = dict(environment)
    collision_environment["SSM_ShadowDocumentSha256"] = "d" * 64
    collision = subprocess.run(
        ["bash", "-c", base_command], env=collision_environment,
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert collision.returncode != 0
    assert target.read_bytes() == new_worker
    assert validator_target.read_bytes() == new_validator
    assert json.loads(binding.read_text())["shadow_document_sha256"] == "c" * 64


@pytest.mark.parametrize("prior", ["all-absent", "mixed"])
@pytest.mark.parametrize("failure_phase", ["validator", "worker", "binding"])
def test_publish_failure_restores_absent_and_mixed_prior_artifact_sets(
    tmp_path, prior, failure_phase,
):
    worker = tmp_path / "bin" / "worker"
    validator = tmp_path / "libexec" / "validator.py"
    binding = tmp_path / "binding.json"
    worker.parent.mkdir()
    validator.parent.mkdir()
    old_worker = b"#!/usr/bin/env bash\necho old\n"
    old_binding = b'{"old":"binding"}\n'
    if prior == "mixed":
        worker.write_bytes(old_worker)
        worker.chmod(0o750)
        binding.write_bytes(old_binding)
        binding.chmod(0o600)
    new_worker = tmp_path / "new-worker"
    new_validator = tmp_path / "new-validator"
    new_worker.write_bytes(b"#!/usr/bin/env bash\necho new\n")
    new_validator.write_bytes(b"#!/usr/bin/env python3\nprint('new')\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "target=sys.argv[sys.argv.index('-o')+1]\n"
        "source=os.environ['NEW_VALIDATOR'] if 'shadow_runtime_evidence.py' in ' '.join(sys.argv) else os.environ['NEW_WORKER']\n"
        "shutil.copyfile(source,target)\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    for name, body in {
        "docker": "#!/usr/bin/env bash\nexit 0\n",
        "chown": "#!/usr/bin/env bash\nexit 0\n",
    }.items():
        path = tools / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    state = tmp_path / "state"
    lock = tmp_path / "lock"
    uid, gid = os.getuid(), os.getgid()
    command = _rendered_rollout_command().replace(
        "worker_target=/usr/local/sbin/kiwoom-shadow-worker",
        f"worker_target={worker}",
    ).replace(
        "validator_target=/usr/local/libexec/kiwoom-shadow-runtime-evidence.py",
        f"validator_target={validator}",
    ).replace(
        "state=/var/lib/kiwoom-stock/shadow-rollout-backups", f"state={state}"
    ).replace(
        "binding=/var/lib/kiwoom-stock/shadow-rollout-current.json",
        f"binding={binding}",
    ).replace(
        "lock=/run/lock/kiwoom-stock-shadow.lock", f"lock={lock}"
    ).replace("-o root -g root ", "").replace(
        "0:0:", f"{uid}:{gid}:"
    ).replace("st_uid!=0", f"st_uid!={uid}").replace(
        "st_gid!=0", f"st_gid!={gid}"
    ).replace('owner=="0:0"', f'owner=="{uid}:{gid}"')
    failure_line = {
        "validator": 'publish "$validator_downloaded" "$validator_target" 750 "$validator_sha" python',
        "worker": 'publish "$downloaded" "$worker_target" 750 "$worker_sha" shell',
        "binding": 'publish "$marker" "$binding" 600 "$marker_sha" no',
    }[failure_phase]
    command = command.replace(failure_line, "false # injected publish failure", 1)
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "NEW_WORKER": str(new_worker), "NEW_VALIDATOR": str(new_validator),
        "SSM_Action": "install", "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": shadow_rollout.sha256(new_worker.read_bytes()),
        "SSM_ValidatorSha256": shadow_rollout.sha256(new_validator.read_bytes()),
        "SSM_ShadowDocumentSha256": "d" * 64,
        "SSM_RolloutAttemptId": "654",
        "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", command], env=environment, check=False,
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode != 0
    if prior == "all-absent":
        assert not worker.exists()
        assert not validator.exists()
        assert not binding.exists()
    else:
        assert worker.read_bytes() == old_worker
        assert not validator.exists()
        assert binding.read_bytes() == old_binding
    manifest = json.loads((state / "654" / "manifest.json").read_text())
    assert manifest["prior_validator_sha256"] == "absent"
    if prior == "all-absent":
        assert manifest["prior_worker_sha256"] == "absent"
        assert manifest["prior_binding_sha256"] == "absent"


def test_host_worker_is_shell_valid_and_has_mandatory_artifact_set_guard():
    completed = subprocess.run(["bash", "-n", str(WORKER)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    text = WORKER.read_text(encoding="utf-8")
    assert "validate_rollout_binding" in text
    assert "--expected-worker-sha256" in text
    assert "--expected-validator-sha256" in text
    assert "--expected-shadow-document-sha256" in text
    assert "shadow-rollout-current.json" in text


def test_rollout_iam_contract_is_exact_and_separate():
    trust = json.loads(Path("deploy/iam/github-shadow-rollout-trust.json.example").read_text())
    condition = trust["Statement"][0]["Condition"]["StringEquals"]
    assert condition == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "repo:SpiceChicken/kiwoom_stock:environment:production-shadow",
    }
    policy = json.loads(Path("deploy/iam/github-shadow-rollout-policy.json.example").read_text())
    statements = policy["Statement"]
    actions = {action for item in statements for action in item["Action"]}
    assert actions == {
        "ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:GetDocument",
        "ssm:DescribeDocument", "ssm:ListDocumentVersions", "ssm:UpdateDocument",
        "ssm:UpdateDocumentDefaultVersion", "ssm:ListCommandInvocations",
        "ssm:ListCommands",
    }
    wildcard = [item for item in statements if item["Resource"] == "*"]
    assert {item["Sid"] for item in wildcard} == {
        "ReadRolloutInvocation", "ReadShadowCommandHistoryForLegacyTransition",
        "ReadShadowCommandAcceptanceForLegacyTransition",
    }
    assert {tuple(item["Action"]) for item in wildcard} == {
        ("ssm:GetCommandInvocation",), ("ssm:ListCommandInvocations",),
        ("ssm:ListCommands",),
    }
    assert "ssm:CreateDocument" not in actions
    assert "ssm:DeleteDocument" not in actions


def test_aws_send_records_acceptance_terminal_status_and_exact_argv(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64,
        shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    evidence = _host_evidence("install", rollout, True)
    responses = iter([
        subprocess.CompletedProcess([], 0, json.dumps({"Command": {"CommandId": command_id}}), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "Status": "Success", "ResponseCode": 0,
            "StandardOutputContent": json.dumps(evidence),
        }), ""),
    ])
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return next(responses)
    monkeypatch.setattr(shadow_rollout.subprocess, "run", fake_run)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    assert adapter.send("install", rollout, expect_tuple=True) == evidence
    assert calls[0][0][0:3] == ["aws", "ssm", "send-command"]
    assert calls[0][1]["AWS_MAX_ATTEMPTS"] == "1"
    assert calls[0][0][calls[0][0].index("--comment") + 1] == (
        "kiwoom-shadow-rollout/123/local/install"
    )
    assert json.loads(
        calls[0][0][calls[0][0].index("--parameters") + 1]
    ) == shadow_rollout._rollout_parameters("install", rollout)
    assert adapter.commands == [{
        "action": "install", "command_id": command_id, "accepted": True,
        "status": "Success", "response_code": 0,
        "comment": "kiwoom-shadow-rollout/123/local/install",
    }]


@pytest.mark.parametrize(
    ("args", "write", "category"),
    [
        (["iam", "create-role"], False, "aws_command_not_allowed"),
        (
            [
                "ssm", "update-document-default-version", "--name",
                shadow_rollout.SHADOW_DOCUMENT, "--document-version", "1",
            ],
            False,
            "aws_command_write_mismatch",
        ),
        (
            [
                "ssm", "describe-document", "--name",
                shadow_rollout.SHADOW_DOCUMENT,
            ],
            True,
            "aws_command_write_mismatch",
        ),
    ],
)
def test_aws_call_rejects_unallowed_or_write_mismatch_before_subprocess(
    monkeypatch, args, write, category,
):
    invoked = False

    def forbidden_run(*_args, **_kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("AWS subprocess must not run")

    monkeypatch.setattr(shadow_rollout.subprocess, "run", forbidden_run)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)

    with pytest.raises(shadow_rollout.RolloutError, match=category):
        adapter.call(args, write=write)

    assert invoked is False


@pytest.mark.parametrize(
    ("nonterminal", "terminal", "response_code", "succeeds"),
    [
        ("Pending", "Success", 0, True),
        ("InProgress", "Success", 0, True),
        ("Delayed", "Success", 0, True),
        ("Cancelling", "Success", 0, True),
        ("Cancelling", "Cancelled", 1, False),
    ],
)
def test_finish_command_waits_through_nonterminal_to_true_terminal(
    monkeypatch, nonterminal, terminal, response_code, succeeds,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    evidence = _host_evidence("install", rollout, True)
    responses = iter([
        {"Status": nonterminal},
        {
            "Status": terminal, "ResponseCode": response_code,
            "StandardOutputContent": json.dumps(evidence),
        },
    ])
    calls = []
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)

    def call(args, write=False):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(adapter, "call", call)
    monkeypatch.setattr(shadow_rollout.time, "sleep", lambda _: None)
    record = {}
    if succeeds:
        assert adapter._finish_command(
            "install", rollout, record,
            "00000000-0000-0000-0000-000000000001", expect_tuple=True,
        ) == evidence
    else:
        with pytest.raises(shadow_rollout.RolloutError, match="host_action_failed"):
            adapter._finish_command(
                "install", rollout, record,
                "00000000-0000-0000-0000-000000000001", expect_tuple=True,
            )
    assert len(calls) == 2
    assert record["status"] == terminal
    assert record["response_code"] == response_code


@pytest.mark.parametrize("response_code", [False, 0.0, "0"])
def test_finish_command_rejects_non_exact_integer_response_code(
    monkeypatch, response_code,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    invocation = {
        "Status": "Success", "ResponseCode": response_code,
        "StandardOutputContent": json.dumps(
            _host_evidence("install", rollout, True)
        ),
    }
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "poll", lambda command_id: invocation)
    record = {}
    with pytest.raises(shadow_rollout.RolloutError, match="host_action_failed"):
        adapter._finish_command(
            "install", rollout, record,
            "00000000-0000-0000-0000-000000000001", expect_tuple=True,
        )
    assert record["status"] == "Success"
    assert record["response_code"] == response_code


def _acceptance_command(rollout, command_id, *, parameters=None):
    return {
        "CommandId": command_id,
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "DocumentVersion": "1",
        "Comment": "kiwoom-shadow-rollout/123/local/install",
        "Parameters": parameters or shadow_rollout._rollout_parameters(
            "install", rollout
        ),
        "InstanceIds": [shadow_rollout.INSTANCE_ID],
        "Targets": [],
        "RequestedDateTime": "2026-08-09T12:00:00+00:00",
        "Status": "InProgress",
    }


def _acceptance_invocation(command_id):
    return {
        "CommandId": command_id,
        "InstanceId": shadow_rollout.INSTANCE_ID,
        "Comment": "kiwoom-shadow-rollout/123/local/install",
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "RequestedDateTime": "2026-08-09T12:00:00+00:00",
        "Status": "InProgress",
        "CommandPlugins": [],
    }


def test_response_lost_send_reconciles_one_exact_command_without_resend(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    evidence = _host_evidence("install", rollout, True)
    responses = iter([
        subprocess.CompletedProcess([], 1, "", "response lost"),
        subprocess.CompletedProcess([], 0, json.dumps({"Commands": []}), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "Commands": [_acceptance_command(rollout, command_id)],
        }), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "CommandInvocations": [_acceptance_invocation(command_id)],
        }), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "Status": "Success", "ResponseCode": 0,
            "StandardOutputContent": json.dumps(evidence),
        }), ""),
    ])
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return next(responses)

    monkeypatch.setattr(shadow_rollout.subprocess, "run", fake_run)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    with pytest.raises(shadow_rollout.RolloutError, match="aws_command_failed"):
        adapter.send("install", rollout, expect_tuple=True)
    assert adapter.reconcile_acceptance(
        "install", rollout, expect_tuple=True, sleeper=lambda _: None,
    ) == evidence
    assert sum(call[1:3] == ["ssm", "send-command"] for call in calls) == 1
    assert adapter.commands[0]["accepted"] == "reconciled"
    assert adapter.commands[0]["command_id"] == command_id
    assert adapter.commands[0]["status"] == "Success"


@pytest.mark.parametrize("exact_page", [0, 1])
def test_acceptance_history_finds_one_exact_command_on_first_or_later_page(
    monkeypatch, exact_page,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    exact_id = "00000000-0000-0000-0000-000000000001"
    other_id = "00000000-0000-0000-0000-000000000002"
    exact = _acceptance_command(rollout, exact_id)
    other = {
        **_acceptance_command(rollout, other_id),
        "Comment": "unrelated-command",
    }
    pages = [
        {"Commands": [exact if exact_page == 0 else other], "NextToken": "p2"},
        {"Commands": [other if exact_page == 0 else exact]},
    ]
    calls = []
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)

    def call(args, write=False):
        calls.append(args)
        return pages[len(calls) - 1]

    monkeypatch.setattr(adapter, "call", call)
    rollout_comment = shadow_rollout._rollout_comment("install", rollout)
    assert adapter._acceptance_commands(
        rollout_comment,
        shadow_rollout._rollout_parameters("install", rollout),
    ) == [exact_id]
    assert rollout_comment == "kiwoom-shadow-rollout/123/local/install"
    assert "--no-paginate" in calls[0]
    assert "--next-token" not in calls[0]
    assert calls[1][calls[1].index("--next-token") + 1] == "p2"


def test_acceptance_history_rejects_exact_matches_split_across_pages(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    pages = iter([
        {"Commands": [_acceptance_command(
            rollout, "00000000-0000-0000-0000-000000000001"
        )], "NextToken": "p2"},
        {"Commands": [_acceptance_command(
            rollout, "00000000-0000-0000-0000-000000000002"
        )]},
    ])
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    adapter.commands.append({
        "action": "install", "command_id": None, "accepted": "uncertain",
        "status": "unknown", "response_code": None,
        "comment": "kiwoom-shadow-rollout/123/local/install",
    })
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(pages))
    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_ambiguous"
    ):
        adapter.reconcile_acceptance("install", rollout, sleeper=lambda _: None)


def test_acceptance_history_rejects_duplicate_command_id_across_pages(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    pages = iter([
        {"Commands": [{
            **_acceptance_command(rollout, command_id),
            "Comment": "unrelated-command",
        }], "NextToken": "p2"},
        {"Commands": [_acceptance_command(rollout, command_id)]},
    ])
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(pages))
    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_item_invalid"
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
        )


@pytest.mark.parametrize(
    "pages",
    [
        [
            {"Commands": [], "NextToken": "same"},
            {"Commands": [], "NextToken": "same"},
        ],
        [{"Commands": [], "NextToken": 1}],
        [{"Commands": [], "NextToken": "x" * 4097}],
    ],
)
def test_acceptance_history_rejects_repeated_or_invalid_next_token(
    monkeypatch, pages,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    responses = iter(pages)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(responses))
    with pytest.raises(
        shadow_rollout.RolloutError,
        match="acceptance_history_next_token_invalid",
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
        )


def test_acceptance_history_rejects_extra_response_key_and_oversized_page(
    monkeypatch,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(
        adapter, "call", lambda args, write=False: {"Commands": [], "Extra": 1}
    )
    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_shape_invalid"
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
        )
    monkeypatch.setattr(shadow_rollout, "ACCEPTANCE_HISTORY_PAGE_SIZE", 1)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: {
        "Commands": [
            _acceptance_command(
                rollout, "00000000-0000-0000-0000-000000000001"
            ),
            _acceptance_command(
                rollout, "00000000-0000-0000-0000-000000000002"
            ),
        ],
    })
    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_page_oversized"
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
        )


@pytest.mark.parametrize(
    ("limit_name", "limit_value", "pages", "category"),
    [
        (
            "ACCEPTANCE_HISTORY_MAX_PAGES", 2,
            [
                {"Commands": [], "NextToken": "p2"},
                {"Commands": [], "NextToken": "p3"},
            ],
            "acceptance_history_page_limit",
        ),
        (
            "ACCEPTANCE_HISTORY_COMMAND_CAP", 1,
            [{"Commands": [
                {"CommandId": "ignored"}, {"CommandId": "also-ignored"},
            ]}],
            "acceptance_history_command_cap",
        ),
    ],
)
def test_acceptance_history_rejects_page_limit_or_total_command_cap(
    monkeypatch, limit_name, limit_value, pages, category,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    monkeypatch.setattr(shadow_rollout, limit_name, limit_value)
    responses = iter(pages)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(responses))
    with pytest.raises(shadow_rollout.RolloutError, match=category):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
        )


@pytest.mark.parametrize(
    ("commands", "category"),
    [
        ([{"Comment": "kiwoom-shadow-rollout/123/local/install"}],
         "acceptance_history_shape_invalid"),
        ("multiple", "acceptance_history_ambiguous"),
        ("tuple-mismatch", "acceptance_history_tuple_mismatch"),
    ],
)
def test_acceptance_reconciliation_rejects_malformed_ambiguous_or_wrong_tuple(
    monkeypatch, commands, category,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    first = "00000000-0000-0000-0000-000000000001"
    if commands == "multiple":
        commands = [
            _acceptance_command(rollout, first),
            _acceptance_command(
                rollout, "00000000-0000-0000-0000-000000000002"
            ),
        ]
    elif commands == "tuple-mismatch":
        wrong = shadow_rollout._rollout_parameters("install", rollout)
        wrong["ValidatorSha256"] = ["0" * 64]
        commands = [_acceptance_command(rollout, first, parameters=wrong)]
    monkeypatch.setattr(
        shadow_rollout.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 0, json.dumps({"Commands": commands}), ""
        ),
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    adapter.commands.append({
        "action": "install", "command_id": None, "accepted": "uncertain",
        "status": "unknown", "response_code": None,
        "comment": "kiwoom-shadow-rollout/123/local/install",
    })
    with pytest.raises(shadow_rollout.RolloutError, match=category):
        adapter.reconcile_acceptance(
            "install", rollout, sleeper=lambda _: None
        )


def test_aws_send_response_loss_preserves_uncertain_acceptance(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64,
        shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    monkeypatch.setattr(
        shadow_rollout.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", "timeout"),
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    with pytest.raises(shadow_rollout.RolloutError, match="aws_command_failed"):
        adapter.send("install", rollout)
    assert adapter.commands == [{
        "action": "install", "command_id": None, "accepted": "uncertain",
        "status": "unknown", "response_code": None,
        "comment": "kiwoom-shadow-rollout/123/local/install",
    }]


class _FakeAws:
    instances = []

    def __init__(self, deadline):
        self.calls = []
        self.command_ids = []
        self.host_evidence = []
        self.commands = []
        self.default = "1"
        self.__class__.instances.append(self)

    def send(self, action, rollout, expect_tuple=False):
        self.calls.append(("send", action))
        command_id = f"00000000-0000-0000-0000-{len(self.command_ids):012d}"
        self.command_ids.append(command_id)
        self.commands.append({"action": action, "command_id": command_id,
                              "accepted": True, "status": "Success", "response_code": 0})
        if action == "readback" and len([c for c in self.calls if c == ("send", "readback")]) == 1:
            return _host_evidence(action, rollout, False)
        return _host_evidence(action, rollout, True)

    def call(self, args, write=False):
        self.calls.append(("call", tuple(args), write))
        if args[:2] == ["ssm", "describe-document"]:
            name = args[args.index("--name") + 1]
            if name == shadow_rollout.ROLLOUT_DOCUMENT:
                return {"Document": {"DefaultVersion": "1", "LatestVersion": "1", "Status": "Active"}}
            return {"Document": {"DefaultVersion": self.default, "LatestVersion": "2", "Status": "Active"}}
        if args[:2] == ["ssm", "get-document"]:
            name = args[args.index("--name") + 1]
            if name == shadow_rollout.ROLLOUT_DOCUMENT:
                return {"Content": json.dumps(shadow_rollout.expected_rollout_document())}
            raw = Path("deploy/ssm/shadow-worker-document.yaml").read_bytes()
            _, canonical = shadow_rollout.canonical_json(raw)
            return {"Content": canonical.decode()}
        if args[:2] == ["ssm", "update-document"]:
            return {"DocumentDescription": {"DocumentVersion": "2"}}
        if args[:2] == ["ssm", "update-document-default-version"]:
            self.default = args[args.index("--document-version") + 1]
        return {}


class _HistoryAws:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def call(self, args, write=False):
        self.calls.append((tuple(args), write))
        return next(self.responses)


def _history_item(*, age_seconds=7200, status="Success"):
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    requested = datetime.fromtimestamp(
        checked.timestamp() - age_seconds, tz=timezone.utc
    ).isoformat()
    return {
        "CommandId": "00000000-0000-0000-0000-000000000001",
        "InstanceId": shadow_rollout.INSTANCE_ID,
        "DocumentName": shadow_rollout.SHADOW_DOCUMENT,
        "RequestedDateTime": requested,
        "Status": status,
        "CommandPlugins": [],
    }


def _history_command(*, age_seconds=7200, status="Success"):
    item = _history_item(age_seconds=age_seconds, status=status)
    return {
        "CommandId": item["CommandId"],
        "DocumentName": item["DocumentName"],
        "InstanceIds": [shadow_rollout.INSTANCE_ID],
        "Targets": [],
        "RequestedDateTime": item["RequestedDateTime"],
        "Status": item["Status"],
    }


def _drain_kwargs(checked):
    state = {"elapsed": 0.0}

    def monotonic():
        return state["elapsed"]

    def sleeper(seconds):
        state["elapsed"] += seconds

    return {"now": checked, "monotonic": monotonic, "sleeper": sleeper}


def test_legacy_history_quiet_pass_is_metadata_only_and_explicitly_paginated():
    aws = _HistoryAws([
        {"Commands": [_history_command()], "NextToken": "commands-page-2"},
        {"Commands": []},
        {"CommandInvocations": [_history_item()], "NextToken": "page-2"},
        {},
        {"Commands": []}, {"CommandInvocations": []},
        {"Commands": []}, {"CommandInvocations": []},
    ])
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    evidence = shadow_rollout.attest_legacy_command_quiet(
        aws, **_drain_kwargs(checked)
    )
    assert evidence["result"] == "PASS"
    assert evidence["scan_count"] == 3
    assert evidence["settling_seconds"] == 60
    assert evidence["observed_settling_seconds"] == 60
    assert evidence["first_checked_at"] == "2026-08-07T12:00:00Z"
    assert evidence["last_checked_at"] == "2026-08-07T12:01:00Z"
    assert evidence["scans"][0]["aggregate_commands"]["count"] == 1
    assert evidence["scans"][0]["node_invocations"]["count"] == 1
    assert all(scan["result"] == "PASS" for scan in evidence["scans"])
    assert len(aws.calls) == 8
    assert aws.calls[0][0][:2] == ("ssm", "list-commands")
    assert aws.calls[0][0][aws.calls[0][0].index("--instance-id") + 1] == (
        shadow_rollout.INSTANCE_ID
    )
    assert (
        f"key=DocumentName,value={shadow_rollout.SHADOW_DOCUMENT}"
        in aws.calls[0][0]
    )
    assert "--no-paginate" in aws.calls[0][0]
    assert "--next-token" in aws.calls[1][0]
    assert aws.calls[2][0][:2] == ("ssm", "list-command-invocations")
    assert "--no-details" in aws.calls[2][0]
    assert "--no-paginate" in aws.calls[2][0]
    assert "--next-token" in aws.calls[3][0]


def test_legacy_history_null_invocations_member_is_empty_and_quiet():
    aws = _HistoryAws([
        {"Commands": [], "NextToken": ""},
        {"CommandInvocations": None, "NextToken": ""},
        {"Commands": [], "NextToken": ""},
        {"CommandInvocations": None, "NextToken": ""},
        {"Commands": [], "NextToken": ""},
        {"CommandInvocations": None, "NextToken": ""},
    ])
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    evidence = shadow_rollout.attest_legacy_command_quiet(
        aws, **_drain_kwargs(checked)
    )
    assert evidence["result"] == "PASS"
    assert evidence["scans"][0]["node_invocations"]["count"] == 0


@pytest.mark.parametrize(
    ("response", "category"),
    [
        ({"CommandInvocations": [_history_item(age_seconds=60)]},
         "legacy_history_not_quiet"),
        ({"CommandInvocations": [_history_item(status="InProgress")]},
         "legacy_history_nonterminal"),
        ({"CommandInvocations": [{**_history_item(), "RequestedDateTime": "not-time"}]},
         "legacy_history_timestamp_invalid"),
        ({"CommandInvocations": "not-a-list"}, "legacy_history_shape_invalid"),
    ],
)
def test_legacy_history_recent_nonterminal_and_malformed_fail(response, category):
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(shadow_rollout.RolloutError, match=category):
        shadow_rollout.attest_legacy_command_quiet(
            _HistoryAws([{"Commands": []}, response]),
            **_drain_kwargs(checked),
        )


def test_legacy_history_repeated_pagination_token_fails_closed():
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    aws = _HistoryAws([
        {"Commands": []},
        {"CommandInvocations": [], "NextToken": "same"},
        {"CommandInvocations": [], "NextToken": "same"},
    ])
    with pytest.raises(shadow_rollout.RolloutError, match="legacy_history_next_token_invalid"):
        shadow_rollout.attest_legacy_command_quiet(
            aws, **_drain_kwargs(checked)
        )


@pytest.mark.parametrize(
    ("response", "category"),
    [
        ({"Commands": [_history_command(age_seconds=60)]},
         "legacy_commands_not_quiet"),
        ({"Commands": [_history_command(status="Pending")]},
         "legacy_commands_nonterminal"),
        ({"Commands": [{**_history_command(), "InstanceIds": []}]},
         "legacy_commands_item_invalid"),
        ({"Commands": [{**_history_command(), "RequestedDateTime": "bad"}]},
         "legacy_history_timestamp_invalid"),
        ({"Commands": "not-a-list"}, "legacy_commands_shape_invalid"),
    ],
)
def test_legacy_aggregate_recent_pending_and_malformed_fail(response, category):
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    with pytest.raises(shadow_rollout.RolloutError, match=category):
        shadow_rollout.attest_legacy_command_quiet(
            _HistoryAws([response]), **_drain_kwargs(checked)
        )


def test_legacy_aggregate_repeated_pagination_token_fails_closed():
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    aws = _HistoryAws([
        {"Commands": [], "NextToken": "same"},
        {"Commands": [], "NextToken": "same"},
    ])
    with pytest.raises(shadow_rollout.RolloutError, match="legacy_commands_next_token_invalid"):
        shadow_rollout.attest_legacy_command_quiet(
            aws, **_drain_kwargs(checked)
        )


def test_legacy_aggregate_pagination_page_limit_fails_closed():
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    aws = _HistoryAws([
        {"Commands": [], "NextToken": f"page-{index}"}
        for index in range(shadow_rollout.LEGACY_HISTORY_MAX_PAGES)
    ])
    with pytest.raises(shadow_rollout.RolloutError, match="legacy_commands_page_limit"):
        shadow_rollout.attest_legacy_command_quiet(
            aws, **_drain_kwargs(checked)
        )


def test_legacy_settling_rescan_rejects_delayed_pending_visibility():
    checked = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    aws = _HistoryAws([
        {"Commands": []}, {"CommandInvocations": []},
        {"Commands": [_history_command(status="Pending")]},
    ])
    evidence = {}
    with pytest.raises(shadow_rollout.RolloutError, match="legacy_commands_nonterminal"):
        shadow_rollout.attest_legacy_command_quiet(
            aws, evidence=evidence, **_drain_kwargs(checked)
        )
    assert evidence["scan_count"] == 2
    assert evidence["observed_settling_seconds"] == 30
    assert evidence["result"] == "FAIL"
    assert evidence["scans"][0]["result"] == "PASS"
    assert evidence["scans"][1]["aggregate_commands"]["result"] == "FAIL"


def test_executor_orders_host_install_document_default_and_final_readback(monkeypatch, tmp_path):
    _FakeAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", _FakeAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "audit.json"
    result = shadow_rollout.execute("a" * 40, "123", audit)
    calls = _FakeAws.instances[-1].calls
    rollout_attest = next(
        i for i, call in enumerate(calls)
        if call[0] == "call" and shadow_rollout.ROLLOUT_DOCUMENT in call[1]
    )
    first_send = next(i for i, call in enumerate(calls) if call[0] == "send")
    install = calls.index(("send", "install"))
    update = next(i for i, call in enumerate(calls) if call[0] == "call" and "update-document" in call[1])
    default = next(i for i, call in enumerate(calls) if call[0] == "call" and "update-document-default-version" in call[1])
    final_readback = len(calls) - 1 - calls[::-1].index(("send", "readback"))
    assert rollout_attest < first_send <= install < update < default < final_readback
    assert result["outcome"] == "applied"
    assert audit.stat().st_mode & 0o777 == 0o600
    evidence = json.loads(audit.read_text())
    assert evidence["semantic_readback"] is True
    assert evidence["byte_readback"] is True
    assert evidence["phase"] == "applied"
    assert evidence["commands"]
    assert evidence["host_new"]["worker_metadata_valid"] is True
    assert evidence["host_new"]["validator_metadata_valid"] is True
    assert evidence["host_new"]["binding_metadata_valid"] is True
    assert evidence["host_final"]["worker_owner"] == "0:0"
    assert evidence["host_final"]["validator_owner"] == "0:0"
    assert evidence["validator_sha256"] == shadow_rollout.sha256(
        shadow_rollout.VALIDATOR_PATH.read_bytes()
    )
    assert evidence["legacy_transition"]["mode"] == "steady"
    assert evidence["legacy_transition"]["result"] == "n-a"
    assert evidence["host_prestate_classification"] == "all-absent"
    assert evidence["preexisting_skew"] is False
    assert not any(
        call[0] == "call"
        and (
            "list-commands" in call[1]
            or "list-command-invocations" in call[1]
        )
        for call in calls
    )
    assert "AWS_SECRET_ACCESS_KEY" not in audit.read_text()


def test_incoherent_prestate_fails_before_install_and_records_existing_skew(
    monkeypatch, tmp_path,
):
    class IncoherentAws(_FakeAws):
        def send(self, action, rollout, expect_tuple=False):
            self.calls.append(("send", action, expect_tuple))
            assert action == "readback"
            evidence = _host_evidence(action, rollout, True)
            evidence["observed_validator_sha256"] = "0" * 64
            return evidence

    monkeypatch.setattr(shadow_rollout, "AwsCli", IncoherentAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "audit.json"
    with pytest.raises(shadow_rollout.RolloutError, match="host_prestate_incoherent"):
        shadow_rollout.execute("a" * 40, "123", audit)
    evidence = json.loads(audit.read_text())
    assert evidence["host_prestate_classification"] == "incoherent"
    assert evidence["preexisting_skew"] is True
    assert evidence["skew"] is True
    assert not any(call[:2] == ("send", "install") for call in IncoherentAws.instances[-1].calls)


def test_legacy_transition_drain_precedes_first_host_command(monkeypatch, tmp_path):
    class LegacyAws(_FakeAws):
        def call(self, args, write=False):
            if args[:2] in (
                ["ssm", "list-commands"],
                ["ssm", "list-command-invocations"],
            ):
                self.calls.append(("call", tuple(args), write))
                if args[:2] == ["ssm", "list-commands"]:
                    return {"Commands": []}
                return {"CommandInvocations": []}
            if args[:2] == ["ssm", "get-document"]:
                name = args[args.index("--name") + 1]
                version = args[args.index("--document-version") + 1]
                if name == shadow_rollout.SHADOW_DOCUMENT and version == "1":
                    self.calls.append(("call", tuple(args), write))
                    return {"Content": '{"schemaVersion":"2.2","description":"legacy"}'}
            return super().call(args, write=write)

    LegacyAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", LegacyAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "legacy-audit.json"
    drain_time = _drain_kwargs(
        datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)
    )
    result = shadow_rollout.execute(
        "a" * 40, "124", audit,
        drain_sleeper=drain_time["sleeper"],
        drain_monotonic=drain_time["monotonic"],
    )
    calls = LegacyAws.instances[-1].calls
    command_drains = [
        i for i, call in enumerate(calls)
        if call[0] == "call" and "list-commands" in call[1]
    ]
    invocation_drains = [
        i for i, call in enumerate(calls)
        if call[0] == "call" and "list-command-invocations" in call[1]
    ]
    first_send = next(i for i, call in enumerate(calls) if call[0] == "send")
    assert len(command_drains) == len(invocation_drains) == 3
    assert all(
        command < invocation
        for command, invocation in zip(command_drains, invocation_drains)
    )
    assert invocation_drains[-1] + 1 == first_send
    assert result["legacy_transition"]["mode"] == "legacy"
    assert result["legacy_transition"]["result"] == "PASS"
    assert result["legacy_transition"]["scan_count"] == 3
    assert result["legacy_transition"]["observed_settling_seconds"] == 60


def test_install_terminal_success_evidence_failure_reconciles_and_rolls_back(
    monkeypatch, tmp_path
):
    class FailureAws(_FakeAws):
        def __init__(self, deadline):
            super().__init__(deadline)
            self.readbacks = 0

        def _value(self, action, rollout, installed):
            return _host_evidence(action, rollout, installed)

        def send(self, action, rollout, expect_tuple=False):
            self.calls.append(("send", action))
            self.commands.append({
                "action": action, "command_id": "0" * 8 + "-0000-0000-0000-000000000001",
                "accepted": True, "status": "Success", "response_code": 0,
            })
            if action == "install":
                raise shadow_rollout.RolloutError("host_evidence_missing")
            if action == "readback":
                self.readbacks += 1
                return self._value(action, rollout, self.readbacks > 1)
            assert action == "rollback"
            return self._value(action, rollout, False)

    FailureAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", FailureAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "failed-audit.json"
    with pytest.raises(shadow_rollout.RolloutError, match="host_evidence_missing"):
        shadow_rollout.execute("a" * 40, "456", audit)
    evidence = json.loads(audit.read_text())
    assert evidence["phase"] == "rolled_back"
    assert evidence["skew"] is False
    assert evidence["host_final"]["observed_worker_sha256"] == ""
    assert any(item["action"] == "rollback" for item in evidence["commands"])


@pytest.mark.parametrize("terminal", ["success", "failed"])
def test_delayed_response_lost_install_is_terminal_before_adopt_or_rollback(
    monkeypatch, tmp_path, terminal,
):
    class DelayedAws(_FakeAws):
        def __init__(self, deadline):
            super().__init__(deadline)
            self.installed = False
            self.readbacks = 0

        def send(self, action, rollout, expect_tuple=False):
            self.calls.append(("send", action))
            if action == "install":
                self.commands.append({
                    "action": action, "command_id": None,
                    "accepted": "uncertain", "status": "unknown",
                    "response_code": None,
                    "comment": "kiwoom-shadow-rollout/789/local/install",
                })
                raise shadow_rollout.RolloutError("aws_command_failed")
            if action == "readback":
                self.readbacks += 1
                installed = self.installed
                self.commands.append({
                    "action": action, "command_id": f"readback-{self.readbacks}",
                    "accepted": True, "status": "Success", "response_code": 0,
                })
                return _host_evidence(action, rollout, installed)
            assert action == "rollback"
            self.installed = False
            self.commands.append({
                "action": action, "command_id": "rollback",
                "accepted": True, "status": "Success", "response_code": 0,
            })
            return _host_evidence(action, rollout, False)

        def reconcile_acceptance(
            self, action, rollout, expect_tuple=False, sleeper=lambda _: None,
        ):
            self.calls.append(("reconcile", action))
            assert self.readbacks == 2  # prestate, then immediate response-loss readback
            record = next(
                item for item in self.commands if item.get("action") == "install"
            )
            record.update({
                "accepted": "reconciled",
                "command_id": "00000000-0000-0000-0000-000000000789",
            })
            if terminal == "success":
                self.installed = True
                record.update({"status": "Success", "response_code": 0})
                return _host_evidence(action, rollout, True)
            self.installed = True  # partial host transaction requires exact rollback
            record.update({"status": "Failed", "response_code": 1})
            raise shadow_rollout.RolloutError("host_action_failed")

    DelayedAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", DelayedAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / f"delayed-{terminal}.json"
    if terminal == "success":
        result = shadow_rollout.execute(
            "a" * 40, "789", audit, acceptance_sleeper=lambda _: None,
        )
        assert result["outcome"] == "applied"
        assert result["host_observed_after_send_error"]["worker_present"] is False
        assert result["host_new"]["worker_present"] is True
        assert result["host_final"]["worker_present"] is True
    else:
        with pytest.raises(shadow_rollout.RolloutError, match="host_action_failed"):
            shadow_rollout.execute(
                "a" * 40, "789", audit, acceptance_sleeper=lambda _: None,
            )
        result = json.loads(audit.read_text())
        assert result["phase"] == "rolled_back"
        assert result["skew"] is False
        assert result["host_observed_after_send_error"]["worker_present"] is False
        assert result["host_reconciled_after_failure"]["worker_present"] is True
        assert result["host_final"]["worker_present"] is False


def test_unresolved_response_lost_install_never_claims_final_host(
    monkeypatch, tmp_path,
):
    class UnresolvedAws(_FakeAws):
        def __init__(self, deadline):
            super().__init__(deadline)
            self.readbacks = 0

        def send(self, action, rollout, expect_tuple=False):
            self.calls.append(("send", action))
            if action == "install":
                self.commands.append({
                    "action": action, "command_id": None,
                    "accepted": "uncertain", "status": "unknown",
                    "response_code": None,
                    "comment": "kiwoom-shadow-rollout/790/local/install",
                })
                raise shadow_rollout.RolloutError("aws_command_failed")
            assert action == "readback"
            self.readbacks += 1
            return _host_evidence(action, rollout, False)

        def reconcile_acceptance(
            self, action, rollout, expect_tuple=False, sleeper=lambda _: None,
        ):
            self.calls.append(("reconcile", action))
            raise shadow_rollout.RolloutError("acceptance_history_unresolved")

    UnresolvedAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", UnresolvedAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "unresolved.json"
    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_unresolved"
    ):
        shadow_rollout.execute(
            "a" * 40, "790", audit, acceptance_sleeper=lambda _: None,
        )
    result = json.loads(audit.read_text())
    assert result["host_observed_after_send_error"]["worker_present"] is False
    assert result["host_observed_after_uncertain"]["worker_present"] is False
    assert result["host_final"] is None
    assert result["rollback_failure_category"] == "install_acceptance_unresolved"
    assert result["phase"] == "skew"
    assert result["skew"] is True


def test_activation_attests_default_even_when_latest_differs(monkeypatch):
    class ActivationAws:
        def __init__(self, deadline): pass
        def call(self, args, write=False):
            if args[1] == "describe-document":
                return {"Document": {"Status": "Active", "DefaultVersion": "3", "LatestVersion": "4"}}
            raw = Path("deploy/ssm/shadow-worker-document.yaml").read_bytes()
            value, _ = shadow_rollout.canonical_json(raw)
            return {"Content": json.dumps(value)}
    raw = Path("deploy/ssm/shadow-worker-document.yaml").read_bytes()
    value, _ = shadow_rollout.canonical_json(raw)
    monkeypatch.setattr(shadow_rollout, "AwsCli", ActivationAws)
    assert shadow_rollout.attest_activation_document(
        shadow_rollout.sha256(shadow_rollout._canonical_bytes(value))
    ) == "3"


@pytest.mark.parametrize(
    ("status", "default", "latest"),
    [
        ("Creating", "1", "1"),
        ("Active", "2", "2"),
        ("Active", "1", "2"),
    ],
)
def test_rollout_attestation_rejects_non_active_or_non_v1_document(
    status, default, latest
):
    class DriftedRolloutAws:
        def call(self, args, write=False):
            assert args[:2] == ["ssm", "describe-document"]
            return {"Document": {
                "Status": status,
                "DefaultVersion": default,
                "LatestVersion": latest,
            }}

    with pytest.raises(
        shadow_rollout.RolloutError,
        match="rollout_document_version_invalid",
    ):
        shadow_rollout.attest_rollout_document(
            DriftedRolloutAws(), shadow_rollout.expected_rollout_document()
        )


def test_default_write_response_loss_is_authoritatively_reconciled():
    class AmbiguousDefaultAws:
        def __init__(self):
            self.default = "1"
        def call(self, args, write=False):
            if args[1] == "update-document-default-version":
                self.default = args[args.index("--document-version") + 1]
                raise shadow_rollout.RolloutError("aws_timeout")
            return {"Document": {"DefaultVersion": self.default}}
    assert shadow_rollout.set_default_reconciled(
        AmbiguousDefaultAws(), "7"
    ) == "ambiguous+readback"


def _load_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_shadow_rollout_test", "deploy/bootstrap_shadow_rollout.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_ambiguous_exact_creator_is_never_deleted_and_next_run_reuses(monkeypatch):
    module = _load_bootstrap_module()
    state = {"role": None, "policy": None, "document": False}
    deletes = []
    def fake_run(args, missing=None):
        operation = tuple(args[:2])
        if operation == ("accessanalyzer", "validate-policy"):
            return {"findings": []}
        if operation == ("iam", "get-role"):
            return None if state["role"] is None else {"Role": state["role"]}
        if operation == ("iam", "create-role"):
            trust = json.loads(args[args.index("--assume-role-policy-document") + 1])
            state["role"] = {"Arn": "arn:aws:iam::123456789012:role/" + module.ROLE_NAME,
                             "AssumeRolePolicyDocument": trust}
            raise module.BootstrapError("response lost")
        if operation == ("iam", "get-role-policy"):
            return None if state["policy"] is None else {"PolicyDocument": state["policy"]}
        if operation == ("iam", "put-role-policy"):
            state["policy"] = json.loads(args[args.index("--policy-document") + 1])
            return {}
        if operation == ("ssm", "describe-document"):
            return None if not state["document"] else {"Document": {"Status": "Active", "DefaultVersion": "1", "LatestVersion": "1"}}
        if operation == ("ssm", "create-document"):
            state["document"] = True
            return {}
        if operation == ("ssm", "get-document"):
            return {"Content": module.DOCUMENT.read_text(encoding="utf-8")}
        if operation in {
            ("iam", "delete-role"), ("iam", "delete-role-policy"),
            ("ssm", "delete-document"),
        }:
            deletes.append(operation)
            return {}
        raise AssertionError(args)
    monkeypatch.setattr(module, "_run", fake_run)
    with pytest.raises(module.BootstrapError, match="ownership-uncertain"):
        module.bootstrap("123456789012")
    assert deletes == []

    result = module.bootstrap("123456789012")
    assert result["status"] == "PASS"
    assert [item["resource"] for item in result["journal"]] == ["document", "policy"]
    assert [item["state"] for item in result["journal"]] == [
        "created-by-attempt", "commit-readback-exact",
    ]


def test_bootstrap_successful_policy_upsert_then_failure_never_deletes(monkeypatch):
    module = _load_bootstrap_module()
    trust, _ = module._render(module.TRUST, "123456789012")
    policy, _ = module._render(module.POLICY, "123456789012")
    state = {"policy": None}
    deletes = []

    def fake_run(args, missing=None):
        operation = tuple(args[:2])
        if operation == ("accessanalyzer", "validate-policy"):
            return {"findings": []}
        if operation == ("iam", "get-role"):
            if state["policy"] is not None:
                raise module.BootstrapError("later trust read failed")
            return {"Role": {
                "Arn": "arn:aws:iam::123456789012:role/" + module.ROLE_NAME,
                "AssumeRolePolicyDocument": trust,
            }}
        if operation == ("iam", "get-role-policy"):
            return None if state["policy"] is None else {"PolicyDocument": state["policy"]}
        if operation == ("iam", "put-role-policy"):
            state["policy"] = policy
            return {}
        if operation == ("ssm", "describe-document"):
            return {"Document": {"Status": "Active", "DefaultVersion": "1", "LatestVersion": "1"}}
        if operation == ("ssm", "get-document"):
            return {"Content": module.DOCUMENT.read_text(encoding="utf-8")}
        if "delete" in operation[1]:
            deletes.append(operation)
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(module, "_run", fake_run)
    with pytest.raises(module.BootstrapError, match="commit-boundary-no-delete"):
        module.bootstrap("123456789012")
    assert deletes == []


def test_bootstrap_concurrent_exact_policy_then_final_failure_never_deletes(monkeypatch):
    module = _load_bootstrap_module()
    trust, _ = module._render(module.TRUST, "123456789012")
    policy, _ = module._render(module.POLICY, "123456789012")
    state = {"role": None, "document": False, "policy_observed": False}
    deletes = []
    puts = []

    def fake_run(args, missing=None):
        operation = tuple(args[:2])
        if operation == ("accessanalyzer", "validate-policy"):
            return {"findings": []}
        if operation == ("iam", "get-role"):
            if state["policy_observed"]:
                raise module.BootstrapError("final trust read failed")
            return None if state["role"] is None else {"Role": state["role"]}
        if operation == ("iam", "create-role"):
            state["role"] = {
                "Arn": "arn:aws:iam::123456789012:role/" + module.ROLE_NAME,
                "AssumeRolePolicyDocument": trust,
            }
            return {}
        if operation == ("iam", "get-role-policy"):
            state["policy_observed"] = True
            return {"PolicyDocument": policy}
        if operation == ("iam", "put-role-policy"):
            puts.append(operation)
            return {}
        if operation == ("ssm", "describe-document"):
            if not state["document"]:
                return None
            return {"Document": {
                "Status": "Active", "DefaultVersion": "1", "LatestVersion": "1",
            }}
        if operation == ("ssm", "create-document"):
            state["document"] = True
            return {}
        if operation == ("ssm", "get-document"):
            return {"Content": module.DOCUMENT.read_text(encoding="utf-8")}
        if "delete" in operation[1]:
            deletes.append(operation)
            return {}
        raise AssertionError(args)

    monkeypatch.setattr(module, "_run", fake_run)
    with pytest.raises(module.BootstrapError, match="commit-boundary-no-delete"):
        module.bootstrap("123456789012")
    assert puts == []
    assert deletes == []


def test_bootstrap_cleanup_attempts_all_owned_resources_after_delete_failure(monkeypatch):
    module = _load_bootstrap_module()
    attempted = []
    def fake_run(args, missing=None):
        attempted.append(tuple(args[:2]))
        if args[:2] == ["ssm", "delete-document"]:
            raise module.BootstrapError("delete failed")
        return {}
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_document_once", lambda: {"Status": "Active"})
    monkeypatch.setattr(module, "_policy", lambda: None)
    monkeypatch.setattr(module, "_role", lambda: None)
    cleanup, orphans = module._cleanup({"document": True, "policy": True, "role": True})
    assert cleanup["document"] == "delete-error"
    assert orphans == ["document"]
    assert ("iam", "delete-role-policy") in attempted
    assert ("iam", "delete-role") in attempted
