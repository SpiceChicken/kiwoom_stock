from dataclasses import replace
from types import SimpleNamespace

import pytest

from kiwoom_stock.application.execution import (
    ActivationTuple,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionPolicyError,
    LiveActivationNotImplemented,
    SHADOW_DATABASE_PATH,
)
from kiwoom_stock.cli import build_parser
from kiwoom_stock import cli
from kiwoom_stock.settings import (
    SETTING_SPEC_BY_NAME,
    Settings,
    SettingsValidationError,
)


def _activation() -> ActivationTuple:
    return ActivationTuple(
        source_sha="a" * 40,
        image_digest=(
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
        ),
        activation_id="shadow-test-1",
    )


def test_shadow_policy_is_fixed_and_fail_closed():
    policy = ExecutionPolicy.for_request(ExecutionMode.SHADOW_ONCE, _activation())

    assert policy.stock_code == "005930"
    assert policy.proxy_code == "069500"
    assert policy.max_cycles == 1
    assert policy.max_http_attempts == 23
    assert policy.shadow_database_path == SHADOW_DATABASE_PATH
    assert policy.market_reads is True
    assert policy.paper_ledger_writes is True
    assert policy.account_reads is False
    assert policy.broker_orders is False
    assert policy.oauth_revoke is False
    assert policy.external_notifications is False
    assert policy.reports is False
    policy.assert_paper_transition()

    with pytest.raises(ExecutionPolicyError, match="invariant"):
        replace(policy, broker_orders=True)
    with pytest.raises((TypeError, ValueError), match="shadow_database_path|init=False"):
        replace(policy, shadow_database_path=SHADOW_DATABASE_PATH.parent / "other.db")
    with pytest.raises(TypeError):
        ExecutionPolicy(
            mode=ExecutionMode.SHADOW_ONCE,
            activation=_activation(),
            shadow_database_path=SHADOW_DATABASE_PATH.parent / "other.db",
        )


def test_continuous_policy_reuses_exact_frozen_shadow_capabilities():
    once = ExecutionPolicy.for_request(ExecutionMode.SHADOW_ONCE, _activation())
    continuous = ExecutionPolicy.for_request(
        ExecutionMode.SHADOW_CONTINUOUS, _activation()
    )

    assert replace(continuous, mode=ExecutionMode.SHADOW_ONCE) == once
    assert continuous.max_cycles == 1
    assert continuous.max_http_attempts == 23
    assert continuous.shadow_database_path == SHADOW_DATABASE_PATH
    assert continuous.account_reads is False
    assert continuous.broker_orders is False
    assert continuous.oauth_revoke is False
    assert continuous.external_notifications is False
    assert continuous.reports is False
    continuous.assert_paper_transition()
    with pytest.raises(TypeError):
        ExecutionPolicy.for_request(
            ExecutionMode.SHADOW_ONCE,
            _activation(),
            shadow_database_path=SHADOW_DATABASE_PATH.parent / "other.db",
        )


@pytest.mark.parametrize(
    ("mode", "activation"),
    [
        (ExecutionMode.CHECK_ONLY, None),
        (ExecutionMode.CHECK_ONLY, _activation()),
        (ExecutionMode.SHADOW_ONCE, None),
        (ExecutionMode.SHADOW_CONTINUOUS, None),
    ],
)
def test_missing_and_check_only_requests_cannot_activate_shadow(mode, activation):
    with pytest.raises(ExecutionPolicyError):
        ExecutionPolicy.for_request(mode, activation)


def test_live_mode_is_hard_rejected():
    with pytest.raises(LiveActivationNotImplemented):
        ExecutionPolicy.for_request(ExecutionMode.LIVE, _activation())


