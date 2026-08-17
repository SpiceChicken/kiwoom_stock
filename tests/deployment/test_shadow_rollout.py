"""Contracts for the exact protected shadow rollout plane."""

import json
import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from kiwoom_stock.deployment import shadow_rollout


WORKFLOW = Path(".github/workflows/cd-shadow-worker-rollout.yml")
ROLLOUT_DOCUMENT = Path("deploy/ssm/shadow-worker-rollout-document.yaml")
WORKER = Path("deploy/ec2/shadow_worker_control.sh")
VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")


@pytest.fixture(autouse=True)
def _isolate_github_run_attempt(monkeypatch):
    """Keep local fallback assertions independent of GitHub runner metadata."""

    monkeypatch.delenv("GITHUB_RUN_ATTEMPT", raising=False)


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
        "fixed_container_recovery": (
            "absent" if action == "install" else "not-requested"
        ),
    }


def _poll_response(rollout, command_id, **values):
    return {
        "CommandId": command_id,
        "InstanceId": shadow_rollout.INSTANCE_ID,
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "DocumentVersion": rollout.rollout_document_version or "1",
        **values,
    }


def _send_response(rollout, command_id, action="install"):
    return {"Command": {
        "CommandId": command_id,
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "DocumentVersion": rollout.rollout_document_version,
        "Comment": shadow_rollout._rollout_comment(action, rollout),
        "Parameters": shadow_rollout._rollout_parameters(action, rollout),
        "InstanceIds": [shadow_rollout.INSTANCE_ID],
        "Targets": [],
        "Status": "Pending",
    }}


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
        rollout_document_version="2",
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
        "group": "kiwoom-stock-shadow-i-0e42e09d6c087ba29",
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
    assert "api.github.com/repos/SpiceChicken/kiwoom_stock/contents/${path}?ref=${source_sha}" in text
    assert "download_github_file deploy/ec2/shadow_worker_control.sh \"$downloaded\"" in text
    assert "download_github_file deploy/ec2/shadow_runtime_evidence.py \"$validator_downloaded\"" in text
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
        "SSM_ExpectedInstanceId": "i-0e42e09d6c087ba29",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", _rendered_rollout_command()], env=environment,
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 64
    assert completed.stderr == "unsupported rollout action: unsupported-test-action\n"


_DEFAULT_CAPABILITY_VALUE = object()


def _run_fixed_container_guard(
    tmp_path, lifecycle, *, cap_drop=_DEFAULT_CAPABILITY_VALUE,
    cap_add=_DEFAULT_CAPABILITY_VALUE,
):
    worker = tmp_path / "worker"
    validator = tmp_path / "validator.py"
    binding = tmp_path / "binding.json"
    state = tmp_path / "state"
    lock = tmp_path / "rollout.lock"
    worker_bytes = b"old-worker"
    validator_bytes = b"old-validator"
    worker.write_bytes(worker_bytes)
    worker.chmod(0o750)
    validator.write_bytes(validator_bytes)
    validator.chmod(0o750)
    binding.write_text(json.dumps({
        "source_sha": "a" * 40,
        "worker_sha256": shadow_rollout.sha256(worker_bytes),
        "validator_sha256": shadow_rollout.sha256(validator_bytes),
        "shadow_document_sha256": "d" * 64,
        "rollout_attempt_id": "111",
    }), encoding="utf-8")
    binding.chmod(0o600)
    if lifecycle == "artifact-metadata":
        worker.chmod(0o700)
    elif lifecycle == "binding-shape":
        binding.write_text("[]", encoding="utf-8")
    elif lifecycle == "binding-value":
        value = json.loads(binding.read_text(encoding="utf-8"))
        value["source_sha"] = "invalid"
        binding.write_text(json.dumps(value), encoding="utf-8")
    elif lifecycle == "binding-value-type":
        value = json.loads(binding.read_text(encoding="utf-8"))
        value["source_sha"] = 123
        binding.write_text(json.dumps(value), encoding="utf-8")
    elif lifecycle == "worker-hash":
        value = json.loads(binding.read_text(encoding="utf-8"))
        value["worker_sha256"] = "f" * 64
        binding.write_text(json.dumps(value), encoding="utf-8")
    elif lifecycle == "validator-hash":
        value = json.loads(binding.read_text(encoding="utf-8"))
        value["validator_sha256"] = "f" * 64
        binding.write_text(json.dumps(value), encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl_marker = tmp_path / "curl-called"
    removed_marker = tmp_path / "container-removed"
    docker = tools / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "lifecycle=os.environ['FIXED_CONTAINER_LIFECYCLE']\n"
        "removed=pathlib.Path(os.environ['FIXED_CONTAINER_REMOVED'])\n"
        "if args[:2]==['container','ls']:\n"
        "    if lifecycle=='operational-error': raise SystemExit(55)\n"
        "    if not removed.exists(): print('kiwoom-shadow-once')\n"
        "    raise SystemExit(0)\n"
        "if args[:2]==['container','inspect']:\n"
        "    if lifecycle=='inspect-shape': print('[]'); raise SystemExit(0)\n"
        "    image='ghcr.io/spicechicken/kiwoom_stock@sha256:'+'e'*64\n"
        "    activation='bounded-recovery-1'\n"
        "    labels={'io.kiwoom.shadow.source-sha':'a'*40,'io.kiwoom.shadow.image-digest':image,'io.kiwoom.shadow.activation-id':activation,'io.kiwoom.shadow.mode':'shadow-continuous'}\n"
        "    if lifecycle=='label-missing': labels.pop('io.kiwoom.shadow.activation-id')\n"
        "    if lifecycle=='label-mismatch': labels['io.kiwoom.shadow.source-sha']='f'*40\n"
        "    running=lifecycle=='running'\n"
        "    cap_drop=json.loads(os.environ['FIXED_CONTAINER_CAP_DROP'])\n"
        "    cap_add=json.loads(os.environ['FIXED_CONTAINER_CAP_ADD'])\n"
        "    value={'Name':'/kiwoom-shadow-once','State':{'Running':running,'Status':'running' if running else 'exited'},'Config':{'User':'0:0','Labels':labels,'Image':image,'Cmd':['python','-m','kiwoom_stock','shadow-worker','--source-sha','a'*40,'--image-digest',image,'--activation-id',activation]},'HostConfig':{'ReadonlyRootfs':True,'RestartPolicy':{'Name':'no'},'CapDrop':cap_drop,'CapAdd':cap_add,'SecurityOpt':['no-new-privileges:true']}}\n"
        "    if lifecycle=='config-shape': value['Config']['User']='1000:1000'\n"
        "    if lifecycle=='labels-shape': value['Config']['Labels']=[]\n"
        "    if lifecycle=='image': value['Config']['Image']='invalid-image'\n"
        "    if lifecycle=='command': value['Config']['Cmd'].append('--unexpected')\n"
        "    if lifecycle=='runtime-security': value['HostConfig']['ReadonlyRootfs']=False\n"
        "    if lifecycle=='capabilities': value['HostConfig']['CapAdd']=[]\n"
        "    if lifecycle=='no-new-privileges': value['HostConfig']['SecurityOpt']=[]\n"
        "    print(json.dumps([value],separators=(',',':')))\n"
        "    raise SystemExit(0)\n"
        "if args[:2]==['rm','--'] and lifecycle=='valid-stopped':\n"
        "    removed.touch()\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(91)\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    curl = tools / "curl"
    curl.write_text(
        f"#!/usr/bin/env bash\ntouch '{curl_marker}'\nexit 99\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
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
        "value.st_uid==0", f"value.st_uid=={uid}"
    ).replace(
        "value.st_gid==0", f"value.st_gid=={gid}"
    ).replace(
        'if [[ "$action" == install ]]; then prepare_fixed_container_for_install; fi',
        'if [[ "$action" == install ]]; then prepare_fixed_container_for_install; exit 77; fi',
    )
    environment = dict(os.environ)
    environment.update({
        "PATH": f"{tools}:{environment['PATH']}",
        "FIXED_CONTAINER_LIFECYCLE": lifecycle,
        "FIXED_CONTAINER_REMOVED": str(removed_marker),
        "FIXED_CONTAINER_CAP_DROP": json.dumps(
            ["ALL"] if cap_drop is _DEFAULT_CAPABILITY_VALUE else cap_drop
        ),
        "FIXED_CONTAINER_CAP_ADD": json.dumps(
            ["CHOWN", "SETGID", "SETUID"]
            if cap_add is _DEFAULT_CAPABILITY_VALUE else cap_add
        ),
        "SSM_Action": "install", "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": "b" * 64, "SSM_ValidatorSha256": "c" * 64,
        "SSM_ShadowDocumentSha256": "d" * 64,
        "SSM_RolloutAttemptId": "321",
        "SSM_ExpectedInstanceId": "i-0e42e09d6c087ba29",
        "SSM_Region": "ap-northeast-2",
    })
    completed = subprocess.run(
        ["bash", "-c", command], env=environment, check=False,
        capture_output=True, text=True, timeout=10,
    )
    return completed, worker, validator, binding, curl_marker, removed_marker, state


@pytest.mark.parametrize(("lifecycle", "failure_code"), [
    ("running", "lifecycle"),
    ("label-missing", "activation"),
    ("label-mismatch", "source_mode"),
    ("operational-error", None),
])
def test_untrusted_fixed_container_blocks_before_backup_download_publish(
    tmp_path, lifecycle, failure_code,
):
    (completed, worker, validator, binding, curl_marker, removed_marker,
     state) = _run_fixed_container_guard(tmp_path, lifecycle)
    assert completed.returncode != 0
    if lifecycle == "operational-error":
        assert "docker fixed shadow inventory failed" in completed.stderr
    else:
        assert completed.stderr.splitlines() == [
            f"fixed-identity:{failure_code}",
            "fixed stopped shadow identity validation failed",
        ]
    assert worker.read_bytes() == b"old-worker"
    assert validator.read_bytes() == b"old-validator"
    assert json.loads(binding.read_text(encoding="utf-8"))["source_sha"] == "a" * 40
    assert not curl_marker.exists()
    assert not removed_marker.exists()
    assert not (state / "321").exists()


@pytest.mark.parametrize(("lifecycle", "failure_code"), [
    ("artifact-metadata", "artifact_metadata"),
    ("binding-shape", "binding_shape"),
    ("binding-value", "binding_value"),
    ("binding-value-type", "binding_value"),
    ("worker-hash", "worker_hash"),
    ("validator-hash", "validator_hash"),
    ("inspect-shape", "inspect_shape"),
    ("running", "lifecycle"),
    ("config-shape", "config_shape"),
    ("labels-shape", "labels_shape"),
    ("label-mismatch", "source_mode"),
    ("image", "image"),
    ("label-missing", "activation"),
    ("command", "command"),
    ("runtime-security", "runtime_security"),
    ("capabilities", "capabilities"),
    ("no-new-privileges", "no_new_privileges"),
])
def test_fixed_container_identity_failures_emit_only_allowlisted_category(
    tmp_path, lifecycle, failure_code,
):
    completed, *_ = _run_fixed_container_guard(tmp_path, lifecycle)
    assert completed.returncode != 0
    assert completed.stderr.splitlines() == [
        f"fixed-identity:{failure_code}",
        "fixed stopped shadow identity validation failed",
    ]


def test_exact_stopped_fixed_container_is_removed_before_install(tmp_path):
    (completed, _worker, _validator, _binding, curl_marker, removed_marker,
     state) = _run_fixed_container_guard(tmp_path, "valid-stopped")
    assert completed.returncode == 77
    assert completed.stderr == ""
    assert removed_marker.is_file()
    assert not curl_marker.exists()
    assert not (state / "321").exists()


@pytest.mark.parametrize("cap_add", [
    ["CHOWN", "SETGID", "SETUID"],
    ["SETUID", "CHOWN", "SETGID"],
    ["CAP_CHOWN", "CAP_SETGID", "CAP_SETUID"],
    ["CAP_SETUID", "CAP_CHOWN", "CAP_SETGID"],
])
def test_fixed_container_accepts_legacy_or_docker_28_canonical_capabilities(
    tmp_path, cap_add,
):
    completed, *_rest, removed_marker, state = _run_fixed_container_guard(
        tmp_path, "valid-stopped", cap_add=cap_add,
    )

    assert completed.returncode == 77
    assert completed.stderr == ""
    assert removed_marker.is_file()
    assert not (state / "321").exists()


@pytest.mark.parametrize(("cap_drop", "cap_add"), [
    (None, ["CHOWN", "SETGID", "SETUID"]),
    (True, ["CHOWN", "SETGID", "SETUID"]),
    (1, ["CHOWN", "SETGID", "SETUID"]),
    ({}, ["CHOWN", "SETGID", "SETUID"]),
    ([True], ["CHOWN", "SETGID", "SETUID"]),
    ([1], ["CHOWN", "SETGID", "SETUID"]),
    (["ALL", "ALL"], ["CHOWN", "SETGID", "SETUID"]),
    (["all"], ["CHOWN", "SETGID", "SETUID"]),
    ([], ["CHOWN", "SETGID", "SETUID"]),
    ("ALL", ["CHOWN", "SETGID", "SETUID"]),
    (["ALL"], None),
    (["ALL"], True),
    (["ALL"], 1),
    (["ALL"], {}),
    (["ALL"], [True, "SETGID", "SETUID"]),
    (["ALL"], [1, "SETGID", "SETUID"]),
    (["ALL"], ["CAP_CHOWN", "SETGID", "SETUID"]),
    (["ALL"], ["CHOWN", "SETGID", "SETGID"]),
    (["ALL"], ["CHOWN", "SETGID", "SETUID", "NET_RAW"]),
    (["ALL"], ["chown", "SETGID", "SETUID"]),
    (["ALL"], ["", "SETGID", "SETUID"]),
    (["ALL"], ["CHOWN", 1, "SETUID"]),
    (["ALL"], []),
    (["ALL"], "CHOWN,SETGID,SETUID"),
])
def test_fixed_container_rejects_nonexact_capability_contract(
    tmp_path, cap_drop, cap_add,
):
    completed, *_ = _run_fixed_container_guard(
        tmp_path, "valid-stopped", cap_drop=cap_drop, cap_add=cap_add,
    )

    assert completed.returncode != 0
    assert completed.stderr.splitlines() == [
        "fixed-identity:capabilities",
        "fixed stopped shadow identity validation failed",
    ]


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
        "import base64, json, os, sys\n"
        "destination=sys.argv[sys.argv.index('-o')+1]\n"
        "is_validator='shadow_runtime_evidence.py' in ' '.join(sys.argv)\n"
        "source=os.environ['FAKE_VALIDATOR'] if is_validator else os.environ['FAKE_WORKER']\n"
        "path='deploy/ec2/shadow_runtime_evidence.py' if is_validator else 'deploy/ec2/shadow_worker_control.sh'\n"
        "with open(source, 'rb') as stream: content=base64.b64encode(stream.read()).decode()\n"
        "with open(destination, 'w', encoding='utf-8') as stream: json.dump({'type':'file','path':path,'encoding':'base64','content':content}, stream)\n",
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
        "SSM_ExpectedInstanceId": "i-0e42e09d6c087ba29",
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
    assert host_evidence["fixed_container_recovery"] == "absent"
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
        "SSM_ExpectedInstanceId": "i-0e42e09d6c087ba29",
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
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    evidence = _host_evidence("install", rollout, True)
    responses = iter([
        subprocess.CompletedProcess([], 0, json.dumps({"Document": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "Status": "Active", "DefaultVersion": "2", "LatestVersion": "2",
        }}), ""),
        subprocess.CompletedProcess([], 0, json.dumps(_send_response(
            rollout, command_id
        )), ""),
        subprocess.CompletedProcess([], 0, json.dumps(_poll_response(
            rollout, command_id,
            Status="Success", ResponseCode=0,
            StandardOutputContent=json.dumps(evidence),
        )), ""),
    ])
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return next(responses)
    monkeypatch.setattr(shadow_rollout.subprocess, "run", fake_run)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    assert adapter.send("install", rollout, expect_tuple=True) == evidence
    assert calls[1][0][0:3] == ["aws", "ssm", "send-command"]
    assert calls[1][0][calls[1][0].index("--document-version") + 1] == "2"
    assert calls[1][1]["AWS_MAX_ATTEMPTS"] == "1"
    assert calls[1][0][calls[1][0].index("--comment") + 1] == (
        "kiwoom-shadow-rollout/123/local/install"
    )
    assert json.loads(
        calls[1][0][calls[1][0].index("--parameters") + 1]
    ) == shadow_rollout._rollout_parameters("install", rollout)
    assert adapter.commands == [{
        "action": "install", "command_id": command_id, "accepted": True,
        "status": "Success", "response_code": 0,
        "comment": "kiwoom-shadow-rollout/123/local/install",
        "document_version": "2",
    }]


