"""Static least-privilege contracts for the AWS deployment templates."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[1]
IAM_DIR = ROOT / "deploy" / "iam"
SSM_MANAGED_INSTANCE_CORE_V2_ACTIONS = {
    "ssm:DescribeAssociation",
    "ssm:DescribeDocument",
    "ssm:GetDeployablePatchSnapshotForInstance",
    "ssm:GetDocument",
    "ssm:GetManifest",
    "ssm:GetParameter",
    "ssm:GetParameters",
    "ssm:ListAssociations",
    "ssm:ListInstanceAssociations",
    "ssm:PutComplianceItems",
    "ssm:PutConfigurePackageResult",
    "ssm:PutInventory",
    "ssm:UpdateAssociationStatus",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:UpdateInstanceInformation",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ec2messages:AcknowledgeMessage",
    "ec2messages:DeleteMessage",
    "ec2messages:FailMessage",
    "ec2messages:GetEndpoint",
    "ec2messages:GetMessages",
    "ec2messages:SendReply",
}
PARAMETER_READ_ACTIONS = {
    "ssm:GetParameter",
    "ssm:GetParameters",
}


def _policy(name: str) -> dict[str, Any]:
    parsed = json.loads((IAM_DIR / name).read_text(encoding="utf-8"))
    return cast(dict[str, Any], parsed)


def _statements(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], policy["Statement"])


def test_oidc_trust_requires_exact_audience_and_resolved_subject():
    policy = _policy("github-oidc-trust-policy.json.example")
    statement = _statements(policy)[0]
    conditions = statement["Condition"]

    assert "StringLike" not in conditions
    assert conditions["StringEquals"] == {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
        "token.actions.githubusercontent.com:sub": "<GITHUB_OIDC_SUBJECT>",
    }
    assert statement["Action"] == "sts:AssumeRoleWithWebIdentity"


def test_github_deploy_role_cannot_read_runtime_parameters():
    policy = _policy("github-deploy-policy.json.example")
    actions = {
        action
        for statement in _statements(policy)
        for action in statement["Action"]
    }

    assert "ssm:GetParameter" not in actions
    assert "ssm:GetParameters" not in actions
    assert "ssm:GetParametersByPath" not in actions
    assert "iam:PassRole" not in actions
    assert "ssm:SendCommand" in actions
    assert not {action for action in actions if action.startswith("ecr:")}


def test_get_command_invocation_uses_only_required_wildcard():
    policy = _policy("github-deploy-policy.json.example")
    wildcard_statements = [
        statement
        for statement in _statements(policy)
        if statement["Resource"] == "*"
    ]

    wildcard_actions = {
        tuple(statement["Action"]) for statement in wildcard_statements
    }
    assert wildcard_actions == {
        ("ssm:GetCommandInvocation",),
    }


def test_github_deploy_role_is_ssm_only_and_targets_exact_resources():
    statements = _statements(_policy("github-deploy-policy.json.example"))
    send = next(
        statement
        for statement in statements
        if statement.get("Sid") == "RunExactProductionCheck"
    )

    assert send["Action"] == ["ssm:SendCommand"]
    assert send["Resource"] == [
        (
            "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:document/"
            "KiwoomStock-ProductionCheck"
        ),
        (
            "arn:aws:ec2:<AWS_REGION>:<AWS_ACCOUNT_ID>:instance/"
            "<EC2_INSTANCE_ID>"
        ),
    ]
    assert all(
        set(statement) <= {"Sid", "Effect", "Action", "Resource"}
        for statement in statements
    )


def test_custom_ssm_document_only_invokes_preinstalled_allowlisted_command():
    document_path = (
        ROOT / "deploy/ssm/production-check-document.yaml"
    )
    document = yaml.safe_load(document_path.read_text(encoding="utf-8"))
    parameters = document["parameters"]

    assert document["schemaVersion"] == "2.2"
    assert set(parameters) == {
        "ImageDigest",
        "SourceSha",
        "ComposeSha256",
        "ComposeProdSha256",
        "ExpectedInstanceId",
        "Region",
    }
    assert all(
        parameter["type"] == "String"
        and parameter["interpolationType"] == "ENV_VAR"
        and parameter["allowedPattern"].startswith("^")
        and parameter["allowedPattern"].endswith("$")
        for parameter in parameters.values()
    )
    assert parameters["ImageDigest"]["allowedPattern"] == (
        r"^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$"
    )
    assert parameters["SourceSha"]["allowedPattern"] == r"^[0-9a-f]{40}$"
    assert parameters["ExpectedInstanceId"]["allowedPattern"] == (
        r"^i-02cb0a404794bd43a$"
    )
    assert parameters["Region"]["allowedPattern"] == r"^ap-northeast-2$"
    steps = document["mainSteps"]
    assert len(steps) == 1
    assert steps[0]["action"] == "aws:runShellScript"
    commands = steps[0]["inputs"]["runCommand"]
    assert len(commands) == 1
    command = commands[0]
    assert command.startswith(
        "exec /usr/local/sbin/kiwoom-production-check "
    )
    assert "$SSM_ImageDigest" in command
    assert "$SSM_SourceSha" in command
    assert "curl" not in command
    assert "bash -c" not in command
    assert "sudo" not in command
    assert "\n" not in command


def test_custom_ssm_document_digest_pattern_matches_literal_dot_only():
    document = yaml.safe_load(
        (ROOT / "deploy/ssm/production-check-document.yaml").read_text(
            encoding="utf-8"
        )
    )
    pattern = re.compile(document["parameters"]["ImageDigest"]["allowedPattern"])
    valid = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + ("a" * 64)

    assert pattern.fullmatch(valid)
    assert not pattern.fullmatch(valid.replace("ghcr.io", "ghcrXio"))
    assert not pattern.fullmatch(valid.replace("sha256:", "sha256:\\"))


def test_custom_ssm_document_shell_quotes_render_exact_host_argv(tmp_path):
    document = yaml.safe_load(
        (ROOT / "deploy/ssm/production-check-document.yaml").read_text(
            encoding="utf-8"
        )
    )
    command = document["mainSteps"][0]["inputs"]["runCommand"][0]
    capture = tmp_path / "capture-argv"
    output = tmp_path / "argv.json"
    capture.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['ARGV_OUTPUT'], 'w', encoding='utf-8') as stream:\n"
        "    json.dump(sys.argv[1:], stream)\n",
        encoding="utf-8",
    )
    capture.chmod(0o755)
    image = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + ("b" * 64)
    source_sha = "c" * 40
    compose_sha = "d" * 64
    compose_prod_sha = "e" * 64
    environment = dict(os.environ)
    environment.update(
        {
            "ARGV_OUTPUT": str(output),
            "SSM_ImageDigest": image,
            "SSM_SourceSha": source_sha,
            "SSM_ComposeSha256": compose_sha,
            "SSM_ComposeProdSha256": compose_prod_sha,
            "SSM_ExpectedInstanceId": "i-02cb0a404794bd43a",
            "SSM_Region": "ap-northeast-2",
        }
    )

    completed = subprocess.run(
        [
            "bash",
            "-c",
            command.replace(
                "/usr/local/sbin/kiwoom-production-check",
                str(capture),
                1,
            ),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == [
        "--image",
        image,
        "--source-sha",
        source_sha,
        "--compose-sha256",
        compose_sha,
        "--compose-prod-sha256",
        compose_prod_sha,
        "--expected-instance-id",
        "i-02cb0a404794bd43a",
        "--region",
        "ap-northeast-2",
    ]


def test_custom_ssm_document_has_no_arbitrary_command_parameter():
    text = (
        ROOT / "deploy/ssm/production-check-document.yaml"
    ).read_text(encoding="utf-8")

    assert "AWS-RunShellScript" not in text
    assert "commands:" not in text
    assert "{{" not in text
    assert "/usr/local/sbin/kiwoom-production-check" in text


def test_ec2_runtime_role_reads_only_exact_parameter_paths():
    policy = _policy("ec2-runtime-policy.json.example")
    parameter_statement = next(
        statement
        for statement in _statements(policy)
        if statement.get("Sid") == "ReadExactKiwoomParameters"
    )

    assert parameter_statement["Action"] == ["ssm:GetParameters"]
    parameter_prefix = (
        "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:parameter/"
        "kiwoom-stock/prod/oauth/"
    )
    assert parameter_statement["Resource"] == [
        parameter_prefix + "app-key",
        parameter_prefix + "secret-key",
    ]


def test_ec2_runtime_role_needs_no_registry_permission_for_public_ghcr():
    policy = _policy("ec2-runtime-policy.json.example")
    actions = {
        action
        for statement in _statements(policy)
        for action in statement["Action"]
    }

    assert actions == {"ssm:GetParameters"}
    assert not {action for action in actions if action.startswith("ecr:")}


def test_custom_ssm_core_is_exact_managed_v2_minus_parameter_reads():
    policy = _policy(
        "ec2-ssm-core-no-parameter-read-policy.json.example"
    )
    statements = _statements(policy)
    actions = {
        action
        for statement in statements
        for action in statement["Action"]
    }

    assert actions == (
        SSM_MANAGED_INSTANCE_CORE_V2_ACTIONS - PARAMETER_READ_ACTIONS
    )
    for statement in statements:
        assert statement["Effect"] == "Allow"
        assert statement["Resource"] == "*"
        assert "NotAction" not in statement
        assert "NotResource" not in statement


def test_custom_ssm_core_has_no_ssm_parameter_action_or_wildcard():
    policy = _policy(
        "ec2-ssm-core-no-parameter-read-policy.json.example"
    )
    actions = {
        action
        for statement in _statements(policy)
        for action in statement["Action"]
    }

    assert not {
        action
        for action in actions
        if action.startswith("ssm:") and "Parameter" in action
    }
    assert not {action for action in actions if "*" in action}


def test_combined_ec2_policies_only_allow_exact_parameter_pair():
    core = _policy(
        "ec2-ssm-core-no-parameter-read-policy.json.example"
    )
    runtime = _policy("ec2-runtime-policy.json.example")
    parameter_actions = {
        "ssm:GetParameter",
        "ssm:GetParameters",
        "ssm:GetParametersByPath",
        "ssm:GetParameterHistory",
    }
    all_statements = [
        statement
        for policy in (core, runtime)
        for statement in _statements(policy)
    ]
    assert all(statement["Effect"] == "Allow" for statement in all_statements)
    assert all("NotAction" not in statement for statement in all_statements)
    assert not {
        action
        for statement in all_statements
        for action in statement["Action"]
        if "*" in action
    }
    grants = [
        statement
        for statement in all_statements
        if parameter_actions.intersection(statement["Action"])
    ]

    assert len(grants) == 1
    assert grants[0]["Sid"] == "ReadExactKiwoomParameters"
    assert grants[0]["Action"] == ["ssm:GetParameters"]
    assert grants[0]["Resource"] == [
        (
            "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:parameter/"
            "kiwoom-stock/prod/oauth/app-key"
        ),
        (
            "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:parameter/"
            "kiwoom-stock/prod/oauth/secret-key"
        ),
    ]


def test_operations_doc_records_exact_current_role_inventory():
    guide = (
        ROOT / "docs/operations/github-oidc-aws-bootstrap.md"
    ).read_text(encoding="utf-8")

    assert "attached managed policies: `0`" in guide
    assert "inline policies: `2`" in guide
    assert "`KiwoomStockSsmCoreWithoutParameterRead`" in guide
    assert "`KiwoomStockRuntimeMinimal`" in guide


def test_installer_check_is_non_mutating_and_matches_systemd_contract():
    installer = ROOT / "deploy/ec2/install_secret_materializer.sh"
    completed = subprocess.run(
        [str(installer), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    service = (ROOT / "deploy/ec2/kiwoom-secrets.service").read_text(
        encoding="utf-8"
    )
    config = (ROOT / "deploy/ec2/kiwoom-secrets.conf.example").read_text(
        encoding="utf-8"
    )

    assert completed.returncode == 0
    assert completed.stdout == "check passed (no changes made)\n"
    assert "/opt/kiwoom-stock/.venv/bin/python" in service
    assert "RuntimeDirectory=kiwoom-stock" in service
    assert "RuntimeDirectoryMode=0700" in service
    assert "RuntimeDirectoryPreserve=yes" in service
    assert "UMask=0077" in service
    assert "ReadWritePaths=/run/kiwoom-stock" in service
    assert service.index("RuntimeDirectory=kiwoom-stock") < service.index(
        "ReadWritePaths=/run/kiwoom-stock"
    )
    assert "KIWOOM_AWS_REGION=" in config


def test_installer_preserves_existing_live_configuration():
    installer = (ROOT / "deploy/ec2/install_secret_materializer.sh").read_text(
        encoding="utf-8"
    )

    assert 'if [[ ! -e "$CONFIG_DIR/kiwoom-secrets.conf" ]]' in installer
    assert '"$CONFIG_DIR/kiwoom-secrets.conf.example"' in installer
