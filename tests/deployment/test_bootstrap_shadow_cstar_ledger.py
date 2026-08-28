"""Regression tests for the DynamoDB bootstrap adapter boundary."""

from dataclasses import replace
import hashlib

import boto3
import pytest
from botocore.stub import Stubber

from deploy.bootstrap_shadow_cstar_ledger import (
    BootstrapConfig,
    BootstrapError,
    _seed_ledger,
    verify_source_artifacts,
)


def _artifact_config() -> BootstrapConfig:
    return BootstrapConfig(
        table_name="table",
        generation="cstar-g000001",
        protocol_sha256="a" * 64,
        source_sha="b" * 40,
        image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
        compose_shadow_sha256="d" * 64,
        worker_sha256="e" * 64,
        validator_sha256="f" * 64,
        shadow_document_sha256="0" * 64,
        rollout_attempt_id="1",
    )


def test_source_artifacts_are_verified_at_exact_git_revision(monkeypatch, tmp_path):
    blobs = {
        "compose.shadow.yaml": b"compose",
        "deploy/ec2/shadow_worker_control.sh": b"worker",
        "deploy/ec2/shadow_runtime_evidence.py": b"validator",
        "deploy/ssm/shadow-worker-document.yaml": b'{"b":2,"a":1}',
    }
    config = replace(
        _artifact_config(),
        compose_shadow_sha256=hashlib.sha256(b"compose").hexdigest(),
        worker_sha256=hashlib.sha256(b"worker").hexdigest(),
        validator_sha256=hashlib.sha256(b"validator").hexdigest(),
        shadow_document_sha256=hashlib.sha256(b'{"a":1,"b":2}').hexdigest(),
    )
    monkeypatch.setattr(
        "deploy.bootstrap_shadow_cstar_ledger._source_blob",
        lambda _root, _sha, path: blobs[path],
    )

    verify_source_artifacts(config, tmp_path)


def test_source_artifact_mismatch_fails_before_aws_mutation(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "deploy.bootstrap_shadow_cstar_ledger._source_blob",
        lambda _root, _sha, _path: b"wrong",
    )

    with pytest.raises(BootstrapError, match="source artifact hash mismatch"):
        verify_source_artifacts(_artifact_config(), tmp_path)


def test_seed_ledger_uses_native_values_for_resource_backed_transaction(monkeypatch):
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret-key")
    table = boto3.resource("dynamodb", region_name="ap-northeast-2").Table("table")
    client = table.meta.client
    stubber = Stubber(client)
    for _ in range(3):
        stubber.add_response("get_item", {})
    stubber.add_response("transact_write_items", {})
    stubber.activate()
    captured = {}

    def capture(**kwargs):
        captured["params"] = kwargs["params"]

    client.meta.events.register(
        "before-parameter-build.dynamodb.TransactWriteItems",
        capture,
        unique_id="test-bootstrap-transact-shape",
    )
    try:
        _seed_ledger(
            table,
            BootstrapConfig(
                table_name="table",
                generation="cstar-g000001",
                protocol_sha256="a" * 64,
                source_sha="b" * 40,
                image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64,
                compose_shadow_sha256="d" * 64,
                worker_sha256="e" * 64,
                validator_sha256="f" * 64,
                shadow_document_sha256="0" * 64,
                rollout_attempt_id="1",
            ),
            "arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/start",
            "arn:aws:scheduler:ap-northeast-2:380648615401:schedule/cstar/stop",
        )
    finally:
        client.meta.events.unregister(
            "before-parameter-build.dynamodb.TransactWriteItems",
            unique_id="test-bootstrap-transact-shape",
        )
        stubber.deactivate()

    transactions = captured["params"]["TransactItems"]
    assert len(transactions) == 3
    for transaction in transactions:
        item = transaction["Put"]["Item"]
        assert item["PK"]["S"].startswith(("GEN#", "RELEASE#", "CONTROL#"))
        assert item["SK"] == {"S": "META"} or item["SK"] == {"S": "RELEASE"}