@pytest.mark.parametrize(("action", "recovery"), [
    ("install", "not-requested"),
    ("readback", "removed"),
    ("rollback", "absent"),
    ("install", "unknown"),
])
def test_host_evidence_recovery_is_action_bound(monkeypatch, action, recovery):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    evidence = _host_evidence(action, rollout, True)
    evidence["fixed_container_recovery"] = recovery
    command_id = "00000000-0000-0000-0000-000000000001"
    invocation = _poll_response(
        rollout, command_id, Status="Success", ResponseCode=0,
        StandardOutputContent=json.dumps(evidence),
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "poll", lambda *_args: invocation)
    with pytest.raises(
        shadow_rollout.RolloutError, match="host_evidence_recovery_invalid"
    ):
        adapter._finish_command(
            action, rollout, {}, command_id, expect_tuple=True,
        )


def test_host_identity_excludes_install_recovery_observation():
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    absent = _host_evidence("install", rollout, True)
    removed = dict(absent, fixed_container_recovery="removed")
    assert shadow_rollout._host_identity(absent) == shadow_rollout._host_identity(
        removed
    )


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
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    evidence = _host_evidence("install", rollout, True)
    command_id = "00000000-0000-0000-0000-000000000001"
    responses = iter([
        _poll_response(rollout, command_id, Status=nonterminal),
        _poll_response(
            rollout, command_id, Status=terminal, ResponseCode=response_code,
            StandardOutputContent=json.dumps(evidence),
        ),
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
            command_id, expect_tuple=True,
        ) == evidence
    else:
        with pytest.raises(shadow_rollout.RolloutError, match="host_action_failed"):
            adapter._finish_command(
                "install", rollout, record,
                command_id, expect_tuple=True,
            )
    assert len(calls) == 2
    assert record["status"] == terminal
    assert record["response_code"] == response_code
    if not succeeds:
        assert record["failure_category"] == "host_action_failed"


@pytest.mark.parametrize(
    ("marker", "category"),
    sorted(shadow_rollout.FIXED_IDENTITY_FAILURE_CATEGORIES.items()),
)
def test_host_action_failure_category_accepts_one_exact_install_marker(
    marker, category,
):
    invocation = {
        "Status": "Failed",
        "ResponseCode": 1,
        "StandardErrorContent": (
            f"{marker}\nfixed stopped shadow identity validation failed\n"
        )
    }
    assert shadow_rollout._host_action_failure_category(
        "install", invocation
    ) == category


@pytest.mark.parametrize("stderr", [
    (
        "ssm wrapper before\n"
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
    ),
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "ssm wrapper after\n"
    ),
    (
        "ssm wrapper before\n"
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "ssm wrapper after\n"
    ),
])
def test_host_action_failure_category_ignores_untrusted_non_marker_wrapper_lines(
    stderr,
):
    assert shadow_rollout._host_action_failure_category(
        "install", {
            "Status": "Failed", "ResponseCode": 1,
            "StandardErrorContent": stderr,
        }
    ) == "host_fixed_identity_lifecycle"


