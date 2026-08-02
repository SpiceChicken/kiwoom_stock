"""Static contracts for the bounded, no-order shadow activation plane."""

from pathlib import Path
import subprocess

import yaml


SCRIPT = Path("deploy/ec2/shadow_worker_control.sh")
DOCUMENT = Path("deploy/ssm/shadow-worker-document.yaml")
WORKFLOW = Path(".github/workflows/cd-shadow-worker-activation.yml")
POLICY = Path("deploy/iam/github-shadow-activation-policy.json.example")


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
        "--remove-orphans",
        "/api/dostk/ordr",
        "AccountService",
        "OAuth revoke",
        "slack-sdk",
        "google.generativeai",
        "boto3",
    ):
        assert forbidden not in text.casefold()


def test_shadow_ssm_document_has_only_oneshot_or_stop_and_no_secret_parameters():
    document = yaml.safe_load(DOCUMENT.read_text(encoding="utf-8"))
    parameters = document["parameters"]
    assert parameters["DesiredState"]["allowedValues"] == ["oneshot", "stop"]
    assert set(parameters) == {
        "DesiredState",
        "ImageDigest",
        "SourceSha",
        "ActivationId",
        "ComposeShadowSha256",
        "ExpectedInstanceId",
        "Region",
    }
    text = DOCUMENT.read_text(encoding="utf-8")
    assert "/usr/local/sbin/kiwoom-shadow-worker" in text
    assert "AWS-RunShellScript" not in text
    assert "SecureString" not in text
    assert "AppKey" not in text
    assert "SecretKey" not in text


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
    }
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
    assert "DesiredState=oneshot" in text
    assert "runtime safe result was not found" in text
    assert '"orders": side_effects["broker_orders"]' in text
    assert '"database": bool(result.get("db_identity"))' in text


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
