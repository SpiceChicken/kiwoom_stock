"""Regression tests for the DynamoDB bootstrap adapter boundary."""

import boto3
from botocore.stub import Stubber

from deploy.bootstrap_shadow_cstar_ledger import BootstrapConfig, _seed_ledger


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