@pytest.mark.parametrize("stderr", [
    "fixed-identity:unknown\n",
    "fixed-identity:lifecycle\n",
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "fixed-identity:unknown\n"
    ),
    "fixed-identity:lifecycle\nfixed-identity:lifecycle\n",
    "fixed-identity:lifecycle\nfixed-identity:image\n",
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "fixed stopped shadow identity validation failed\n"
    ),
    (
        "fixed-identity:lifecycle\n"
        "ssm wrapper displaced companion\n"
        "fixed stopped shadow identity validation failed\n"
    ),
    "prefix fixed-identity:lifecycle suffix\n",
    "x" * 65537,
    None,
])
def test_host_action_failure_category_rejects_unknown_ambiguous_or_unbounded_stderr(
    stderr,
):
    assert shadow_rollout._host_action_failure_category(
        "install", {
            "Status": "Failed", "ResponseCode": 1,
            "StandardErrorContent": stderr,
        }
    ) == "host_action_failed"


def test_host_action_failure_category_is_install_only():
    assert shadow_rollout._host_action_failure_category(
        "readback", {
            "Status": "Failed", "ResponseCode": 1,
            "StandardErrorContent": (
                "fixed-identity:lifecycle\n"
                "fixed stopped shadow identity validation failed\n"
            ),
        }
    ) == "host_action_failed"


@pytest.mark.parametrize(
    ("status", "response_code"),
    [("Cancelled", 1), ("TimedOut", 1), ("Failed", 2), ("Success", 1)],
)
def test_host_action_failure_category_requires_exact_failed_exit_one(
    status, response_code,
):
    assert shadow_rollout._host_action_failure_category(
        "install", {
            "Status": status,
            "ResponseCode": response_code,
            "StandardErrorContent": (
                "fixed-identity:lifecycle\n"
                "fixed stopped shadow identity validation failed\n"
            ),
        }
    ) == "host_action_failed"


def test_finish_command_records_only_safe_fixed_identity_failure(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    invocation = _poll_response(
        rollout, command_id, Status="Failed", ResponseCode=1,
        StandardErrorContent=(
            "fixed-identity:runtime_security\n"
            "fixed stopped shadow identity validation failed\n"
        ),
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "poll", lambda command_id, rollout: invocation)
    record = {}
    with pytest.raises(
        shadow_rollout.RolloutError,
        match="host_fixed_identity_runtime_security",
    ):
        adapter._finish_command(
            "install", rollout, record, command_id, expect_tuple=True,
        )
    assert record == {
        "status": "Failed",
        "response_code": 1,
        "failure_category": "host_fixed_identity_runtime_security",
    }


@pytest.mark.parametrize("response_code", [False, 0.0, "0"])
def test_finish_command_rejects_non_exact_integer_response_code(
    monkeypatch, response_code,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    invocation = _poll_response(
        rollout, command_id, Status="Success", ResponseCode=response_code,
        StandardOutputContent=json.dumps(_host_evidence("install", rollout, True)),
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "poll", lambda command_id, rollout: invocation)
    record = {}
    with pytest.raises(shadow_rollout.RolloutError, match="host_action_failed"):
        adapter._finish_command(
            "install", rollout, record,
            command_id, expect_tuple=True,
        )
    assert record["status"] == "Success"
    assert record["response_code"] == response_code


def _acceptance_command(rollout, command_id, *, parameters=None):
    return {
        "CommandId": command_id,
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "DocumentVersion": rollout.rollout_document_version or "1",
        "Comment": "kiwoom-shadow-rollout/123/local/install",
        "Parameters": parameters or shadow_rollout._rollout_parameters(
            "install", rollout
        ),
        "InstanceIds": [shadow_rollout.INSTANCE_ID],
        "Targets": [],
        "RequestedDateTime": "2026-08-09T12:00:00+00:00",
        "Status": "InProgress",
    }


def _acceptance_invocation(command_id, version="1"):
    return {
        "CommandId": command_id,
        "InstanceId": shadow_rollout.INSTANCE_ID,
        "Comment": "kiwoom-shadow-rollout/123/local/install",
        "DocumentName": shadow_rollout.ROLLOUT_DOCUMENT,
        "DocumentVersion": version,
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
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    evidence = _host_evidence("install", rollout, True)
    responses = iter([
        subprocess.CompletedProcess([], 0, json.dumps({"Document": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "Status": "Active", "DefaultVersion": "2", "LatestVersion": "2",
        }}), ""),
        subprocess.CompletedProcess([], 1, "", "response lost"),
        subprocess.CompletedProcess([], 0, json.dumps({"Commands": []}), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "Commands": [_acceptance_command(rollout, command_id)],
        }), ""),
        subprocess.CompletedProcess([], 0, json.dumps({
            "CommandInvocations": [_acceptance_invocation(command_id, "2")],
        }), ""),
        subprocess.CompletedProcess([], 0, json.dumps(_poll_response(
            rollout, command_id, Status="Success", ResponseCode=0,
            StandardOutputContent=json.dumps(evidence),
        )), ""),
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
        document_version="1",
    ) == [exact_id]
    assert rollout_comment == "kiwoom-shadow-rollout/123/local/install"
    assert "--no-paginate" in calls[0]
    assert "--next-token" not in calls[0]
    assert calls[1][calls[1].index("--next-token") + 1] == "p2"


@pytest.mark.parametrize("split_pages", [False, True])
def test_acceptance_history_ignores_unrelated_v1_and_finds_exact_vn(
    monkeypatch, split_pages,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    exact_id = "00000000-0000-0000-0000-000000000011"
    old_id = "00000000-0000-0000-0000-000000000012"
    exact = _acceptance_command(rollout, exact_id)
    old = {
        **_acceptance_command(rollout, old_id),
        "DocumentVersion": "1",
        "Comment": "unrelated-historical-command",
    }
    pages = (
        [{"Commands": [old], "NextToken": "p2"}, {"Commands": [exact]}]
        if split_pages else [{"Commands": [old, exact]}]
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    responses = iter(pages)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(responses))

    assert adapter._acceptance_commands(
        shadow_rollout._rollout_comment("install", rollout),
        shadow_rollout._rollout_parameters("install", rollout),
        document_version="2",
    ) == [exact_id]


@pytest.mark.parametrize("split_pages", [False, True])
def test_acceptance_history_rejects_exact_tuple_on_wrong_version(
    monkeypatch, split_pages,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    wrong = {
        **_acceptance_command(
            rollout, "00000000-0000-0000-0000-000000000021"
        ),
        "DocumentVersion": "1",
    }
    unrelated = {
        **_acceptance_command(
            rollout, "00000000-0000-0000-0000-000000000022"
        ),
        "Comment": "unrelated-current-command",
    }
    pages = (
        [{"Commands": [unrelated], "NextToken": "p2"}, {"Commands": [wrong]}]
        if split_pages else [{"Commands": [unrelated, wrong]}]
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    responses = iter(pages)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: next(responses))

    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_tuple_mismatch"
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
            document_version="2",
        )


@pytest.mark.parametrize("invalid_version", [None, 0, "0", "01"])
def test_acceptance_history_rejects_invalid_unrelated_document_version(
    monkeypatch, invalid_version,
):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command = {
        **_acceptance_command(
            rollout, "00000000-0000-0000-0000-000000000031"
        ),
        "DocumentVersion": invalid_version,
        "Comment": "unrelated-historical-command",
    }
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(
        adapter, "call", lambda args, write=False: {"Commands": [command]}
    )

    with pytest.raises(
        shadow_rollout.RolloutError, match="acceptance_history_item_invalid"
    ):
        adapter._acceptance_commands(
            shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout),
            document_version="2",
        )


def test_acceptance_history_rejects_exact_matches_split_across_pages(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="1",
        rollout_document_canonical_sha256="9" * 64,
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
            document_version="1",
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
            document_version="1",
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
            document_version="1",
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
            document_version="1",
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
            document_version="1",
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
        rollout_document_version="1",
        rollout_document_canonical_sha256="9" * 64,
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
        rollout_document_version="1",
        rollout_document_canonical_sha256="9" * 64,
    )
    def fake_run(argv, **kwargs):
        if argv[1:3] == ["ssm", "describe-document"]:
            return subprocess.CompletedProcess([], 0, json.dumps({"Document": {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "Status": "Active", "DefaultVersion": "1", "LatestVersion": "1",
            }}), "")
        return subprocess.CompletedProcess([], 1, "", "timeout")

    monkeypatch.setattr(shadow_rollout.subprocess, "run", fake_run)
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    with pytest.raises(shadow_rollout.RolloutError, match="aws_command_failed"):
        adapter.send("install", rollout)
    assert adapter.commands == [{
        "action": "install", "command_id": None, "accepted": "uncertain",
        "status": "unknown", "response_code": None,
        "comment": "kiwoom-shadow-rollout/123/local/install",
        "document_version": "1",
    }]


class _FakeAws:
    instances: list[object] = []

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
                return {"Document": {
                    "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                    "DefaultVersion": "1", "LatestVersion": "1", "Status": "Active",
                }}
            return {"Document": {"DefaultVersion": self.default, "LatestVersion": "2", "Status": "Active"}}
        if args[:2] == ["ssm", "get-document"]:
            name = args[args.index("--name") + 1]
            if name == shadow_rollout.ROLLOUT_DOCUMENT:
                return {
                    "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                    "DocumentVersion": "1", "DocumentFormat": "JSON",
                    "Status": "Active",
                    "Content": json.dumps(shadow_rollout.expected_rollout_document()),
                }
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
    assert evidence["fixed_container_recovery"] == "absent"
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


@pytest.mark.parametrize(("stderr", "failure_category", "forbidden"), [
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n",
        "host_fixed_identity_lifecycle",
        "StandardErrorContent",
    ),
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "sentinel raw host detail\n",
        "host_fixed_identity_lifecycle",
        "sentinel raw host detail",
    ),
    (
        "fixed-identity:lifecycle\n"
        "fixed stopped shadow identity validation failed\n"
        "fixed-identity:unknown\n"
        "sentinel raw host detail\n",
        "host_action_failed",
        "sentinel raw host detail",
    ),
])
def test_failed_invocation_persists_only_safe_category_not_stderr(
    monkeypatch, tmp_path, stderr, failure_category, forbidden,
):
    finish_command = shadow_rollout.AwsCli._finish_command

    class PersistedFailureAws(_FakeAws):
        def send(self, action, rollout, expect_tuple=False):
            self.calls.append(("send", action))
            command_id = (
                f"00000000-0000-0000-0000-{len(self.command_ids):012d}"
            )
            self.command_ids.append(command_id)
            record = {
                "action": action, "command_id": command_id,
                "accepted": True, "status": "unknown", "response_code": None,
            }
            self.commands.append(record)
            if action == "install":
                return finish_command(
                    self, action, rollout, record, command_id,
                    expect_tuple=expect_tuple,
                )
            record["status"] = "Success"
            record["response_code"] = 0
            return _host_evidence(action, rollout, False)

        def poll(self, command_id, rollout):
            return _poll_response(
                rollout, command_id, Status="Failed", ResponseCode=1,
                StandardErrorContent=stderr,
            )

    PersistedFailureAws.instances.clear()
    monkeypatch.setattr(shadow_rollout, "AwsCli", PersistedFailureAws)
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    audit = tmp_path / "failed-invocation-audit.json"
    with pytest.raises(shadow_rollout.RolloutError, match=failure_category):
        shadow_rollout.execute("a" * 40, "125", audit)
    encoded = audit.read_text(encoding="utf-8")
    evidence = json.loads(encoded)
    assert evidence["failure_category"] == failure_category
    install = next(
        item for item in evidence["commands"] if item["action"] == "install"
    )
    assert install["failure_category"] == failure_category
    assert "StandardErrorContent" not in encoded
    assert forbidden not in encoded
    assert evidence["phase"] == "rolled_back"
    assert evidence["skew"] is False


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
        ("Active", "1", "2"),
    ],
)
def test_rollout_attestation_rejects_non_active_or_mismatched_document(
    status, default, latest
):
    class DriftedRolloutAws:
        def call(self, args, write=False):
            assert args[:2] == ["ssm", "describe-document"]
            return {"Document": {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "Status": status,
                "DefaultVersion": default,
                "LatestVersion": latest,
            }}

    with pytest.raises(
        shadow_rollout.RolloutError,
        match="document_description_invalid|rollout_document_version_invalid",
    ):
        shadow_rollout.attest_rollout_document(
            DriftedRolloutAws(), shadow_rollout.expected_rollout_document()
        )


