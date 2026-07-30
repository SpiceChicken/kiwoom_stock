from datetime import date, datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from kiwoom_stock.application.credentials import KiwoomClientCredentials, SensitiveText
from kiwoom_stock.application.runtime import (
    RuntimeDisabledError,
    create_trading_runtime,
)
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.reporting.minute_chart import _extract_and_save_1min_chart
from kiwoom_stock.reporting import minute_chart as minute_chart_module
from kiwoom_stock.settings import Settings, SettingsIssue, SettingsValidationError


class RecordingLedger:
    def __init__(self, events, close_error=None):
        self.events = events
        self.close_error = close_error

    def close(self):
        self.events.append("ledger.close")
        if self.close_error is not None:
            raise self.close_error


class RecordingPhysicalRepository:
    def __init__(self, events, close_error=None):
        self.events = events
        self.close_error = close_error

    def close(self):
        self.events.append("physical.close")
        if self.close_error is not None:
            raise self.close_error


def _settings(db_path: Path) -> Settings:
    credentials_dir = db_path.parent / "credentials"
    return Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "mock",
            "KIWOOM_APP_ENV": "staging",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir),
            "KIWOOM_PROCESS_NAME": "test-process",
            "KIWOOM_DB_PATH": str(db_path),
        }
    )


class _SyntheticProvider:
    def load(self):
        return KiwoomClientCredentials(
            SensitiveText("synthetic-app-key"),
            SensitiveText("synthetic-secret-key"),
        )


def _create_runtime(**kwargs):
    kwargs.setdefault("credential_provider_factory", lambda path: _SyntheticProvider())
    return create_trading_runtime(**kwargs)


def _activated_config(settings: Settings):
    return SimpleNamespace(
        CONFIG={
            "app_env": "local",
        },
        STRATEGY_CONFIG={"process_name": "test-process"},
        OUTPUT_DIR_STR="/isolated/output/20260718",
        configure_from_environment=MagicMock(
            side_effect=AssertionError("settings reloaded")
        ),
        validate_environment_settings=MagicMock(
            side_effect=AssertionError("settings reloaded")
        ),
        activate_runtime_settings=MagicMock(return_value=settings),
    )


def test_create_trading_runtime_builds_configured_persistence_before_client_and_engine(
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    ledger = RecordingLedger(events)
    physical_repository = RecordingPhysicalRepository(events)
    config_module = SimpleNamespace(
        CONFIG={
            "app_env": "local",
        },
        STRATEGY_CONFIG={"process_name": "test-process"},
        OUTPUT_DIR_STR="/tmp/kiwoom-test-output/20260718",
    )

    def validate_environment_settings():
        events.append("settings")
        return settings

    def ledger_factory(db_path):
        events.append(("ledger", db_path))
        return ledger

    def physical_repository_factory(candidate):
        assert candidate is ledger
        events.append(("physical", candidate))
        return physical_repository

    client = MagicMock()
    client.ensure_auth_ready.side_effect = lambda: events.append("auth.ready")

    def client_factory(**kwargs):
        events.append(("client", kwargs))
        return client

    def engine_factory(
        client,
        app_config,
        *,
        ledger: object,
        physical_state_repository: object,
    ):
        events.append(
            (
                "engine",
                client,
                app_config["process_name"],
                ledger,
                physical_state_repository,
            )
        )
        return "monitor"

    config_module.validate_environment_settings = validate_environment_settings
    config_module.activate_runtime_settings = MagicMock(return_value=settings)
    credentials = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    provider = MagicMock()
    provider.load.return_value = credentials
    provider_factory = MagicMock(return_value=provider)

    runtime = _create_runtime(
        today=date(2026, 7, 18),
        config_module=config_module,
        credential_provider_factory=provider_factory,
        client_factory=client_factory,
        engine_factory=engine_factory,
        ledger_factory=ledger_factory,
        physical_state_repository_factory=physical_repository_factory,
    )

    assert runtime.settings is settings
    assert runtime.app_config["process_name"] == "test-process"
    assert runtime.output_dir_str == "/tmp/kiwoom-test-output/20260718"
    assert runtime.client is client
    assert runtime.monitor == "monitor"
    assert "app_config" not in repr(runtime)
    provider_factory.assert_called_once_with(settings.kiwoom.credentials_dir)
    provider.load.assert_called_once_with()
    assert events[3][1]["credentials"] is credentials
    assert events == [
        "settings",
        ("ledger", settings.database.path),
        ("physical", ledger),
        (
            "client",
            {
                "credentials": ANY,
                "endpoint": settings.kiwoom.endpoint,
            },
        ),
        "auth.ready",
        ("engine", client, "test-process", ledger, physical_repository),
    ]


def test_create_trading_runtime_does_not_build_client_when_settings_are_invalid():
    validation_error = SettingsValidationError(
        (SettingsIssue("KIWOOM_PROCESS_NAME", "is required and must be a non-empty string"),)
    )
    config_module = SimpleNamespace(
        CONFIG={},
        STRATEGY_CONFIG={},
        OUTPUT_DIR_STR="",
        validate_environment_settings=MagicMock(side_effect=validation_error),
    )
    client_factory = MagicMock(side_effect=AssertionError("client must not be created"))
    engine_factory = MagicMock(side_effect=AssertionError("engine must not be created"))
    ledger_factory = MagicMock(side_effect=AssertionError("ledger must not be created"))
    physical_factory = MagicMock(side_effect=AssertionError("physical adapter must not be created"))

    with pytest.raises(SettingsValidationError):
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=client_factory,
            engine_factory=engine_factory,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
        )

    ledger_factory.assert_not_called()
    physical_factory.assert_not_called()
    client_factory.assert_not_called()
    engine_factory.assert_not_called()


