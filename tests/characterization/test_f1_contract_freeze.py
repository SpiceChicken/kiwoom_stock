"""Independent F1 snapshots for CLI and settings observable contracts."""

import json
from pathlib import Path
from types import SimpleNamespace

import kiwoom_stock.cli as cli
from kiwoom_stock.application.execution import ExecutionMode
from kiwoom_stock.settings import SETTING_SPECS


SOURCE_SHA = "a" * 40
IMAGE_DIGEST = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64


def _settings(*, mode: ExecutionMode, candidate: bool = False, path: Path | None = None):
    return SimpleNamespace(
        execution=SimpleNamespace(mode=mode),
        swing_candidate=SimpleNamespace(
            enabled=candidate,
            database_path=path,
            portfolio_id="candidate-v1" if candidate else None,
        ),
    )


def test_shadow_cli_success_stdout_and_exit_are_exact(monkeypatch, capsys):
    from kiwoom_stock.core import config
    from kiwoom_stock.application import shadow_worker

    monkeypatch.setattr(
        config,
        "validate_environment_settings",
        lambda: _settings(mode=ExecutionMode.SHADOW_ONCE),
    )
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_once_managed",
        lambda _policy, **_kwargs: SimpleNamespace(
            to_safe_dict=lambda: {
                "activation_id": "f1-success",
                "mode": "shadow-once",
                "status": "PASS",
            }
        ),
    )

    result = cli.main(
        [
            "shadow-once",
            "--source-sha", SOURCE_SHA,
            "--image-digest", IMAGE_DIGEST,
            "--activation-id", "f1-success",
        ]
    )

    assert result == 0
    captured = capsys.readouterr()
    assert captured.out == (
        '{"activation_id": "f1-success", "mode": "shadow-once", '
        '"status": "PASS"}\n'
    )
    assert captured.err == ""


def test_shadow_cli_failure_stdout_stderr_and_exit_are_exact(monkeypatch, capsys):
    from kiwoom_stock.core import config
    from kiwoom_stock.application import shadow_worker

    monkeypatch.setattr(
        config,
        "validate_environment_settings",
        lambda: _settings(mode=ExecutionMode.SHADOW_ONCE),
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("secret-bearing detail must not escape")

    monkeypatch.setattr(shadow_worker, "run_shadow_once_managed", fail)

    result = cli.main(
        [
            "shadow-once",
            "--source-sha", SOURCE_SHA,
            "--image-digest", IMAGE_DIGEST,
            "--activation-id", "f1-failure",
        ]
    )

    assert result == 1
    captured = capsys.readouterr()
    expected = '{"error_type": "RuntimeError", "status": "FAILED"}\n'
    assert captured.out == expected
    assert captured.err == expected
    assert "secret-bearing" not in captured.out + captured.err


def test_shadow_cli_forwards_enabled_candidate_identity_exactly(monkeypatch, capsys, tmp_path):
    from kiwoom_stock.core import config
    from kiwoom_stock.application import shadow_worker

    candidate_path = (tmp_path / "candidate.sqlite3").resolve()
    settings = _settings(
        mode=ExecutionMode.SHADOW_ONCE,
        candidate=True,
        path=candidate_path,
    )
    captured = []
    monkeypatch.setattr(config, "validate_environment_settings", lambda: settings)
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_once_managed",
        lambda policy, **_kwargs: captured.append(policy)
        or SimpleNamespace(
            to_safe_dict=lambda: {
                "activation_id": "f1-candidate",
                "mode": "shadow-once",
                "status": "PASS",
            }
        ),
    )

    assert cli.main(
        [
            "shadow-once",
            "--source-sha", SOURCE_SHA,
            "--image-digest", IMAGE_DIGEST,
            "--activation-id", "f1-candidate",
        ]
    ) == 0
    capsys.readouterr()

    assert len(captured) == 1
    assert captured[0].swing_candidate_enabled is True
    assert captured[0].swing_candidate_database_path == candidate_path
    assert captured[0].swing_candidate_portfolio_id == "candidate-v1"