def test_rollout_attestation_accepts_exact_active_v2_and_returns_binding():
    expected = shadow_rollout.expected_rollout_document()

    class ExactV2Aws:
        def call(self, args, write=False):
            if args[:2] == ["ssm", "describe-document"]:
                return {"Document": {
                    "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                    "Status": "Active", "DefaultVersion": "2", "LatestVersion": "2",
                }}
            assert args[args.index("--document-version") + 1] == "2"
            return {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "DocumentVersion": "2", "DocumentFormat": "JSON",
                "Status": "Active", "Content": json.dumps(expected),
            }

    attested = shadow_rollout.attest_rollout_document(ExactV2Aws(), expected)
    assert attested.version == "2"
    assert attested.canonical_sha256 == shadow_rollout.sha256(
        shadow_rollout._canonical_bytes(expected)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Name", "wrong"),
        ("DocumentVersion", "3"),
        ("DocumentFormat", "YAML"),
        ("Status", "Creating"),
    ],
)
def test_rollout_attestation_rejects_wrong_get_identity_before_host(field, value):
    expected = shadow_rollout.expected_rollout_document()

    class WrongGetAws:
        def call(self, args, write=False):
            if args[:2] == ["ssm", "describe-document"]:
                return {"Document": {
                    "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                    "Status": "Active", "DefaultVersion": "2", "LatestVersion": "2",
                }}
            response = {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "DocumentVersion": "2", "DocumentFormat": "JSON",
                "Status": "Active", "Content": json.dumps(expected),
            }
            response[field] = value
            return response

    with pytest.raises(shadow_rollout.RolloutError, match="document_readback_invalid"):
        shadow_rollout.attest_rollout_document(WrongGetAws(), expected)


def test_pre_send_rollout_version_drift_fails_before_host_command(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    calls = []

    def call(args, write=False):
        calls.append((args, write))
        return {"Document": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "Status": "Active", "DefaultVersion": "3", "LatestVersion": "3",
        }}

    monkeypatch.setattr(adapter, "call", call)
    with pytest.raises(
        shadow_rollout.RolloutError, match="rollout_document_pre_send_drift"
    ):
        adapter.send("install", rollout)
    assert calls == [([
        "ssm", "describe-document", "--name", shadow_rollout.ROLLOUT_DOCUMENT,
    ], False)]
    assert adapter.commands == []


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


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migrate_shadow_rollout_document_test",
        "deploy/migrate_shadow_rollout_document.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_migration_bootstrap_module():
    spec = importlib.util.spec_from_file_location(
        "bootstrap_shadow_rollout_migration_test",
        "deploy/bootstrap_shadow_rollout_migration.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ImmutableVersionStore(dict):
    def __setitem__(self, version, content):
        if version in self:
            raise AssertionError("SSM document versions are immutable")
        super().__setitem__(version, content)


class _PriorContentOverrides(dict):
    def __setitem__(self, version, content):
        if version != "1":
            raise AssertionError("candidate version content is immutable")
        super().__setitem__(version, content)


class _MigrationAws:
    def __init__(self, module):
        self.module = module
        self.expected = shadow_rollout.expected_rollout_document()
        self.legacy = {"schemaVersion": "2.2", "description": "legacy"}
        self.versions = _ImmutableVersionStore({"1": self.legacy})
        self.content_overrides = _PriorContentOverrides()
        self.names = {"1": None}
        self.default = "1"
        self.parameters = {}
        self.calls = []
        self.lose = set()
        self.clock = 0.0
        self.account = "123456789012"
        self.role_name = "kiwoom-stock-github-shadow-migration"
        self.session_name = "kiwoom-shadow-migration-77-1"
        self.authorized_candidate = None
        self.deadline = None
        self.latest_override = None
        self.document_status = "Active"
        self.version_status = {"1": "Active"}
        self.transient_once = set()
        self.update_response_override = None

    @property
    def latest(self):
        return self.latest_override or str(max(map(int, self.versions)))

    def remaining(self, operation="primary"):
        if self.deadline is not None:
            return self.deadline.remaining(operation)
        return 999.0

    def authorize_candidate(self, version):
        if self.authorized_candidate not in (None, version):
            raise self.module.MigrationError("candidate_authority_changed")
        self.authorized_candidate = version

    def call(self, args, operation="primary"):
        self.remaining(operation)
        self.calls.append((tuple(args), operation))
        command = tuple(args[:2])
        if command in self.transient_once:
            self.transient_once.remove(command)
            raise self.module.MigrationError("aws_read_failed")
        if command == ("sts", "get-caller-identity"):
            return {
                "Account": self.account,
                "Arn": (
                    f"arn:aws:sts::{self.account}:assumed-role/"
                    f"{self.role_name}/{self.session_name}"
                ),
                "UserId": f"AROATEST:{self.session_name}",
            }
        if command == ("ssm", "get-parameter"):
            name = args[args.index("--name") + 1]
            if name not in self.parameters:
                raise self.module.MigrationError("parameter_not_found")
            return {"Parameter": {"Value": self.parameters[name]}}
        if command == ("ssm", "put-parameter"):
            name = args[args.index("--name") + 1]
            value = args[args.index("--value") + 1]
            if "--no-overwrite" in args and name in self.parameters:
                raise self.module.MigrationError("parameter_exists")
            self.parameters[name] = value
            return {"Version": 1}
        if command == ("ssm", "delete-parameter"):
            self.parameters.pop(self.module.LOCK_PARAMETER, None)
            return {}
        if command == ("ssm", "describe-document"):
            return {"Document": {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "Status": self.document_status,
                "DefaultVersion": self.default, "LatestVersion": self.latest,
            }}
        if command == ("ssm", "get-document"):
            version = args[args.index("--document-version") + 1]
            return {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "DocumentVersion": version, "DocumentFormat": "JSON",
                "Status": self.version_status.get(version, "Active"),
                "Content": json.dumps(
                    self.content_overrides.get(version, self.versions[version])
                ),
            }
        if command == ("ssm", "list-document-versions"):
            return {"DocumentVersions": [
                {
                    "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                    "DocumentVersion": version, "VersionName": self.names[version],
                    "Status": self.version_status.get(version, "Active"),
                    "IsDefaultVersion": version == self.default,
                }
                for version in sorted(self.versions, key=int)
            ]}
        if command == ("ssm", "update-document"):
            version = str(int(self.latest) + 1)
            self.versions[version] = yaml.safe_load(args[args.index("--content") + 1])
            self.names[version] = args[args.index("--version-name") + 1]
            self.version_status[version] = "Active"
            if "update" in self.lose:
                raise self.module.MigrationError("aws_write_response_lost")
            if self.update_response_override is not None:
                return self.update_response_override
            return {"DocumentDescription": {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "VersionName": self.names[version],
                "DocumentVersion": version, "Status": "Active",
            }}
        if command == ("ssm", "update-document-default-version"):
            self.default = args[args.index("--document-version") + 1]
            if "default" in self.lose:
                raise self.module.MigrationError("aws_write_response_lost")
            return {"Description": {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "DefaultVersion": self.default,
            }}
        raise AssertionError(args)


def _migration_kwargs(
    module, aws, mode="apply", *, session_name="kiwoom-shadow-migration-77-1",
    account="123456789012", role_name="kiwoom-stock-github-shadow-migration",
    source_sha=None,
):
    aws.session_name = session_name
    aws.account = account
    aws.role_name = role_name
    source = Path(module.DOCUMENT_PATH).read_bytes()
    legacy_hash = shadow_rollout.sha256(
        shadow_rollout._canonical_bytes(aws.legacy)
    )
    return {
        "mode": mode,
        "account": account,
        "role_arn": (
            f"arn:aws:iam::{account}:role/{role_name}"
        ),
        "session_name": session_name,
        "source_sha": source_sha or "a" * 40,
        "source_blob": source,
        "provenance": {path: "b" * 64 for path in module.RELEVANT_PATHS},
        "attempt": "77",
        "prior_version": "1",
        "prior_hash": legacy_hash,
        "sleeper": lambda _: None,
    }


def test_migration_fake_models_document_version_content_as_immutable():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    with pytest.raises(AssertionError, match="document versions are immutable"):
        aws.versions["1"] = dict(aws.expected)
    with pytest.raises(AssertionError, match="candidate version content is immutable"):
        aws.content_overrides["2"] = dict(aws.expected)


def test_migration_apply_uses_direct_content_version_name_and_single_writes():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    result = module.execute(aws, **_migration_kwargs(module, aws))

    assert result["status"] == "PASS"
    writes = [call for call, _operation in aws.calls if call[0] == "ssm" and call[1] in {
        "put-parameter", "delete-parameter", "update-document",
        "update-document-default-version",
    }]
    update = [call for call in writes if call[:2] == ("ssm", "update-document")]
    defaults = [
        call for call in writes
        if call[:2] == ("ssm", "update-document-default-version")
    ]
    assert len(update) == len(defaults) == 1
    assert update[0][update[0].index("--version-name") + 1] == (
        "ksr-77-" + "a" * 12
    )
    content = update[0][update[0].index("--content") + 1]
    assert not content.startswith("file://")
    assert content == Path(module.DOCUMENT_PATH).read_text(encoding="utf-8")
    assert module.LOCK_PARAMETER not in aws.parameters


@pytest.mark.parametrize("lost", ["update", "default"])
def test_migration_response_loss_reconciles_without_write_retry(lost):
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.lose.add(lost)

    if lost == "default":
        result = module.execute(aws, **_migration_kwargs(module, aws))
        assert result["status"] == "PASS"
    else:
        result = module.execute(aws, **_migration_kwargs(module, aws))
        assert result["status"] == "PASS"
    operations = [call[:2] for call, _operation in aws.calls]
    assert operations.count(("ssm", "update-document")) == 1
    assert operations.count(("ssm", "update-document-default-version")) == 1


def test_migration_invalid_update_response_reconciles_without_write_retry():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.update_response_override = {
        "DocumentDescription": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "VersionName": "wrong",
            "DocumentVersion": "2",
            "Status": "Active",
        }
    }
    result = module.execute(aws, **_migration_kwargs(module, aws))
    assert result["status"] == "PASS"
    assert sum(
        call[:2] == ("ssm", "update-document") for call, _operation in aws.calls
    ) == 1