def test_disabled_mode_rejects_runtime_before_local_or_external_resources():
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "disabled",
            "KIWOOM_PROCESS_NAME": "config-check",
        }
    )
    config_module = SimpleNamespace(
        CONFIG={},
        STRATEGY_CONFIG={},
        OUTPUT_DIR_STR="",
        validate_environment_settings=MagicMock(return_value=settings),
    )
    ledger_factory = MagicMock()
    physical_factory = MagicMock()
    provider_factory = MagicMock()
    client_factory = MagicMock()
    engine_factory = MagicMock()

    with pytest.raises(RuntimeDisabledError):
        create_trading_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            credential_provider_factory=provider_factory,
            client_factory=client_factory,
            engine_factory=engine_factory,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
        )

    ledger_factory.assert_not_called()
    physical_factory.assert_not_called()
    provider_factory.assert_not_called()
    client_factory.assert_not_called()
    engine_factory.assert_not_called()


def test_disabled_mode_rejects_minute_chart_before_clock_activation_or_provider():
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "disabled",
            "KIWOOM_PROCESS_NAME": "config-check",
        }
    )
    config_module = SimpleNamespace(
        validate_environment_settings=MagicMock(return_value=settings),
        activate_runtime_settings=MagicMock(),
    )
    clock = MagicMock()
    provider_factory = MagicMock()
    client_factory = MagicMock()

    with pytest.raises(RuntimeDisabledError):
        _extract_and_save_1min_chart(
            None,
            config_module=config_module,
            datetime_type=clock,
            client_factory=client_factory,
            collector_factory=MagicMock(),
            database_factory=MagicMock(),
            target_logger=MagicMock(),
            credential_provider_factory=provider_factory,
        )

    clock.now.assert_not_called()
    config_module.activate_runtime_settings.assert_not_called()
    provider_factory.assert_not_called()
    client_factory.assert_not_called()