def test_setting_specs_have_an_independent_full_metadata_snapshot():
    expected = (
        ("KIWOOM_EXECUTION_MODE", "enum", "no", "check-only", "execution policy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SWING_CANDIDATE_ENABLED", "strict boolean", "no", "false", "isolated swing shadow candidate", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SWING_CANDIDATE_DB_PATH", "file path", "candidate enabled", "./runtime/swing-candidate.sqlite3", "isolated swing candidate ledger", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SWING_CANDIDATE_PORTFOLIO_ID", "string", "candidate enabled", "swing-paper-v1", "isolated swing candidate portfolio", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SWING_STRATEGY_SEMANTICS_VERSION", "string", "no", "swing-v1", "swing candidate policy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_IMAGE_REF", "OCI image digest", "shadow execution", None, "shadow activation attestation", False, ("prod", "production-like")),
        ("KIWOOM_IMAGE_DIGEST", "OCI image digest", "shadow execution", None, "shadow activation attestation", False, ("prod", "production-like")),
        ("KIWOOM_REQUIRE_SHADOW_VOLUME", "strict boolean", "shadow execution", None, "shadow volume attestation", False, ("prod", "production-like")),
        ("KIWOOM_REQUIRE_SHADOW_TELEMETRY", "strict boolean", "shadow execution", None, "shadow telemetry attestation", False, ("prod", "production-like")),
        ("KIWOOM_SHADOW_TELEMETRY_PATH", "file path", "shadow execution", None, "shadow telemetry sidecar", False, ("prod", "production-like")),
        ("KIWOOM_API_MODE", "enum", "no", "disabled", "runtime composition", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_PROCESS_NAME", "string", "yes", None, "runtime lifecycle", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_APP_ENV", "enum", "no", "local", "retention policy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_CREDENTIALS_DIR", "absolute directory path", "for mock/prod", None, "strict credential provider", False, ("staging", "prod", "production-like")),
        ("KIWOOM_OUTPUT_DIR", "directory path", "no", "current working directory", "report output", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_DB_PATH", "file path", "no", "trades.db", "runtime and post-market SQLite", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SLACK_WEBHOOK_URL", "URL", "no", None, "Slack webhook", True, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SLACK_BOT_TOKEN", "string", "with channel ID", None, "Slack file upload", True, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SLACK_CHANNEL_ID", "string", "with bot token", None, "Slack file upload", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_GEMINI_API_KEY", "string", "no", None, "Gemini reports", True, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_S3_BUCKET_NAME", "string", "no; production-class missing preserves outputs", None, "S3 archive", False, ("prod", "production-like")),
        ("KIWOOM_AWS_REGION", "string", "no", None, "future AWS session", False, ("staging", "prod", "production-like")),
        ("KIWOOM_FAST_INTERVAL_SECONDS", "positive float", "no", "10", "TradingEngine", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_SLOW_INTERVAL_SECONDS", "positive float", "no", "60", "TradingEngine", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_MAX_WORKERS", "positive integer", "no", "8", "TradingEngine", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_MARKET_PROXY_CODE", "six-digit string", "no", "069500", "MarketAnalyzer", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_MAX_STOCKS", "positive integer", "no", "50", "StockManager", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_ETF_KEYWORDS", "comma-separated strings", "no", "empty list", "StockManager", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_DEBUG_MODE", "strict boolean", "no", "false", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_DAY_TRADE_EXIT_TIME", "HH:MM", "no", "15:30", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_ENTRY_DEADLINE", "HH:MM", "no", "15:00", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_CUMULATIVE_TRADE_RETURN_SCORE_FLOOR", "float percentage points", "no", "-5", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_TOTAL_LOSS_LIMIT", "deprecated float percentage points", "no", None, "settings migration only", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_TARGET_STOP_UNIT_VERSION", "enum", "atomic group", "percentage-points-v1", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS", "positive float percentage points", "atomic group", "3.0", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
        ("KIWOOM_STOP_LOSS_PERCENTAGE_POINTS", "positive float percentage points", "atomic group", "3.0", "TradingStrategy", False, ("local", "dev", "test", "staging", "prod", "production-like")),
    )
    actual = tuple(
        (
            spec.name,
            spec.value_type,
            spec.required,
            spec.default,
            spec.consumer,
            spec.sensitive,
            spec.environments,
        )
        for spec in SETTING_SPECS
    )
    assert actual == expected


def test_cli_failure_payload_is_valid_json_without_sensitive_fields(monkeypatch, capsys):
    from kiwoom_stock.core import config
    from kiwoom_stock.application import shadow_worker

    monkeypatch.setattr(
        config,
        "validate_environment_settings",
        lambda: _settings(mode=ExecutionMode.SHADOW_ONCE),
    )
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_once_managed",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("hidden")),
    )

    assert cli.main(
        [
            "shadow-once",
            "--source-sha", SOURCE_SHA,
            "--image-digest", IMAGE_DIGEST,
            "--activation-id", "f1-json",
        ]
    ) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error_type": "ValueError", "status": "FAILED"}
    assert json.loads(captured.err) == {"error_type": "ValueError", "status": "FAILED"}