@pytest.mark.parametrize(
    ("crash_phase", "expected_phase"),
    [
        ("update_submitting", "update_submitting"),
        ("candidate_verified", "candidate_verified"),
        ("cutover_submitting", "cutover_submitting"),
    ],
)
def test_migration_same_attempt_crash_resume_never_repeats_submit(
    crash_phase, expected_phase,
):
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == crash_phase:
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError, match="process loss"):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    journal = json.loads(aws.parameters[journal_name])
    assert journal["phase"] == expected_phase
    before = [call[:2] for call, _operation in aws.calls]
    try:
        result = module.execute(
            aws, **_migration_kwargs(module, aws, mode="reconcile")
        )
    except module.MigrationError as error:
        assert error.category in {"update_uncertain", "cutover_uncertain_no_cas"}
    else:
        assert result["status"] == "PASS"
    after = [call[:2] for call, _operation in aws.calls]
    assert after.count(("ssm", "update-document")) <= 1
    assert after.count(("ssm", "update-document-default-version")) <= 1
    if journal["phase"].endswith("submitting") and (
        crash_phase in {"update_submitting", "cutover_submitting"}
    ):
        assert before.count(("ssm", "update-document")) <= 1


def test_migration_journal_created_before_lock_resumes_same_attempt():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "attempt_created":
            raise RuntimeError("process loss before lock")

    with pytest.raises(RuntimeError, match="before lock"):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    assert json.loads(aws.parameters[journal_name])["phase"] == "attempt_created"
    assert module.LOCK_PARAMETER not in aws.parameters

    result = module.execute(
        aws,
        **_migration_kwargs(
            module, aws, mode="reconcile",
            session_name="kiwoom-shadow-migration-77-2",
        ),
    )
    assert result["status"] == "PASS"
    assert module.LOCK_PARAMETER not in aws.parameters


def test_migration_post_lock_journal_race_closes_in_manual_hold(monkeypatch):
    module = _load_migration_module()
    aws = _MigrationAws(module)
    original = module._put_lock

    def racing_put_lock(adapter, encoded):
        original(adapter, encoded)
        name = module.JOURNAL_PREFIX + "77"
        raced = json.loads(adapter.parameters[name])
        raced["actor_last"] = "d" * 64
        adapter.parameters[name] = module._canonical_json(raced)

    monkeypatch.setattr(module, "_put_lock", racing_put_lock)
    with pytest.raises(module.MigrationError, match="journal_changed_during_lock"):
        module.execute(aws, **_migration_kwargs(module, aws))
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert module._validate_journal(journal) is None
    assert module.LOCK_PARAMETER in aws.parameters


@pytest.mark.parametrize("drift", ["default", "latest", "status", "hash"])
def test_migration_prestate_resume_drift_holds_before_document_write(drift):
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "prestate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    if drift in {"default", "latest"}:
        aws.versions["2"] = dict(aws.legacy)
        aws.names["2"] = None
        aws.version_status["2"] = "Active"
    if drift == "default":
        aws.default = "2"
        aws.latest_override = "1"
    elif drift == "latest":
        aws.latest_override = "2"
    elif drift == "status":
        aws.document_status = "Failed"
    else:
        aws.content_overrides["1"] = {
            "schemaVersion": "2.2", "description": "drift"
        }
    marker = len(aws.calls)
    with pytest.raises(module.MigrationError, match="unknown_prestate"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    after = aws.calls[marker:]
    assert not any(
        call[:2] in {
            ("ssm", "update-document"),
            ("ssm", "update-document-default-version"),
        }
        for call, _operation in after
    )
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_stable_attempt_allows_cross_run_sessions():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "candidate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError, match="process loss"):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    original = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    result = module.execute(
        aws,
        **_migration_kwargs(
            module, aws, mode="reconcile",
            session_name="kiwoom-shadow-migration-77-2",
        ),
    )
    assert result["status"] == "PASS"
    assert result["contract"] == original["contract"]
    second_actor = result["actor_last"]

    result = module.execute(
        aws,
        **_migration_kwargs(
            module, aws, mode="reconcile",
            session_name="kiwoom-shadow-migration-88-1",
        ),
    )
    assert result["status"] == "PASS"
    assert result["contract"] == original["contract"]
    assert result["actor_last"] != second_actor
    assert "session" not in result["contract"]
    assert set(json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])[
        "contract"
    ]) == {
        "schema", "account", "role_arn_sha256", "source_sha", "attempt",
        "prior_version", "prior_sha256", "target_sha256", "version_name",
        "provenance",
    }


@pytest.mark.parametrize("changed", ["role", "account", "source"])
def test_migration_stable_attempt_rejects_changed_authority_or_source(changed):
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "prestate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    journal_before = aws.parameters[journal_name]
    options = {
        "session_name": "kiwoom-shadow-migration-77-2",
        "role_name": (
            "different-shadow-migration" if changed == "role"
            else "kiwoom-stock-github-shadow-migration"
        ),
        "account": "210987654321" if changed == "account" else "123456789012",
        "source_sha": "b" * 40 if changed == "source" else "a" * 40,
    }
    lock_before = aws.parameters[module.LOCK_PARAMETER]
    with pytest.raises(module.MigrationError, match="journal_contract_mismatch"):
        module.execute(
            aws, **_migration_kwargs(module, aws, mode="reconcile", **options)
        )
    assert aws.parameters[journal_name] == journal_before
    assert aws.parameters[module.LOCK_PARAMETER] == lock_before


def test_migration_contract_equality_has_no_session_fingerprint():
    module = _load_migration_module()
    values = (
        "123456789012",
        "arn:aws:iam::123456789012:role/kiwoom-stock-github-shadow-migration",
        "a" * 40, "77", "1", "b" * 64, "c" * 64,
        {path: "d" * 64 for path in module.RELEVANT_PATHS},
    )
    c1 = module._contract(*values)
    c2 = module._contract(*values)
    assert c1 == c2
    assert "session" not in c1


