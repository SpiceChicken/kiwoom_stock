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


def _host_evidence(action, rollout, installed):
    present = bool(installed)
    return {
        "action": action,
        "source_sha": rollout.source_sha if present else "",
        "worker_sha256": rollout.worker_sha256 if present else "",
        "shadow_document_sha256": rollout.shadow_document_sha256 if present else "",
        "rollout_attempt_id": rollout.rollout_attempt_id if present else "",
        "observed_worker_sha256": rollout.worker_sha256 if present else "",
        "worker_present": present, "worker_owner": "0:0" if present else "",
        "worker_mode": "750" if present else "", "worker_links": 1 if present else 0,
        "worker_regular": present, "worker_metadata_valid": True,
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
        "Action", "SourceSha", "WorkerSha256", "ShadowDocumentSha256",
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
    assert "rollback_pair >/dev/null 2>&1" in text
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


def test_atomic_binding_publish_failure_restores_exact_prior_pair(tmp_path):
    target = tmp_path / "bin" / "kiwoom-shadow-worker"
    state = tmp_path / "state"
    binding = tmp_path / "current.json"
    lock = tmp_path / "rollout.lock"
    target.parent.mkdir()
    old_worker = b"#!/usr/bin/env bash\necho old\n"
    new_worker = b"#!/usr/bin/env bash\necho new\n"
    target.write_bytes(old_worker)
    target.chmod(0o750)
    old_binding = b'{"prior":"binding"}\n'
    binding.write_bytes(old_binding)
    binding.chmod(0o600)
    source = tmp_path / "new-worker"
    source.write_bytes(new_worker)
    source.chmod(0o700)
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_curl = tools / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env python3\n"
        "import os, shutil, sys\n"
        "destination=sys.argv[sys.argv.index('-o')+1]\n"
        "shutil.copyfile(os.environ['FAKE_WORKER'],destination)\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    uid, gid = os.getuid(), os.getgid()
    base_command = _rendered_rollout_command()
    base_command = base_command.replace(
        "target=/usr/local/sbin/kiwoom-shadow-worker", f"target={target}"
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
        "SSM_Action": "install", "SSM_SourceSha": "a" * 40,
        "SSM_WorkerSha256": shadow_rollout.sha256(new_worker),
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
    assert binding.read_bytes() == old_binding
    assert target.stat().st_mode & 0o777 == 0o750
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
    host_evidence = json.loads(
        next(line for line in replay.stdout.splitlines() if line.startswith("{"))
    )
    assert set(host_evidence) == shadow_rollout.HOST_EVIDENCE_KEYS
    assert host_evidence["worker_metadata_valid"] is True
    assert host_evidence["binding_metadata_valid"] is True
    current = json.loads(binding.read_text(encoding="utf-8"))
    assert current["rollout_attempt_id"] == "789"
    assert current["worker_sha256"] == shadow_rollout.sha256(new_worker)
    collision_environment = dict(environment)
    collision_environment["SSM_ShadowDocumentSha256"] = "d" * 64
    collision = subprocess.run(
        ["bash", "-c", base_command], env=collision_environment,
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert collision.returncode != 0
    assert target.read_bytes() == new_worker
    assert json.loads(binding.read_text())["shadow_document_sha256"] == "c" * 64


def test_host_worker_is_shell_valid_and_has_mandatory_pair_guard():
    completed = subprocess.run(["bash", "-n", str(WORKER)], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    text = WORKER.read_text(encoding="utf-8")
    assert "validate_rollout_binding" in text
    assert "--expected-worker-sha256" in text
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
    assert adapter.commands == [{
        "action": "install", "command_id": command_id, "accepted": True,
        "status": "Success", "response_code": 0,
    }]


def test_aws_send_response_loss_preserves_uncertain_acceptance(monkeypatch):
    rollout = shadow_rollout.RolloutTuple(
        source_sha="a" * 40, worker_sha256="b" * 64,
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
    assert evidence["host_new"]["binding_metadata_valid"] is True
    assert evidence["host_final"]["worker_owner"] == "0:0"
    assert evidence["legacy_transition"]["mode"] == "steady"
    assert evidence["legacy_transition"]["result"] == "n-a"
    assert not any(
        call[0] == "call"
        and (
            "list-commands" in call[1]
            or "list-command-invocations" in call[1]
        )
        for call in calls
    )
    assert "AWS_SECRET_ACCESS_KEY" not in audit.read_text()


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
