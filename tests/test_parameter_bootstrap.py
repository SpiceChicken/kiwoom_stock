"""Offline tests for hidden-input Parameter Store bootstrap behavior."""

from __future__ import annotations

from collections import defaultdict

import pytest

from tools.bootstrap_kiwoom_parameters import (
    BootstrapError,
    DEFAULT_APP_PARAMETER,
    DEFAULT_SECRET_PARAMETER,
    bootstrap_pair,
    main,
    parameter_states,
)


class FakeParameterAdminClient:
    def __init__(self, existing=(), fail_put_number=None):
        self.existing = set(existing)
        self.fail_put_number = fail_put_number
        self.describe_calls: list[str] = []
        self.put_calls: list[dict] = []
        self.delete_calls: list[str] = []
        self.call_counts = defaultdict(int)

    def describe_parameters(self, **kwargs):
        name = kwargs["ParameterFilters"][0]["Values"][0]
        self.describe_calls.append(name)
        parameters = [{"Name": name}] if name in self.existing else []
        return {"Parameters": parameters}

    def put_parameter(self, **kwargs):
        self.call_counts["put"] += 1
        if self.call_counts["put"] == self.fail_put_number:
            raise OSError("synthetic write failure")
        self.put_calls.append(kwargs)
        self.existing.add(kwargs["Name"])
        return {"Version": 1}

    def delete_parameter(self, **kwargs):
        name = kwargs["Name"]
        self.delete_calls.append(name)
        self.existing.discard(name)
        return {}


def test_parameter_states_uses_metadata_without_getting_values():
    client = FakeParameterAdminClient(existing=(DEFAULT_APP_PARAMETER,))

    assert parameter_states(client) == (True, False)
    assert client.describe_calls == [
        DEFAULT_APP_PARAMETER,
        DEFAULT_SECRET_PARAMETER,
    ]
    assert not hasattr(client, "get_parameter")


def test_bootstrap_creates_standard_securestring_pair():
    client = FakeParameterAdminClient()

    names = bootstrap_pair(
        client,
        app_key="synthetic-app-key",
        secret_key="synthetic-secret-key",
    )

    assert names == (DEFAULT_APP_PARAMETER, DEFAULT_SECRET_PARAMETER)
    assert [call["Name"] for call in client.put_calls] == list(names)
    assert all(call["Type"] == "SecureString" for call in client.put_calls)
    assert all(call["Tier"] == "Standard" for call in client.put_calls)
    assert all(call["Overwrite"] is False for call in client.put_calls)


@pytest.mark.parametrize(
    "existing",
    [
        (DEFAULT_APP_PARAMETER,),
        (DEFAULT_SECRET_PARAMETER,),
        (DEFAULT_APP_PARAMETER, DEFAULT_SECRET_PARAMETER),
    ],
)
def test_initial_creation_rejects_any_existing_parameter(existing):
    client = FakeParameterAdminClient(existing=existing)

    with pytest.raises(BootstrapError, match="both parameters to be absent"):
        bootstrap_pair(
            client,
            app_key="synthetic-app-key",
            secret_key="synthetic-secret-key",
        )

    assert client.put_calls == []


def test_rotation_requires_both_existing_parameters():
    client = FakeParameterAdminClient(existing=(DEFAULT_APP_PARAMETER,))

    with pytest.raises(BootstrapError, match="both parameters to exist"):
        bootstrap_pair(
            client,
            app_key="new-app-key",
            secret_key="new-secret-key",
            overwrite=True,
        )


def test_initial_partial_failure_removes_new_first_parameter():
    client = FakeParameterAdminClient(fail_put_number=2)

    with pytest.raises(BootstrapError, match="do not restart"):
        bootstrap_pair(
            client,
            app_key="synthetic-app-key",
            secret_key="synthetic-secret-key",
        )

    assert client.delete_calls == [DEFAULT_APP_PARAMETER]
    assert client.existing == set()


@pytest.mark.parametrize(
    "value",
    ["", " leading", "trailing ", "line\nbreak", "nul\x00byte"],
)
def test_bootstrap_rejects_unsafe_value_without_writing(value):
    client = FakeParameterAdminClient()

    with pytest.raises(BootstrapError):
        bootstrap_pair(
            client,
            app_key=value,
            secret_key="synthetic-secret-key",
        )

    assert client.put_calls == []


def test_check_mode_does_not_prompt_or_write(monkeypatch, capsys):
    client = FakeParameterAdminClient()

    def unexpected_prompt(_message):
        raise AssertionError("check mode must not prompt")

    monkeypatch.setattr(
        "tools.bootstrap_kiwoom_parameters.getpass",
        unexpected_prompt,
    )

    result = main(
        [
            "--profile",
            "synthetic-profile",
            "--region",
            "ap-northeast-2",
            "--check",
        ],
        client=client,
    )

    assert result == 0
    assert "app=missing, secret=missing" in capsys.readouterr().out
    assert client.put_calls == []