def test_migration_apply_existing_journal_requires_reconcile_and_keeps_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    contract = module._contract(
        kwargs["account"], kwargs["role_arn"], kwargs["source_sha"],
        kwargs["attempt"], kwargs["prior_version"],
        kwargs["prior_hash"],
        shadow_rollout.sha256(shadow_rollout._canonical_bytes(aws.expected)),
        kwargs["provenance"],
    )
    aws.parameters[module.JOURNAL_PREFIX + "77"] = module._canonical_json({
        "schema": 2, "status": "IN_PROGRESS", "phase": "lease_acquired",
        "contract": contract, "candidate": None,
        "submits": {"update": 0, "cutover": 0},
    })
    with pytest.raises(module.MigrationError, match="apply_requires_reconcile"):
        module.execute(aws, **kwargs)
    assert module.LOCK_PARAMETER not in aws.parameters


def test_migration_reconcile_contract_mismatch_keeps_lock_and_writes_no_document():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.parameters[module.JOURNAL_PREFIX + "77"] = module._canonical_json({
        "schema": 2, "status": "IN_PROGRESS", "phase": "lease_acquired",
        "contract": {"source_sha": "wrong"}, "candidate": None,
        "submits": {"update": 0, "cutover": 0},
    })
    with pytest.raises(module.MigrationError, match="journal_contract_mismatch"):
        module.execute(
            aws, **_migration_kwargs(module, aws, mode="reconcile")
        )
    assert module.LOCK_PARAMETER not in aws.parameters
    assert not any(
        call[:2] == ("ssm", "update-document") for call, _ in aws.calls
    )


@pytest.mark.parametrize("mode", ["apply", "reconcile"])
def test_migration_incompatible_completed_journal_never_creates_lock(mode):
    module = _load_migration_module()
    aws = _MigrationAws(module)
    result = module.execute(aws, **_migration_kwargs(module, aws))
    assert result["phase"] == "complete"
    journal_name = module.JOURNAL_PREFIX + "77"
    journal_before = aws.parameters[journal_name]
    assert module.LOCK_PARAMETER not in aws.parameters

    kwargs = _migration_kwargs(
        module, aws, mode=mode, source_sha="b" * 40,
        session_name="kiwoom-shadow-migration-77-2",
    )
    category = (
        "apply_requires_reconcile" if mode == "apply"
        else "journal_contract_mismatch"
    )
    with pytest.raises(module.MigrationError, match=category):
        module.execute(aws, **kwargs)
    assert aws.parameters[journal_name] == journal_before
    assert module.LOCK_PARAMETER not in aws.parameters


def test_migration_contract_mismatch_does_not_change_unrelated_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    contract = module._contract(
        kwargs["account"], kwargs["role_arn"], "b" * 40,
        kwargs["attempt"], kwargs["prior_version"], kwargs["prior_hash"],
        shadow_rollout.sha256(shadow_rollout._canonical_bytes(aws.expected)),
        kwargs["provenance"],
    )
    journal = module._new_journal(contract, "c" * 64)
    aws.parameters[module.JOURNAL_PREFIX + "77"] = module._canonical_json(journal)
    aws.parameters[module.LOCK_PARAMETER] = "unrelated-owner"
    with pytest.raises(module.MigrationError, match="journal_contract_mismatch"):
        module.execute(
            aws, **_migration_kwargs(module, aws, mode="reconcile")
        )
    assert aws.parameters[module.LOCK_PARAMETER] == "unrelated-owner"


def test_migration_conflicting_or_malformed_lock_never_writes_document():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.parameters[module.LOCK_PARAMETER] = "not-this-owner"
    with pytest.raises(module.MigrationError, match="lease_conflict"):
        module.execute(aws, **_migration_kwargs(module, aws))
    assert not any(
        call[:2] in {
            ("ssm", "update-document"),
            ("ssm", "update-document-default-version"),
        }
        for call, _ in aws.calls
    )


def test_migration_release_requires_exact_owner():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.parameters[module.LOCK_PARAMETER] = "different-owner"
    with pytest.raises(module.MigrationError, match="lease_owner_changed"):
        module._release_lock(aws, "expected-owner")
    assert aws.parameters[module.LOCK_PARAMETER] == "different-owner"


def test_migration_journal_enforces_standard_parameter_limit():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    with pytest.raises(module.MigrationError, match="journal_oversize"):
        module._put_parameter(
            aws, module.JOURNAL_PREFIX + "77",
            "x" * (module.JOURNAL_LIMIT + 1), overwrite=False,
            operation="primary",
        )


def test_migration_deadline_blocks_write_before_subprocess(monkeypatch):
    module = _load_migration_module()
    adapter = module.AdminAwsCli(
        module.Deadline(50.0, 100.0, clock=lambda: 100.0),
        "approved", "ksr-77-" + "a" * 12, "1",
    )
    invoked = False

    def forbidden(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(module.MigrationError, match="execution_deadline_exhausted"):
        adapter.call(
            ["ssm", "delete-parameter", "--name", module.LOCK_PARAMETER],
            operation="terminal",
        )
    assert invoked is False


def test_migration_adapter_rejects_arbitrary_direct_content_before_subprocess(
    monkeypatch,
):
    module = _load_migration_module()
    approved = "approved immutable yaml"
    version_name = "ksr-77-" + "a" * 12
    adapter = module.AdminAwsCli(
        module.Deadline(100.0, 200.0, clock=lambda: 0.0),
        approved, version_name, "1",
    )
    invoked = False

    def forbidden(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("subprocess must not run")

    monkeypatch.setattr(module.subprocess, "run", forbidden)
    with pytest.raises(module.MigrationError, match="admin_command_not_allowed"):
        adapter.call([
            "ssm", "update-document", "--name",
            shadow_rollout.ROLLOUT_DOCUMENT,
            "--document-version", "$LATEST", "--document-format", "YAML",
            "--version-name", version_name, "--content", "arbitrary yaml",
        ], operation="primary")
    assert invoked is False


@pytest.mark.parametrize(
    ("args_factory", "operation"),
    [
        (
            lambda module, content, name: [
                "ssm", "update-document", "--name",
                shadow_rollout.ROLLOUT_DOCUMENT,
                "--document-version", "$LATEST", "--document-format", "YAML",
                "--version-name", name, "--content", content,
            ],
            "terminal",
        ),
        (
            lambda module, content, name: [
                "ssm", "delete-parameter", "--name", module.LOCK_PARAMETER,
            ],
            "primary",
        ),
        (
            lambda module, content, name: [
                "ssm", "put-parameter", "--name", module.JOURNAL_PREFIX + "77",
                "--type", "String", "--value",
                json.dumps({"phase": "manual_hold"}), "--overwrite",
            ],
            "primary",
        ),
    ],
)
def test_migration_adapter_rejects_operation_class_mismatch(
    monkeypatch, args_factory, operation,
):
    module = _load_migration_module()
    approved = "approved immutable yaml"
    version_name = "ksr-77-" + "a" * 12
    adapter = module.AdminAwsCli(
        module.Deadline(100.0, 200.0, clock=lambda: 0.0),
        approved, version_name, "1",
    )
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("subprocess must not run"),
    )
    with pytest.raises(
        module.MigrationError, match="admin_command_operation_mismatch"
    ):
        adapter.call(
            args_factory(module, approved, version_name), operation=operation
        )


def test_migration_provenance_uses_process_deadline_before_subprocess(monkeypatch):
    module = _load_migration_module()
    monkeypatch.setattr(
        module.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("subprocess must not run"),
    )
    with pytest.raises(module.MigrationError, match="execution_deadline_exhausted"):
        module._git(
            ["rev-parse", "HEAD"],
            module.Deadline(10.0, 20.0, clock=lambda: 10.0),
            text=True,
        )


@pytest.mark.parametrize(
    "args",
    [
        ["ssm", "create-document"],
        ["ssm", "delete-document", "--name", shadow_rollout.ROLLOUT_DOCUMENT],
        ["ssm", "send-command"],
        ["ec2", "describe-instances"],
        [
            "ssm", "update-document", "--name", shadow_rollout.ROLLOUT_DOCUMENT,
            "--document-version", "$LATEST", "--document-format", "YAML",
            "--version-name", "ksr-77-" + "a" * 12,
            "--content", "file:///tmp/mutable",
        ],
        [
            "ssm", "update-document-default-version", "--name",
            shadow_rollout.ROLLOUT_DOCUMENT, "--document-version", "1",
        ],
    ],
)
def test_migration_runtime_classifier_rejects_forbidden_authority(args):
    module = _load_migration_module()
    with pytest.raises(module.MigrationError, match="admin_command_not_allowed"):
        module._classify_admin_command(
            args,
            approved_content="approved",
            approved_version_name="ksr-77-" + "a" * 12,
            candidate_version="2",
        )


def test_migration_approved_sources_binds_head_clean_and_exact_blobs(monkeypatch):
    module = _load_migration_module()
    calls = []

    def fake_git(args, deadline, text=False):
        calls.append((tuple(args), text))
        if args == ["rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        if args[0] == "status":
            return ""
        return ("blob:" + args[1].split(":", 1)[1]).encode()

    monkeypatch.setattr(module, "_git", fake_git)
    source, provenance = module.approved_sources(
        "a" * 40, module.Deadline(100.0, 200.0, clock=lambda: 0.0)
    )
    assert source == ("blob:" + module.DOCUMENT_PATH).encode()
    assert set(provenance) == set(module.RELEVANT_PATHS)
    assert calls[0][0] == ("rev-parse", "HEAD")
    assert calls[1][0] == (
        "status", "--porcelain", "--untracked-files=all",
    )
    assert all(call[0][0] == "show" for call in calls[2:])


def test_migration_versions_rejects_duplicate_and_bad_pagination():
    module = _load_migration_module()

    class Pages:
        def __init__(self):
            self.count = 0

        def call(self, args, operation="primary"):
            self.count += 1
            item = {
                "Name": shadow_rollout.ROLLOUT_DOCUMENT,
                "DocumentVersion": "1", "VersionName": None,
                "Status": "Active", "IsDefaultVersion": True,
            }
            if self.count == 1:
                return {"DocumentVersions": [item], "NextToken": "again"}
            return {"DocumentVersions": [item]}

    with pytest.raises(module.MigrationError, match="document_versions_duplicate"):
        module._versions(Pages(), operation="primary")


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"DocumentDescription": {}, "unexpected": {}},
        {"DocumentDescription": {
            "Name": "wrong", "VersionName": "ksr-77-" + "a" * 12,
            "DocumentVersion": "2", "Status": "Active",
        }},
        {"DocumentDescription": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT, "VersionName": "wrong",
            "DocumentVersion": "2", "Status": "Active",
        }},
        {"DocumentDescription": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "VersionName": "ksr-77-" + "a" * 12,
            "DocumentVersion": "not-numeric", "Status": "Active",
        }},
        {"DocumentDescription": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "VersionName": "ksr-77-" + "a" * 12,
            "DocumentVersion": "2", "Status": "Failed",
        }},
        {"DocumentDescription": {
            "Name": shadow_rollout.ROLLOUT_DOCUMENT,
            "VersionName": "ksr-77-" + "a" * 12,
            "DocumentVersion": "2", "Status": "Active", "Unknown": "x",
        }},
    ],
)
def test_migration_update_response_contract_rejects_invalid_shape(response):
    module = _load_migration_module()
    with pytest.raises(module.MigrationError, match="update_response_invalid"):
        module._update_response_version(response, "ksr-77-" + "a" * 12)