def test_minute_chart_preflights_provider_once_and_reuses_bundle(tmp_path):
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = SimpleNamespace(
        OUTPUT_DIR_STR=str(tmp_path / "output"),
        validate_environment_settings=MagicMock(return_value=settings),
        activate_runtime_settings=MagicMock(return_value=settings),
    )
    credentials = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    provider = MagicMock()
    provider.load.return_value = credentials
    provider_factory = MagicMock(return_value=provider)
    client = SimpleNamespace(market=object(), close=MagicMock())
    client_factory = MagicMock(return_value=client)

    class EmptyDatabase:
        def __init__(self, path):
            self.path = path

        def get_today_traded_targets(self, target_date):
            return []

        def close(self):
            return None

    clock = SimpleNamespace(now=lambda: datetime(2026, 7, 18, 12, 0))

    result = _extract_and_save_1min_chart(
        None,
        config_module=config_module,
        datetime_type=clock,
        client_factory=client_factory,
        collector_factory=MagicMock(),
        database_factory=EmptyDatabase,
        target_logger=MagicMock(),
        credential_provider_factory=provider_factory,
    )

    assert result == []
    provider_factory.assert_called_once_with(settings.kiwoom.credentials_dir)
    provider.load.assert_called_once_with()
    client_factory.assert_called_once_with(
        credentials=credentials,
        endpoint=settings.kiwoom.endpoint,
    )
    client.close.assert_called_once_with()