def test_shadow_cli_requires_exact_activation_tuple_arguments():
    with pytest.raises(SystemExit) as missing:
        build_parser().parse_args(["shadow-once"])
    assert missing.value.code == 2

    parsed = build_parser().parse_args(
        [
            "shadow-once",
            "--source-sha",
            "a" * 40,
            "--image-digest",
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            "--activation-id",
            "shadow-test-1",
        ]
    )
    assert parsed.command == "shadow-once"

    continuous = build_parser().parse_args(
        [
            "shadow-worker",
            "--source-sha", "a" * 40,
            "--image-digest",
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            "--activation-id", "shadow-test-1",
        ]
    )
    assert continuous.command == "shadow-worker"


def test_typed_execution_mode_defaults_check_only_and_rejects_live_unknown():
    base = {"KIWOOM_PROCESS_NAME": "config-check"}
    assert Settings.from_mapping(base).execution.mode is ExecutionMode.CHECK_ONLY
    assert Settings.from_mapping(
        {**base, "KIWOOM_EXECUTION_MODE": "shadow-once"}
    ).execution.mode is ExecutionMode.SHADOW_ONCE
    assert Settings.from_mapping(
        {**base, "KIWOOM_EXECUTION_MODE": "shadow-continuous"}
    ).execution.mode is ExecutionMode.SHADOW_CONTINUOUS
    for value in ("live", "unknown", ""):
        with pytest.raises(SettingsValidationError):
            Settings.from_mapping({**base, "KIWOOM_EXECUTION_MODE": value})
    spec = SETTING_SPEC_BY_NAME["KIWOOM_EXECUTION_MODE"]
    assert spec.default == "check-only"
    assert spec.consumer == "execution policy"
    assert spec.sensitive is False


def test_cli_check_only_mode_rejects_shadow_before_runtime_construction(monkeypatch):
    settings = SimpleNamespace(
        execution=SimpleNamespace(mode=ExecutionMode.CHECK_ONLY)
    )
    from kiwoom_stock.core import config
    from kiwoom_stock.application import runtime

    monkeypatch.setattr(config, "validate_environment_settings", lambda: settings)
    monkeypatch.setattr(
        runtime,
        "create_shadow_runtime",
        lambda **_kwargs: pytest.fail("runtime constructed"),
    )
    result = cli.main(
        [
            "shadow-once",
            "--source-sha", "a" * 40,
            "--image-digest",
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            "--activation-id", "shadow-test-1",
        ]
    )
    assert result == 1


def test_continuous_cli_dispatches_only_the_frozen_continuous_policy(monkeypatch, capsys):
    settings = SimpleNamespace(
        execution=SimpleNamespace(mode=ExecutionMode.SHADOW_CONTINUOUS)
    )
    captured = []
    from kiwoom_stock.core import config
    from kiwoom_stock.application import runtime, shadow_worker

    monkeypatch.setattr(config, "validate_environment_settings", lambda: settings)
    monkeypatch.setattr(
        runtime,
        "create_shadow_runtime",
        lambda **_kwargs: pytest.fail("runner must own runtime construction"),
    )
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_continuous",
        lambda policy, **_kwargs: captured.append(policy)
        or SimpleNamespace(
            exit_code=0,
            to_safe_dict=lambda: {
                "event": "terminal",
                "status": "STOPPED",
                "mode": "shadow-continuous",
            },
        ),
    )

    result = cli.main(
        [
            "shadow-worker",
            "--source-sha", "a" * 40,
            "--image-digest",
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            "--activation-id", "continuous-test",
        ]
    )

    assert result == 0
    assert len(captured) == 1
    assert captured[0].mode is ExecutionMode.SHADOW_CONTINUOUS
    assert captured[0].max_cycles == 1
    assert '"status": "STOPPED"' in capsys.readouterr().out


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_sha", "A" * 40),
        ("image_digest", "example.invalid/image@sha256:" + "b" * 64),
        ("activation_id", "contains whitespace"),
    ],
)
def test_activation_tuple_rejects_unapproved_identity(field, value):
    values = {
        "source_sha": "a" * 40,
        "image_digest": "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
        "activation_id": "shadow-test-1",
    }
    values[field] = value
    with pytest.raises(ExecutionPolicyError):
        ActivationTuple(**values)