def test_migration_update_response_contract_accepts_exact_bound_description():
    module = _load_migration_module()
    response = {"DocumentDescription": {
        "Name": shadow_rollout.ROLLOUT_DOCUMENT,
        "VersionName": "ksr-77-" + "a" * 12,
        "DocumentVersion": "2", "Status": "Active",
    }}
    assert module._update_response_version(
        response, "ksr-77-" + "a" * 12
    ) == "2"


def test_migration_malformed_matching_journal_closes_in_manual_hold():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "prestate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    malformed = json.loads(aws.parameters[journal_name])
    malformed["submits"] = {"update": 0}
    aws.parameters[journal_name] = module._canonical_json(malformed)
    with pytest.raises(module.MigrationError, match="journal_schema_invalid"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    closed = json.loads(aws.parameters[journal_name])
    assert closed["phase"] == "manual_hold"
    assert closed["status"] == "MANUAL_HOLD"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_invalid_submit_order_normalizes_to_stable_manual_hold():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "prestate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    malformed = json.loads(aws.parameters[journal_name])
    malformed["submits"] = {"update": 0, "cutover": 1}
    aws.parameters[journal_name] = module._canonical_json(malformed)
    with pytest.raises(
        module.MigrationError, match="journal_submit_invariant_invalid"
    ):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    closed = json.loads(aws.parameters[journal_name])
    assert closed["submits"] == {"update": 1, "cutover": 1}
    assert module._validate_journal(closed) is None
    with pytest.raises(module.MigrationError, match="manual_hold"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-88-1",
            ),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate", None), ("final", None), ("prestate", None),
        ("response_version", "3"),
    ],
)
def test_migration_incomplete_complete_journal_never_passes_or_releases(
    field, value,
):
    module = _load_migration_module()
    aws = _MigrationAws(module)
    result = module.execute(aws, **_migration_kwargs(module, aws))
    assert result["phase"] == "complete"
    journal_name = module.JOURNAL_PREFIX + "77"
    forged = json.loads(aws.parameters[journal_name])
    forged[field] = value
    aws.parameters[journal_name] = module._canonical_json(forged)
    with pytest.raises(module.MigrationError, match="journal_phase_evidence_invalid"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    closed = json.loads(aws.parameters[journal_name])
    assert closed["phase"] == "manual_hold"
    assert closed["status"] == "MANUAL_HOLD"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_valid_complete_reconcile_authoritatively_reads_before_pass():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    assert module.execute(
        aws, **_migration_kwargs(module, aws)
    )["phase"] == "complete"
    marker = len(aws.calls)
    result = module.execute(
        aws,
        **_migration_kwargs(
            module, aws, mode="reconcile",
            session_name="kiwoom-shadow-migration-77-2",
        ),
    )
    assert result["status"] == "PASS"
    commands = [call[:2] for call, _operation in aws.calls[marker:]]
    assert ("ssm", "describe-document") in commands
    assert ("ssm", "get-document") in commands
    assert ("ssm", "list-document-versions") in commands


def test_migration_terminal_complete_transient_read_retains_phase_and_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    assert module.execute(
        aws, **_migration_kwargs(module, aws)
    )["phase"] == "complete"
    aws.transient_once.add(("ssm", "describe-document"))
    with pytest.raises(module.MigrationError, match="aws_read_failed"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "complete"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_transient_authoritative_read_keeps_phase():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "candidate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    journal_name = module.JOURNAL_PREFIX + "77"
    aws.transient_once.add(("ssm", "list-document-versions"))
    with pytest.raises(module.MigrationError, match="aws_read_failed"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    assert json.loads(aws.parameters[journal_name])["phase"] == (
        "candidate_verified"
    )


@pytest.mark.parametrize("drift", ["version_name", "status", "default", "latest"])
def test_migration_authoritative_candidate_drift_is_durable_manual_hold(drift):
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "candidate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    if drift == "version_name":
        aws.names["2"] = "wrong"
    elif drift == "status":
        aws.version_status["2"] = "Failed"
    elif drift == "default":
        aws.default = "2"
    else:
        aws.latest_override = "1"
    journal_name = module.JOURNAL_PREFIX + "77"
    with pytest.raises(module.MigrationError):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    closed = json.loads(aws.parameters[journal_name])
    assert closed["phase"] == "manual_hold"
    assert closed["status"] == "MANUAL_HOLD"


def test_migration_primary_cutoff_allows_terminal_manual_hold_without_write():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "candidate_verified":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    marker = len(aws.calls)
    aws.deadline = module.Deadline(10.0, 100.0, clock=lambda: 20.0)
    with pytest.raises(module.MigrationError, match="primary_deadline_exhausted"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    after = aws.calls[marker:]
    assert not any(
        operation == "primary" and call[:2] in {
            ("ssm", "update-document"),
            ("ssm", "update-document-default-version"),
        }
        for call, operation in after
    )
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert any(
        operation == "terminal" and call[:2] == ("ssm", "put-parameter")
        for call, operation in after
    )


def test_migration_terminal_reserve_post_cutover_drift_holds_without_write():
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "cutover_reconciled":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    aws.versions["3"] = dict(aws.expected)
    aws.names["3"] = None
    aws.version_status["3"] = "Active"
    marker = len(aws.calls)
    aws.deadline = module.Deadline(10.0, 100.0, clock=lambda: 20.0)
    with pytest.raises(module.MigrationError, match="cutover_state_drift"):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    assert module.LOCK_PARAMETER in aws.parameters
    after = aws.calls[marker:]
    assert all(operation == "terminal" for _call, operation in after)
    assert not any(
        call[:2] == ("ssm", "update-document-default-version")
        for call, _operation in after
    )
    assert not any(
        call[:2] == ("ssm", "delete-parameter")
        for call, _operation in after
    )
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert journal["status"] == "MANUAL_HOLD"


@pytest.mark.parametrize("drift", ["latest", "default", "name", "status"])
def test_migration_post_cutover_drift_never_submits_another_default_write(drift):
    module = _load_migration_module()
    aws = _MigrationAws(module)

    def crash(phase):
        if phase == "cutover_reconciled":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(
            aws, **_migration_kwargs(module, aws), phase_hook=crash
        )
    if drift == "latest":
        aws.versions["3"] = dict(aws.expected)
        aws.names["3"] = None
        aws.version_status["3"] = "Active"
    elif drift == "default":
        aws.default = "1"
    elif drift == "name":
        aws.names["2"] = "wrong"
    elif drift == "status":
        aws.document_status = "Failed"
    marker = len(aws.calls)
    with pytest.raises(module.MigrationError):
        module.execute(
            aws,
            **_migration_kwargs(
                module, aws, mode="reconcile",
                session_name="kiwoom-shadow-migration-77-2",
            ),
        )
    after = aws.calls[marker:]
    assert not any(
        call[:2] == ("ssm", "update-document-default-version")
        for call, _operation in after
    )
    assert json.loads(
        aws.parameters[module.JOURNAL_PREFIX + "77"]
    )["phase"] == "manual_hold"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_failed_safe_crash_reconcile_is_release_only_fail():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    kwargs["prior_hash"] = "f" * 64

    def crash(phase):
        if phase == "failed_safe":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(aws, **kwargs, phase_hook=crash)
    journal_name = module.JOURNAL_PREFIX + "77"
    first_actor = json.loads(aws.parameters[journal_name])["actor_last"]
    marker = len(aws.calls)
    kwargs = _migration_kwargs(
        module, aws, mode="reconcile",
        session_name="kiwoom-shadow-migration-77-2",
    )
    kwargs["prior_hash"] = "f" * 64
    result = module.execute(aws, **kwargs)
    current_actor = module.sha256((
        "arn:aws:sts::123456789012:assumed-role/"
        "kiwoom-stock-github-shadow-migration/"
        "kiwoom-shadow-migration-77-2"
    ).encode())
    remote = json.loads(aws.parameters[journal_name])
    assert result["phase"] == "failed_safe"
    assert result["status"] == "FAIL"
    assert first_actor != current_actor
    assert remote["actor_last"] == current_actor
    assert result["actor_last"] == current_actor
    assert module.LOCK_PARAMETER not in aws.parameters
    release_calls = aws.calls[marker:]
    journal_update = next(
        index for index, (call, _operation) in enumerate(release_calls)
        if call[:2] == ("ssm", "put-parameter")
        and call[call.index("--name") + 1] == journal_name
    )
    lock_delete = next(
        index for index, (call, _operation) in enumerate(release_calls)
        if call[:2] == ("ssm", "delete-parameter")
    )
    assert journal_update < lock_delete
    assert not any(
        call[:2] in {
            ("ssm", "update-document"),
            ("ssm", "update-document-default-version"),
        }
        for call, _operation in aws.calls[marker:]
    )


@pytest.mark.parametrize("failure_point", ["update", "readback"])
def test_migration_failed_safe_actor_audit_failure_retains_lock(failure_point):
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    kwargs["prior_hash"] = "f" * 64

    def crash(phase):
        if phase == "failed_safe":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(aws, **kwargs, phase_hook=crash)
    journal_name = module.JOURNAL_PREFIX + "77"
    original_call = aws.call
    release_update_written = False

    def fail_actor_audit(args, operation="primary"):
        nonlocal release_update_written
        command = tuple(args[:2])
        name = args[args.index("--name") + 1] if "--name" in args else None
        is_journal_update = (
            command == ("ssm", "put-parameter")
            and name == journal_name
            and "--overwrite" in args
        )
        if failure_point == "update" and is_journal_update:
            raise module.MigrationError("aws_read_failed")
        if (
            failure_point == "readback"
            and release_update_written
            and command == ("ssm", "get-parameter")
            and name == journal_name
        ):
            raise module.MigrationError("aws_read_failed")
        result = original_call(args, operation=operation)
        if is_journal_update:
            release_update_written = True
        return result

    aws.call = fail_actor_audit
    kwargs = _migration_kwargs(
        module, aws, mode="reconcile",
        session_name="kiwoom-shadow-migration-77-2",
    )
    kwargs["prior_hash"] = "f" * 64
    with pytest.raises(module.MigrationError, match="aws_read_failed"):
        module.execute(aws, **kwargs)
    assert module.LOCK_PARAMETER in aws.parameters
    assert json.loads(aws.parameters[journal_name])["phase"] == "failed_safe"
    assert not any(
        call[:2] == ("ssm", "delete-parameter")
        for call, _operation in aws.calls
    )


def test_migration_failed_safe_terminal_candidate_presence_holds_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    kwargs["prior_hash"] = "f" * 64

    def crash(phase):
        if phase == "failed_safe":
            raise RuntimeError("process loss")

    with pytest.raises(RuntimeError):
        module.execute(aws, **kwargs, phase_hook=crash)
    aws.versions["2"] = dict(aws.expected)
    aws.names["2"] = "ksr-77-" + "a" * 12
    aws.version_status["2"] = "Active"
    kwargs = _migration_kwargs(
        module, aws, mode="reconcile",
        session_name="kiwoom-shadow-migration-77-2",
    )
    kwargs["prior_hash"] = "f" * 64
    with pytest.raises(
        module.MigrationError, match="failed_safe_candidate_present"
    ):
        module.execute(aws, **kwargs)
    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert module.LOCK_PARAMETER in aws.parameters


def test_migration_immediate_failed_safe_proves_absence_before_release():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    kwargs = _migration_kwargs(module, aws)
    kwargs["prior_hash"] = "f" * 64

    result = module.execute(aws, **kwargs)

    assert result["phase"] == "failed_safe"
    assert result["status"] == "FAIL"
    assert module.LOCK_PARAMETER not in aws.parameters
    calls = [call[:2] for call, _operation in aws.calls]
    assert ("ssm", "list-document-versions") in calls
    assert calls.index(("ssm", "list-document-versions")) < calls.index(
        ("ssm", "delete-parameter")
    )


def test_migration_immediate_failed_safe_candidate_presence_holds_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.versions["2"] = dict(aws.expected)
    aws.names["2"] = "ksr-77-" + "a" * 12
    aws.version_status["2"] = "Active"

    with pytest.raises(
        module.MigrationError, match="failed_safe_candidate_present"
    ):
        module.execute(aws, **_migration_kwargs(module, aws))

    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "manual_hold"
    assert module.LOCK_PARAMETER in aws.parameters
    assert not any(
        call[:2] == ("ssm", "delete-parameter")
        for call, _operation in aws.calls
    )


def test_migration_immediate_failed_safe_transient_read_retains_lock():
    module = _load_migration_module()
    aws = _MigrationAws(module)
    aws.transient_once.add(("ssm", "list-document-versions"))
    kwargs = _migration_kwargs(module, aws)
    kwargs["prior_hash"] = "f" * 64

    with pytest.raises(module.MigrationError, match="aws_read_failed"):
        module.execute(aws, **kwargs)

    journal = json.loads(aws.parameters[module.JOURNAL_PREFIX + "77"])
    assert journal["phase"] == "failed_safe"
    assert journal["status"] == "FAIL"
    assert module.LOCK_PARAMETER in aws.parameters
    assert not any(
        call[:2] == ("ssm", "delete-parameter")
        for call, _operation in aws.calls
    )


@pytest.mark.parametrize(
    ("status", "phase", "expected"),
    [("PASS", "complete", 0), ("FAIL", "failed_safe", 1),
     ("MANUAL_HOLD", "manual_hold", 1)],
)
def test_migration_main_only_complete_exits_zero(
    monkeypatch, tmp_path, status, phase, expected,
):
    module = _load_migration_module()
    source = Path(module.DOCUMENT_PATH).read_bytes()
    provenance = {path: "b" * 64 for path in module.RELEVANT_PATHS}
    monkeypatch.setattr(
        module, "approved_sources", lambda source_sha, deadline: (source, provenance)
    )
    monkeypatch.setattr(
        module, "execute", lambda aws, **kwargs: {
            "status": status, "phase": phase, "actor_last": "c" * 64,
        },
    )
    result = module.main([
        "--mode", "reconcile",
        "--account-id", "123456789012",
        "--expected-role-arn",
        "arn:aws:iam::123456789012:role/kiwoom-stock-github-shadow-migration",
        "--expected-session-name", "kiwoom-shadow-migration-77-1",
        "--source-sha", "a" * 40,
        "--migration-attempt-id", "77",
        "--expected-current-version", "1",
        "--expected-current-canonical-sha256", "d" * 64,
        "--audit-path", str(tmp_path / "audit.json"),
    ])
    assert result == expected
    audit = json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))
    assert audit["actor_session_sha256"] == "c" * 64


def test_migration_bootstrap_reuses_exact_and_refuses_trust_drift(monkeypatch):
    module = _load_migration_bootstrap_module()
    trust, _ = module._render(module.TRUST, "123456789012")
    policy, _ = module._render(module.POLICY, "123456789012")
    role = {
        "Arn": "arn:aws:iam::123456789012:role/" + module.ROLE_NAME,
        "AssumeRolePolicyDocument": trust,
    }

    def exact_run(args, missing=None):
        if args[:2] == ["accessanalyzer", "validate-policy"]:
            return {"findings": []}
        if args[:2] == ["iam", "get-role"]:
            return {"Role": role}
        if args[:2] == ["iam", "get-role-policy"]:
            return {"PolicyDocument": policy}
        raise AssertionError(args)

    monkeypatch.setattr(module, "_run", exact_run)
    assert module.bootstrap("123456789012")["role_created"] is False
    role["AssumeRolePolicyDocument"] = {"Version": "drift"}
    with pytest.raises(module.BootstrapError, match="trust drift"):
        module.bootstrap("123456789012")


def test_routine_rejects_unattested_tuple_before_aws(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
    )
    invoked = False

    def forbidden(*args, **kwargs):
        nonlocal invoked
        invoked = True
        raise AssertionError("AWS must not run")

    monkeypatch.setattr(shadow_rollout.subprocess, "run", forbidden)
    with pytest.raises(
        shadow_rollout.RolloutError, match="rollout_document_version_invalid"
    ):
        shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10).send(
            "install", rollout
        )
    assert invoked is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("DocumentName", "wrong"),
        ("DocumentVersion", "3"),
        ("InstanceIds", ["i-wrong"]),
        ("Parameters", {}),
        ("CommandId", "bad"),
    ],
)
def test_routine_send_response_binds_exact_identity(field, value):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    response = _send_response(rollout, command_id)
    response["Command"][field] = value
    with pytest.raises(
        shadow_rollout.RolloutError, match="send_response_identity_mismatch"
    ):
        shadow_rollout.AwsCli._send_response_command_id(
            response, shadow_rollout._rollout_comment("install", rollout),
            shadow_rollout._rollout_parameters("install", rollout), "2",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CommandId", "00000000-0000-0000-0000-000000000099"),
        ("DocumentName", "wrong"),
        ("DocumentVersion", "3"),
        ("InstanceId", "i-wrong"),
    ],
)
def test_routine_poll_binds_exact_invocation_identity(monkeypatch, field, value):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
        validator_sha256="f" * 64, shadow_document_sha256="c" * 64,
        shadow_document_raw_sha256="d" * 64,
        rollout_document_sha256="e" * 64, rollout_attempt_id="123",
        rollout_document_version="2",
        rollout_document_canonical_sha256="9" * 64,
    )
    command_id = "00000000-0000-0000-0000-000000000001"
    response = _poll_response(rollout, command_id, Status="Success")
    response[field] = value
    adapter = shadow_rollout.AwsCli(shadow_rollout.time.monotonic() + 10)
    monkeypatch.setattr(adapter, "call", lambda args, write=False: response)
    with pytest.raises(shadow_rollout.RolloutError, match="invocation_invalid"):
        adapter.poll(command_id, rollout)


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