def test_minute_chart_success_closes_client_after_last_api_consumer(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    config_module = SimpleNamespace(
        OUTPUT_DIR_STR=str(output_dir),
        validate_environment_settings=MagicMock(return_value=settings),
        activate_runtime_settings=MagicMock(return_value=settings),
    )
    provider = MagicMock()
    provider.load.return_value = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    events = []
    client = SimpleNamespace(
        market=object(),
        close=MagicMock(side_effect=lambda: events.append("client.close")),
    )
    collector = MagicMock()
    collector.fetch_minute_chart.side_effect = lambda *args, **kwargs: (
        events.append("api.read")
        or [{"체결시간": "20260718100000", "현재가": "100"}]
    )
    monkeypatch.setattr(
        minute_chart_module,
        "_read_traded_targets",
        lambda *args, **kwargs: [
            {"stock_code": "005930", "stock_name": "Sample"}
        ],
    )

    result = _extract_and_save_1min_chart(
        "2026-07-18",
        config_module=config_module,
        datetime_type=SimpleNamespace(
            now=lambda: datetime(2026, 7, 18, 12, 0)
        ),
        client_factory=MagicMock(return_value=client),
        collector_factory=MagicMock(return_value=collector),
        database_factory=MagicMock(),
        target_logger=MagicMock(),
        credential_provider_factory=MagicMock(return_value=provider),
    )

    assert len(result) == 1
    assert events == ["api.read", "client.close"]
    client.close.assert_called_once_with()


def test_minute_chart_error_preserves_primary_when_client_close_also_fails(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = SimpleNamespace(
        OUTPUT_DIR_STR=str(tmp_path / "output"),
        validate_environment_settings=MagicMock(return_value=settings),
        activate_runtime_settings=MagicMock(return_value=settings),
    )
    provider = MagicMock()
    provider.load.return_value = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    primary_error = SystemExit(7)
    close_error = RuntimeError("local close failed")
    client = SimpleNamespace(
        market=object(),
        close=MagicMock(side_effect=close_error),
    )
    monkeypatch.setattr(
        minute_chart_module,
        "_read_traded_targets",
        MagicMock(side_effect=primary_error),
    )

    with pytest.raises(SystemExit) as caught:
        _extract_and_save_1min_chart(
            "2026-07-18",
            config_module=config_module,
            datetime_type=SimpleNamespace(
                now=lambda: datetime(2026, 7, 18, 12, 0)
            ),
            client_factory=MagicMock(return_value=client),
            collector_factory=MagicMock(),
            database_factory=MagicMock(),
            target_logger=MagicMock(),
            credential_provider_factory=MagicMock(return_value=provider),
        )

    assert caught.value is primary_error
    assert any(
        "Kiwoom client local close also failed with RuntimeError" in note
        for note in primary_error.__notes__
    )
    client.close.assert_called_once_with()


def test_create_trading_runtime_activates_prevalidated_settings_without_reloading(
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    ledger = RecordingLedger(events)
    physical_repository = RecordingPhysicalRepository(events)
    configure = MagicMock(side_effect=AssertionError("settings reloaded"))
    config_module = SimpleNamespace(
        CONFIG={},
        STRATEGY_CONFIG={"process_name": "test-process"},
        OUTPUT_DIR_STR="/tmp/kiwoom-test-output/20260718",
        validate_environment_settings=configure,
    )

    def activate_runtime_settings(candidate, today):
        assert candidate is settings
        events.append(("activate", today))
        return candidate

    client = MagicMock()
    client.ensure_auth_ready.side_effect = lambda: events.append("auth.ready")

    def client_factory(**kwargs):
        events.append(("client", kwargs["endpoint"]))
        return client

    def ledger_factory(db_path):
        events.append(("ledger", db_path))
        return ledger

    def physical_repository_factory(candidate):
        assert candidate is ledger
        events.append(("physical", candidate))
        return physical_repository

    def engine_factory(
        client,
        app_config,
        *,
        ledger: object,
        physical_state_repository: object,
    ):
        events.append(
            (
                "engine",
                client,
                app_config["process_name"],
                ledger,
                physical_state_repository,
            )
        )
        return "monitor"

    config_module.activate_runtime_settings = activate_runtime_settings

    runtime = _create_runtime(
        today=date(2026, 7, 18),
        config_module=config_module,
        client_factory=client_factory,
        engine_factory=engine_factory,
        ledger_factory=ledger_factory,
        physical_state_repository_factory=physical_repository_factory,
        prevalidated_settings=settings,
    )

    assert runtime.settings is settings
    configure.assert_not_called()
    assert events == [
        ("activate", date(2026, 7, 18)),
        ("ledger", settings.database.path),
        ("physical", ledger),
        ("client", settings.kiwoom.endpoint),
        "auth.ready",
        ("engine", client, "test-process", ledger, physical_repository),
    ]


def test_create_trading_runtime_does_not_build_graph_when_prevalidated_activation_fails(
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = SimpleNamespace(
        CONFIG={},
        STRATEGY_CONFIG={},
        OUTPUT_DIR_STR="",
        configure_from_environment=MagicMock(side_effect=AssertionError("settings reloaded")),
        activate_runtime_settings=MagicMock(side_effect=OSError("cannot activate output")),
    )
    client_factory = MagicMock(side_effect=AssertionError("client must not be created"))
    engine_factory = MagicMock(side_effect=AssertionError("engine must not be created"))
    ledger_factory = MagicMock(side_effect=AssertionError("ledger must not be created"))
    physical_factory = MagicMock(side_effect=AssertionError("physical adapter must not be created"))

    with pytest.raises(OSError, match="cannot activate output"):
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=client_factory,
            engine_factory=engine_factory,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
            prevalidated_settings=settings,
        )

    config_module.configure_from_environment.assert_not_called()
    config_module.activate_runtime_settings.assert_called_once_with(
        settings, today=date(2026, 7, 18)
    )
    ledger_factory.assert_not_called()
    physical_factory.assert_not_called()
    client_factory.assert_not_called()
    engine_factory.assert_not_called()


def test_runtime_ledger_construction_failure_stops_before_physical_client_and_engine(
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    primary_error = OSError("database unavailable")
    ledger_factory = MagicMock(side_effect=primary_error)
    physical_factory = MagicMock(side_effect=AssertionError("physical adapter created"))
    client_factory = MagicMock(side_effect=AssertionError("client created"))
    engine_factory = MagicMock(side_effect=AssertionError("engine created"))

    with pytest.raises(OSError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=client_factory,
            engine_factory=engine_factory,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    ledger_factory.assert_called_once_with(settings.database.path)
    physical_factory.assert_not_called()
    client_factory.assert_not_called()
    engine_factory.assert_not_called()


def test_runtime_physical_construction_failure_closes_ledger_before_client(
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    ledger = RecordingLedger(events)
    primary_error = RuntimeError("physical adapter unavailable")

    def physical_factory(candidate):
        assert candidate is ledger
        events.append("physical.construct")
        raise primary_error

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=MagicMock(side_effect=AssertionError("client created")),
            engine_factory=MagicMock(side_effect=AssertionError("engine created")),
            ledger_factory=MagicMock(return_value=ledger),
            physical_state_repository_factory=physical_factory,
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert events == ["physical.construct", "ledger.close"]


def test_runtime_client_failure_closes_physical_then_ledger(tmp_path):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    ledger = RecordingLedger(events)
    physical_repository = RecordingPhysicalRepository(events)
    primary_error = RuntimeError("client unavailable")

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=MagicMock(side_effect=primary_error),
            engine_factory=MagicMock(side_effect=AssertionError("engine created")),
            ledger_factory=MagicMock(return_value=ledger),
            physical_state_repository_factory=MagicMock(
                return_value=physical_repository
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert events == ["physical.close", "ledger.close"]


def test_runtime_auth_readiness_failure_closes_client_before_engine_and_resources(
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    ledger = RecordingLedger(events)
    physical_repository = RecordingPhysicalRepository(events)
    primary_error = RuntimeError("permanent auth rejection")
    client = MagicMock()
    client.ensure_auth_ready.side_effect = primary_error
    client.close.side_effect = lambda: events.append("client.close")
    engine_factory = MagicMock(side_effect=AssertionError("engine created"))

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=MagicMock(return_value=client),
            engine_factory=engine_factory,
            ledger_factory=MagicMock(return_value=ledger),
            physical_state_repository_factory=MagicMock(
                return_value=physical_repository
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    client.ensure_auth_ready.assert_called_once_with()
    engine_factory.assert_not_called()
    assert events == ["client.close", "physical.close", "ledger.close"]


def test_runtime_engine_failure_closes_client_before_local_resources(tmp_path):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    ledger = RecordingLedger(events)
    physical_repository = RecordingPhysicalRepository(events)
    client = SimpleNamespace(
        ensure_auth_ready=lambda: events.append("auth.ready"),
        close=lambda: events.append("client.close"),
    )
    primary_error = RuntimeError("engine unavailable")

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=MagicMock(return_value=client),
            engine_factory=MagicMock(side_effect=primary_error),
            ledger_factory=MagicMock(return_value=ledger),
            physical_state_repository_factory=MagicMock(
                return_value=physical_repository
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert events == [
        "auth.ready",
        "client.close",
        "physical.close",
        "ledger.close",
    ]


def test_runtime_engine_process_control_error_remains_primary_when_cleanup_raises(
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    ledger = RecordingLedger(events, close_error=SystemExit(8))
    physical_repository = RecordingPhysicalRepository(
        events,
        close_error=KeyboardInterrupt("cleanup interrupt"),
    )
    primary_error = KeyboardInterrupt("engine construction interrupted")

    with pytest.raises(KeyboardInterrupt) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=MagicMock(return_value=MagicMock()),
            engine_factory=MagicMock(side_effect=primary_error),
            ledger_factory=MagicMock(return_value=ledger),
            physical_state_repository_factory=MagicMock(
                return_value=physical_repository
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert events == ["physical.close", "ledger.close"]


@pytest.mark.parametrize("failure_stage", ["physical", "client", "engine"])
def test_runtime_real_ledger_worker_is_closed_at_every_later_failure_point(
    monkeypatch,
    tmp_path,
    failure_stage,
):
    monkeypatch.chdir(tmp_path)
    settings = _settings(tmp_path / f"{failure_stage}.sqlite3")
    config_module = _activated_config(settings)
    captured = {}
    primary_error = RuntimeError(f"{failure_stage} unavailable")

    def ledger_factory(db_path):
        ledger = TradeLogger(db_path)
        captured["ledger"] = ledger
        return ledger

    def physical_factory(ledger):
        if failure_stage == "physical":
            raise primary_error
        return AsyncPhysicalStateRepository(ledger)

    def client_factory(**kwargs):
        if failure_stage == "client":
            raise primary_error
        return MagicMock()

    def engine_factory(client, app_config, **kwargs):
        if failure_stage == "engine":
            raise primary_error
        raise AssertionError("engine must not be reached before its failure stage")

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=config_module,
            client_factory=client_factory,
            engine_factory=engine_factory,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    ledger = captured["ledger"]
    assert not ledger._worker_thread.is_alive()
    assert ledger._async_queue.unfinished_tasks == 0
    with pytest.raises(sqlite3.ProgrammingError):
        ledger.conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        ledger._worker_conn.execute("SELECT 1")
    assert not (tmp_path / "trades.db").exists()
