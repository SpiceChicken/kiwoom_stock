"""Offline contract tests for the EC2 Parameter Store secret materializer."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from deploy.ec2.materialize_kiwoom_secrets import (
    DEFAULT_APP_PARAMETER,
    DEFAULT_GID,
    DEFAULT_SECRET_PARAMETER,
    DEFAULT_UID,
    MaterializationError,
    materialize,
)
from docker.validate_secret_paths import validate_secret_paths


class FakeParameterClient:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls: list[tuple[tuple[str, ...], bool]] = []

    def get_parameters(self, *, Names, WithDecryption):
        self.calls.append((tuple(Names), WithDecryption))
        if self.error is not None:
            raise self.error
        return self.response


def _response(app_key="synthetic-app-key", secret_key="synthetic-secret-key"):
    return {
        "Parameters": [
            {"Name": DEFAULT_APP_PARAMETER, "Value": app_key},
            {"Name": DEFAULT_SECRET_PARAMETER, "Value": secret_key},
        ],
        "InvalidParameters": [],
    }


def test_materialize_reads_exact_pair_and_writes_hardened_files(tmp_path):
    client = FakeParameterClient(_response())
    target = tmp_path / "run" / "credentials"

    names = materialize(
        client,
        target_dir=target,
        owner_uid=os.geteuid(),
        owner_gid=os.getegid(),
    )

    assert names == (DEFAULT_APP_PARAMETER, DEFAULT_SECRET_PARAMETER)
    assert client.calls == [
        ((DEFAULT_APP_PARAMETER, DEFAULT_SECRET_PARAMETER), True)
    ]
    assert target.stat().st_mode & 0o777 == 0o700
    assert (
        (target / "app-key").read_text(encoding="utf-8")
        == "synthetic-app-key"
    )
    assert (
        (target / "secret-key").read_text(encoding="utf-8")
        == "synthetic-secret-key"
    )
    assert (target / "app-key").stat().st_mode & 0o777 == 0o400
    assert (target / "secret-key").stat().st_mode & 0o777 == 0o400
    assert not list(target.glob("*.tmp"))


def test_operating_owner_defaults_match_root_launcher_preflight(tmp_path):
    assert (DEFAULT_UID, DEFAULT_GID) == (0, 0)
    client = FakeParameterClient(_response())
    target = tmp_path / "run" / "credentials"
    owner_options = {}
    if os.geteuid() != 0:
        # A non-root test runner cannot chown to root. The same-owner surrogate
        # exercises the root-launcher rule; constants/config assert production.
        owner_options = {
            "owner_uid": os.geteuid(),
            "owner_gid": os.getegid(),
        }
    materialize(client, target_dir=target, **owner_options)

    environment = {
        "KIWOOM_PROD_APP_KEY_FILE": str(target / "app-key"),
        "KIWOOM_PROD_SECRET_KEY_FILE": str(target / "secret-key"),
    }
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    assert validate_secret_paths(
        "prod",
        environ=environment,
        repository_root=checkout,
    ) == (target / "app-key", target / "secret-key")
    assert target.stat().st_uid == os.geteuid()
    assert target.stat().st_gid == os.getegid()
    assert target.stat().st_mode & 0o777 == 0o700
    assert all(
        path.stat().st_mode & 0o777 == 0o400
        for path in (target / "app-key", target / "secret-key")
    )


@pytest.mark.parametrize(
    "response",
    [
        {"Parameters": [], "InvalidParameters": []},
        {"Parameters": [{"Name": DEFAULT_APP_PARAMETER, "Value": "only-one"}]},
        {
            "Parameters": [
                {"Name": DEFAULT_APP_PARAMETER, "Value": "first"},
                {"Name": DEFAULT_APP_PARAMETER, "Value": "duplicate"},
            ]
        },
        {
            "Parameters": [
                {"Name": DEFAULT_APP_PARAMETER, "Value": "first"},
                {"Name": "/unexpected", "Value": "second"},
            ]
        },
    ],
)
def test_materialize_rejects_incomplete_or_unexpected_response(
    tmp_path, response
):
    client = FakeParameterClient(response)

    with pytest.raises(MaterializationError):
        materialize(
            client,
            target_dir=tmp_path / "credentials",
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize(
    "value", ["", " leading", "trailing ", "line\nbreak", "nul\x00byte"]
)
def test_materialize_rejects_unsafe_values_without_writing(tmp_path, value):
    client = FakeParameterClient(_response(app_key=value))
    target = tmp_path / "credentials"

    with pytest.raises(MaterializationError):
        materialize(
            client,
            target_dir=target,
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )

    assert not target.exists()


def test_materialize_fails_closed_on_parameter_client_error(tmp_path):
    client = FakeParameterClient(error=OSError("transport failure"))

    with pytest.raises(MaterializationError, match="request failed"):
        materialize(
            client,
            target_dir=tmp_path / "credentials",
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )


def test_main_requires_explicit_aws_region_without_injected_client(
    monkeypatch, capsys
):
    monkeypatch.delenv("KIWOOM_AWS_REGION", raising=False)

    from deploy.ec2.materialize_kiwoom_secrets import main

    assert main([]) == 1
    assert "AWS region is required" in capsys.readouterr().err


def test_materialize_rejects_symlinked_target_path(tmp_path):
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(MaterializationError, match="symlink"):
        materialize(
            FakeParameterClient(_response()),
            target_dir=alias_parent / "credentials",
            owner_uid=os.geteuid(),
            owner_gid=os.getegid(),
        )


def test_materialize_does_not_commit_partial_pair_when_staging_fails(tmp_path):
    target = tmp_path / "credentials"
    target.mkdir(mode=0o700)
    for name, value in (
        ("app-key", "old-app-key"),
        ("secret-key", "old-secret-key"),
    ):
        path = target / name
        path.write_text(value, encoding="utf-8")
        path.chmod(0o400)

    from deploy.ec2 import materialize_kiwoom_secrets as module

    original_write = module._write_atomic
    calls = 0

    def fail_second_write(path, value, uid, gid):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MaterializationError("synthetic staging failure")
        original_write(path, value, uid, gid)

    with patch.object(module, "_write_atomic", side_effect=fail_second_write):
        with pytest.raises(MaterializationError, match="staging failure"):
            materialize(
                FakeParameterClient(_response()),
                target_dir=target,
                owner_uid=os.geteuid(),
                owner_gid=os.getegid(),
            )

    assert (target / "app-key").read_text(encoding="utf-8") == "old-app-key"
    assert (
        (target / "secret-key").read_text(encoding="utf-8")
        == "old-secret-key"
    )
    assert not list(target.glob(".credentials.*.tmp"))


def test_materializer_assets_do_not_contain_secret_values():
    root = Path(__file__).resolve().parents[1]
    materializer = (
        root / "deploy/ec2/materialize_kiwoom_secrets.py"
    ).read_text(encoding="utf-8")
    service = (root / "deploy/ec2/kiwoom-secrets.service").read_text(
        encoding="utf-8"
    )
    config = (root / "deploy/ec2/kiwoom-secrets.conf.example").read_text(
        encoding="utf-8"
    )

    for text in (materializer, service, config):
        assert "synthetic-secret-key" not in text
        assert "AKIA" not in text
        assert "AWS_ACCESS_KEY_ID" not in text
    assert "WithDecryption=True" in materializer
    assert "KIWOOM_AWS_REGION" in config
    assert "KIWOOM_APP_PARAMETER" in config
    assert "KIWOOM_SECRET_PARAMETER" in config
    assert "KIWOOM_SECRET_UID=0" in config
    assert "KIWOOM_SECRET_GID=0" in config
