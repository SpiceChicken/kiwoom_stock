from datetime import date, datetime
from math import isnan
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.credentials import KiwoomClientCredentials, SensitiveText
from kiwoom_stock.application.ports import (
    MarketDataCollectionError,
    MarketDataFailureKind,
)
from kiwoom_stock.application.runtime import (
    RuntimeDisabledError,
    create_trading_runtime,
)
from kiwoom_stock.application import runtime as runtime_module
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.domain.strategy import (
    StrategySemanticsValidationError,
    TargetStopPolicy,
    calculate_position_return_percentage_points,
)
from kiwoom_stock.reporting.minute_chart import _extract_and_save_1min_chart
from kiwoom_stock.reporting import minute_chart as minute_chart_module
from kiwoom_stock.settings import (
    LegacyMappings,
    Settings,
    SettingsIssue,
    SettingsValidationError,
)


PAPER_FORCES = {
    "thrust": 0.1,
    "gravity": -0.2,
    "drag": -0.3,
    "magnetic": 0.4,
    "jerk": 0.5,
    "impulse": 0.6,
    "net_force": 0.7,
}


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


@pytest.mark.parametrize(
    ("exit_price", "expected_reason", "expected_return"),
    [
        (10_255.5, "Fixed Target", 2.555),
        (9_744.5, "Fixed Stop", -2.555),
    ],
)
def test_target_stop_settings_reach_paper_sqlite_without_order_capability(
    tmp_path,
    exit_price,
    expected_reason,
    expected_return,
):
    db_path = tmp_path / "phase3a-paper.sqlite3"
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "disabled",
            "KIWOOM_PROCESS_NAME": "phase3a-paper-test",
            "KIWOOM_DB_PATH": str(db_path),
            "KIWOOM_TARGET_STOP_UNIT_VERSION": "percentage-points-v1",
            "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS": "2.555",
            "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS": "2.555",
        }
    )
    system, strategy = settings.to_legacy_mappings()
    app_config = {**dict(system), **dict(strategy)}
    fixed_now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    ledger = TradeLogger(db_path, clock=lambda: fixed_now)
    buy_id = ledger.record_buy(
        {
            "stock_code": "005930",
            "stock_name": "Sample",
            "buy_price": 10_000.0,
            "buy_time": "2026-08-03 09:30:00",
            "buy_regime": "STABLE_BULL",
            **PAPER_FORCES,
            "owning_session_date": date(2026, 8, 3),
            "state_changed_at": fixed_now,
        }
    )
    repository = AsyncPhysicalStateRepository(ledger)
    paper_guard = MagicMock()
    notifier = MagicMock()
    engine = TradingEngine(
        SimpleNamespace(),
        app_config,
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=MagicMock(),
        notifier=notifier,
        paper_transition_guard=paper_guard,
        wall_clock=lambda: fixed_now,
        target_stop_policy=settings.strategy.target_stop_policy,
    )
    engine._execute_order = MagicMock(
        side_effect=AssertionError("broker/order path must remain unreachable")
    )

    try:
        assert engine.strategy.target_stop_policy is settings.strategy.target_stop_policy
        assert engine.strategy.target_profit_percentage_points == 2.555
        engine._process_decisions(
            [
                {
                    "stock_code": "005930",
                    "price": exit_price,
                    "atr_percent": 20.0,
                    "down_atr_percent": 1.0,
                    "forces": {"jerk": 0.0, "thrust": 1.0},
                    "is_buy_signal": False,
                    "regime": "STABLE_BULL",
                    "status": "holding",
                }
            ]
        )
        assert "005930" not in engine.stock_mgr.active_positions
        paper_guard.assert_called_once_with()
        engine._execute_order.assert_not_called()
        notifier.notify_sell.assert_called_once()
    finally:
        engine.close()

    with sqlite3.connect(db_path) as reopened:
        reopened.row_factory = sqlite3.Row
        row = reopened.execute(
            "SELECT status, sell_price, profit_rate, sell_reason FROM trades WHERE id = ?",
            (buy_id,),
        ).fetchone()
        count = reopened.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

    assert count == 1
    assert row["status"] == "CLOSED"
    assert row["sell_price"] == exit_price
    exact_return = calculate_position_return_percentage_points(10_000.0, exit_price)
    assert exact_return == expected_return
    assert row["profit_rate"] == exact_return
    assert row["profit_rate"] not in (2.55, -2.55)
    assert row["sell_reason"] == (
        f"{expected_reason} "
        f"({'-' if expected_return < 0 else ''}2.555 %p; percentage-points-v1)"
    )


def test_non_default_cumulative_score_floor_reaches_concrete_runtime_canonically(
    tmp_path,
):
    db_path = tmp_path / "phase3c-score-floor.sqlite3"
    settings = Settings.from_mapping(
        {
            "KIWOOM_API_MODE": "disabled",
            "KIWOOM_PROCESS_NAME": "phase3c-score-floor-test",
            "KIWOOM_DB_PATH": str(db_path),
            "KIWOOM_CUMULATIVE_TRADE_RETURN_SCORE_FLOOR": "-4.25",
        }
    )
    system, strategy = settings.to_legacy_mappings()
    app_config = {**dict(system), **dict(strategy)}
    runtime_strategy = app_config["strategy"]
    ledger = TradeLogger(db_path)
    repository = AsyncPhysicalStateRepository(ledger)
    engine = TradingEngine(
        SimpleNamespace(),
        app_config,
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=MagicMock(),
        notifier=MagicMock(),
        paper_transition_guard=MagicMock(),
    )

    try:
        assert runtime_strategy["cumulative_trade_return_score_floor"] == -4.25
        assert "total_loss_limit" not in runtime_strategy
        assert engine.strategy.cumulative_trade_return_score_floor == -4.25
    finally:
        engine.close()


_MISSING_POSITION_INPUT = object()


_INVALID_POSITION_UPDATE_CASES = [
    ("buy_price", _MISSING_POSITION_INPUT, "buy_price"),
    ("buy_price", None, "buy_price"),
    ("buy_price", True, "buy_price"),
    ("buy_price", float("nan"), "buy_price"),
    ("buy_price", float("inf"), "buy_price"),
    ("buy_price", 0.0, "buy_price"),
    ("buy_price", -1.0, "buy_price"),
    ("price", _MISSING_POSITION_INPUT, "current_price"),
    ("price", None, "current_price"),
    ("price", True, "current_price"),
    ("price", float("nan"), "current_price"),
    ("price", float("inf"), "current_price"),
    ("price", 0.0, "current_price"),
    ("price", -1.0, "current_price"),
    ("atr_percent", _MISSING_POSITION_INPUT, "atr_percent"),
    ("atr_percent", None, "atr_percent"),
    ("atr_percent", True, "atr_percent"),
    ("atr_percent", "20.0", "atr_percent"),
    ("atr_percent", float("nan"), "atr_percent"),
    ("atr_percent", float("inf"), "atr_percent"),
    ("atr_percent", -1.0, "atr_percent"),
    ("down_atr_percent", _MISSING_POSITION_INPUT, "down_atr_percent"),
    ("down_atr_percent", None, "down_atr_percent"),
    ("down_atr_percent", True, "down_atr_percent"),
    ("down_atr_percent", "1.0", "down_atr_percent"),
    ("down_atr_percent", float("nan"), "down_atr_percent"),
    ("down_atr_percent", float("inf"), "down_atr_percent"),
    ("down_atr_percent", -1.0, "down_atr_percent"),
]


def _assert_position_snapshot(position, expected):
    actual = vars(position)
    assert actual.keys() == expected.keys()
    for name, expected_value in expected.items():
        actual_value = actual[name]
        if isinstance(expected_value, float) and isnan(expected_value):
            assert actual_value is expected_value
        else:
            assert actual_value == expected_value


@pytest.mark.parametrize(
    ("field", "invalid_value", "error_field"),
    _INVALID_POSITION_UPDATE_CASES,
)
def test_invalid_position_candidate_cannot_mutate_memory_or_paper_sqlite(
    tmp_path,
    field,
    invalid_value,
    error_field,
):
    db_path = tmp_path / "phase3a-invalid-buy-price.sqlite3"
    fixed_now = datetime(2026, 8, 3, 15, 27, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    ledger = TradeLogger(db_path, clock=lambda: fixed_now)
    buy_id = ledger.record_buy(
        {
            "stock_code": "005930",
            "stock_name": "Sample",
            "buy_price": 10_000.0,
            "buy_time": "2026-08-03 09:30:00",
            "buy_regime": "STABLE_BULL",
            **PAPER_FORCES,
            "owning_session_date": date(2026, 8, 3),
            "state_changed_at": fixed_now,
        }
    )
    repository = AsyncPhysicalStateRepository(ledger)
    paper_guard = MagicMock()
    notifier = MagicMock()
    engine = TradingEngine(
        SimpleNamespace(),
        {"strategy": {"debug_mode": False}},
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=MagicMock(),
        notifier=notifier,
        paper_transition_guard=paper_guard,
        wall_clock=lambda: fixed_now,
    )
    position = engine.stock_mgr.active_positions["005930"]
    verdict = {
        "stock_code": "005930",
        "price": 10_300.0,
        "atr_percent": 20.0,
        "down_atr_percent": 1.0,
        "forces": {
            "current_velocity": 4.0,
            "thrust": 3.0,
            "magnetic": 1.0,
            "jerk": -2.0,
        },
        "is_buy_signal": False,
        "regime": "STABLE_BULL",
        "status": "holding",
    }
    if field == "buy_price":
        if invalid_value is _MISSING_POSITION_INPUT:
            del position.buy_price
        else:
            position.buy_price = invalid_value
    elif invalid_value is _MISSING_POSITION_INPUT:
        verdict.pop(field)
    else:
        verdict[field] = invalid_value
    position_before = vars(position).copy()
    engine.strategy.decide_position = MagicMock(
        wraps=engine.strategy.decide_position
    )
    engine.stock_mgr.apply_paper_sell = MagicMock(
        side_effect=AssertionError("manager paper transition must remain unreachable")
    )
    ledger.record_sell = MagicMock(
        side_effect=AssertionError("paper ledger write must remain unreachable")
    )

    try:
        with pytest.raises(StrategySemanticsValidationError, match=error_field):
            engine._process_decisions([verdict])

        _assert_position_snapshot(position, position_before)
        assert engine.strategy._kinetic_state == {}
        assert engine.stock_mgr.active_positions["005930"] is position
        engine.strategy.decide_position.assert_not_called()
        engine.stock_mgr.apply_paper_sell.assert_not_called()
        ledger.record_sell.assert_not_called()
        paper_guard.assert_not_called()
        notifier.collect_status.assert_not_called()
        notifier.notify_sell.assert_not_called()
    finally:
        engine.close()

    with sqlite3.connect(db_path) as reopened:
        reopened.row_factory = sqlite3.Row
        row = reopened.execute(
            "SELECT status, sell_price, profit_rate, sell_reason "
            "FROM trades WHERE id = ?",
            (buy_id,),
        ).fetchone()

    assert dict(row) == {
        "status": "OPEN",
        "sell_price": None,
        "profit_rate": None,
        "sell_reason": None,
    }


def test_valid_position_candidate_preserves_update_and_hold_path(tmp_path):
    db_path = tmp_path / "phase3a-valid-position-update.sqlite3"
    fixed_now = datetime(2026, 8, 3, 10, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    ledger = TradeLogger(db_path, clock=lambda: fixed_now)
    buy_id = ledger.record_buy(
        {
            "stock_code": "005930",
            "stock_name": "Sample",
            "buy_price": 10_000.0,
            "buy_time": "2026-08-03 09:30:00",
            "buy_regime": "STABLE_BULL",
            **PAPER_FORCES,
            "owning_session_date": date(2026, 8, 3),
            "state_changed_at": fixed_now,
        }
    )
    repository = AsyncPhysicalStateRepository(ledger)
    paper_guard = MagicMock()
    notifier = MagicMock()
    engine = TradingEngine(
        SimpleNamespace(),
        {"strategy": {"debug_mode": False}},
        ledger=ledger,
        physical_state_repository=repository,
        market_gateway=MagicMock(),
        notifier=notifier,
        paper_transition_guard=paper_guard,
        wall_clock=lambda: fixed_now,
    )
    position = engine.stock_mgr.active_positions["005930"]
    before = vars(position).copy()
    verdict = {
        "stock_code": "005930",
        "price": 10_050.0,
        "atr_percent": 20.0,
        "down_atr_percent": 1.0,
        "forces": {"jerk": 0.0, "thrust": 1.0},
        "is_buy_signal": False,
        "regime": "STABLE_BULL",
        "status": "holding",
    }

    try:
        engine._process_decisions([verdict])

        assert position.sell_price == 10_050.0
        assert position.atr_percent == 20.0
        assert position.down_atr_percent == 1.0
        for name, value in before.items():
            if name not in {"sell_price", "atr_percent", "down_atr_percent"}:
                assert getattr(position, name) == value
        assert engine.strategy._kinetic_state["005930"] == {
            "buy_price": 10_000.0,
            "max_price": 10_050.0,
        }
        assert "005930" in engine.stock_mgr.active_positions
        paper_guard.assert_not_called()
        notifier.collect_status.assert_called_once()
        notifier.notify_sell.assert_not_called()
    finally:
        engine.close()

    with sqlite3.connect(db_path) as reopened:
        reopened.row_factory = sqlite3.Row
        row = reopened.execute(
            "SELECT status, sell_price, profit_rate, sell_reason "
            "FROM trades WHERE id = ?",
            (buy_id,),
        ).fetchone()

    assert dict(row) == {
        "status": "OPEN",
        "sell_price": None,
        "profit_rate": None,
        "sell_reason": None,
    }


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

    def ledger_factory(db_path, clock):
        events.append(("ledger", db_path, clock))
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
        market_gateway: object,
        target_stop_policy: TargetStopPolicy,
        wall_clock,
    ):
        events.append(
            (
                "engine",
                client,
                app_config["process_name"],
                ledger,
                physical_state_repository,
                market_gateway,
                target_stop_policy,
                wall_clock,
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
    assert not hasattr(runtime, "client")
    assert not hasattr(runtime, "account")
    assert runtime.monitor == "monitor"
    assert "app_config" not in repr(runtime)
    provider_factory.assert_called_once_with(settings.kiwoom.credentials_dir)
    provider.load.assert_called_once_with()
    assert events[3][1]["credentials"] is credentials
    assert events == [
        "settings",
        ("ledger", settings.database.path, ANY),
        ("physical", ledger),
        (
            "client",
            {
                "credentials": ANY,
                "endpoint": settings.kiwoom.endpoint,
            },
        ),
        "auth.ready",
        (
            "engine",
            client,
            "test-process",
            ledger,
            physical_repository,
            ANY,
            settings.strategy.target_stop_policy,
            ANY,
        ),
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


def test_canonical_policy_with_orphan_legacy_fails_before_runtime_side_effects():
    with pytest.raises(SettingsValidationError) as caught:
        Settings.from_mapping(
            {
                "KIWOOM_API_MODE": "disabled",
                "KIWOOM_PROCESS_NAME": "invalid-policy",
                "KIWOOM_TARGET_STOP_UNIT_VERSION": "percentage-points-v1",
                "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS": "4.0",
                "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS": "2.0",
            },
            legacy=LegacyMappings.from_mappings(
                strategy_config={"target_profit_rate": 0.03}
            ),
        )

    config_module = SimpleNamespace(
        CONFIG={},
        STRATEGY_CONFIG={},
        OUTPUT_DIR_STR="",
        validate_environment_settings=MagicMock(side_effect=caught.value),
    )
    ledger_factory = MagicMock()
    physical_factory = MagicMock()
    client_factory = MagicMock()
    engine_factory = MagicMock()

    with pytest.raises(SettingsValidationError):
        _create_runtime(
            today=date(2026, 8, 9),
            config_module=config_module,
            ledger_factory=ledger_factory,
            physical_state_repository_factory=physical_factory,
            client_factory=client_factory,
            engine_factory=engine_factory,
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
    client = SimpleNamespace(
        market=object(),
        ensure_auth_ready=MagicMock(),
        close=MagicMock(),
    )
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
        ensure_auth_ready=MagicMock(),
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
        ensure_auth_ready=MagicMock(),
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

    def ledger_factory(db_path, clock):
        events.append(("ledger", db_path, clock))
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
        market_gateway: object,
        target_stop_policy: TargetStopPolicy,
        wall_clock,
    ):
        events.append(
            (
                "engine",
                client,
                app_config["process_name"],
                ledger,
                physical_state_repository,
                market_gateway,
                target_stop_policy,
                wall_clock,
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
        ("ledger", settings.database.path, ANY),
        ("physical", ledger),
        ("client", settings.kiwoom.endpoint),
        "auth.ready",
        (
            "engine",
            client,
            "test-process",
            ledger,
            physical_repository,
            ANY,
            settings.strategy.target_stop_policy,
            ANY,
        ),
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
    ledger_factory.assert_called_once_with(settings.database.path, ANY)
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

    with pytest.raises(MarketDataCollectionError) as caught:
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

    assert caught.value.kind is MarketDataFailureKind.FETCH
    assert caught.value.operation == "auth_preflight"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "permanent auth rejection" not in str(caught.value)
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
        market=object(),
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


def test_runtime_construction_rollback_installs_one_deadline_before_close_order(
    monkeypatch,
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    monkeypatch.setattr(runtime_module, "NORMAL_SHUTDOWN_TIMEOUT_SECONDS", 0.2)

    class DeadlineRecordingLedger(RecordingLedger):
        def __init__(self):
            super().__init__(events)
            self.deadlines = []

        def set_shutdown_deadline(self, deadline_remaining):
            events.append("ledger.deadline")
            self.deadlines.append(deadline_remaining)

    ledger = DeadlineRecordingLedger()
    physical_repository = RecordingPhysicalRepository(events)
    client = SimpleNamespace(
        ensure_auth_ready=lambda: events.append("auth.ready"),
        market=object(),
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
    assert len(ledger.deadlines) == 1
    assert 0.0 <= ledger.deadlines[0]() <= 0.2
    assert events == [
        "auth.ready",
        "ledger.deadline",
        "client.close",
        "physical.close",
        "ledger.close",
    ]


def test_runtime_construction_rollback_stalled_deadline_setter_preserves_traceback(
    monkeypatch,
    caplog,
    tmp_path,
):
    settings = _settings(tmp_path / "setter-stall.sqlite3")
    config_module = _activated_config(settings)
    monkeypatch.setattr(runtime_module, "NORMAL_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    setter_entered = threading.Event()
    release_setter = threading.Event()
    cleanup_complete = threading.Event()
    events = []
    deadlines = []

    class StalledSetterLedger(RecordingLedger):
        def set_shutdown_deadline(self, deadline_remaining):
            deadlines.append(deadline_remaining)
            events.append("ledger.deadline")
            setter_entered.set()
            release_setter.wait()

        def close(self):
            super().close()
            cleanup_complete.set()

    ledger = StalledSetterLedger(events)
    client = SimpleNamespace(
        ensure_auth_ready=lambda: None,
        market=object(),
        close=lambda: events.append("client.close"),
    )
    context_error = ValueError("construction context")
    primary_error = RuntimeError("engine unavailable")
    original = {}

    def failing_engine(*args, **kwargs):
        try:
            raise context_error
        except ValueError:
            try:
                raise primary_error
            except RuntimeError as error:
                original["traceback"] = error.__traceback__
                original["context"] = error.__context__
                raise

    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            _create_runtime(
                today=date(2026, 7, 18),
                config_module=config_module,
                client_factory=MagicMock(return_value=client),
                engine_factory=failing_engine,
                ledger_factory=MagicMock(return_value=ledger),
                physical_state_repository_factory=MagicMock(
                    return_value=RecordingPhysicalRepository(events)
                ),
                prevalidated_settings=settings,
            )
        elapsed = time.monotonic() - started_at
        assert caught.value is primary_error
        assert caught.value.__context__ is original["context"] is context_error
        traceback_tail = caught.value.__traceback__
        while traceback_tail.tb_next is not None:
            traceback_tail = traceback_tail.tb_next
        assert traceback_tail is original["traceback"]
        assert setter_entered.is_set()
        assert elapsed < 0.5
        assert len(deadlines) == 1
        assert deadlines[0]() == 0.0
        assert events == ["ledger.deadline"]
        assert "deadline exceeded (phase=paper ledger deadline installation)" in caplog.text
    finally:
        release_setter.set()

    assert cleanup_complete.wait(timeout=1.0)
    assert events == [
        "ledger.deadline",
        "client.close",
        "physical.close",
        "ledger.close",
    ]


def test_runtime_construction_rollback_deadline_setter_error_continues_cleanup(
    caplog,
    tmp_path,
):
    events = []
    settings = _settings(tmp_path / "setter-error.sqlite3")

    class FailingSetterLedger(RecordingLedger):
        def set_shutdown_deadline(self, deadline_remaining):
            events.append("ledger.deadline")
            raise SystemExit("sensitive setter detail")

    primary_error = RuntimeError("engine unavailable")
    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=_activated_config(settings),
            client_factory=MagicMock(
                return_value=SimpleNamespace(
                    ensure_auth_ready=lambda: None,
                    market=object(),
                    close=lambda: events.append("client.close"),
                )
            ),
            engine_factory=MagicMock(side_effect=primary_error),
            ledger_factory=MagicMock(return_value=FailingSetterLedger(events)),
            physical_state_repository_factory=MagicMock(
                return_value=RecordingPhysicalRepository(events)
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert events == [
        "ledger.deadline",
        "client.close",
        "physical.close",
        "ledger.close",
    ]
    assert "deadline installation failed for paper ledger (type=SystemExit)" in caplog.text
    assert "sensitive setter detail" not in caplog.text


def test_runtime_engine_process_control_error_remains_primary_when_cleanup_raises(
    caplog,
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
    client = SimpleNamespace(
        ensure_auth_ready=lambda: None,
        market=object(),
        close=lambda: (
            events.append("client.close"),
            (_ for _ in ()).throw(GeneratorExit("secret cleanup detail")),
        ),
    )

    with pytest.raises(KeyboardInterrupt) as caught:
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
    assert events == ["client.close", "physical.close", "ledger.close"]
    assert "GeneratorExit" in caplog.text
    assert "KeyboardInterrupt" in caplog.text
    assert "SystemExit" in caplog.text
    assert "secret cleanup detail" not in caplog.text


def test_runtime_construction_rollback_stuck_close_is_bounded_and_releasable(
    monkeypatch,
    caplog,
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    config_module = _activated_config(settings)
    monkeypatch.setattr(runtime_module, "NORMAL_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    release = threading.Event()
    client_close_entered = threading.Event()
    downstream_closed = threading.Event()
    events = []

    class Ledger(RecordingLedger):
        def set_shutdown_deadline(self, deadline_remaining):
            self.deadline_remaining = deadline_remaining

        def close(self):
            super().close()
            downstream_closed.set()

    ledger = Ledger(events)

    def stuck_client_close():
        events.append("client.close")
        client_close_entered.set()
        release.wait()

    client = SimpleNamespace(
        ensure_auth_ready=lambda: None,
        market=object(),
        close=stuck_client_close,
    )
    primary_error = RuntimeError("engine unavailable")
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            _create_runtime(
                today=date(2026, 7, 18),
                config_module=config_module,
                client_factory=MagicMock(return_value=client),
                engine_factory=MagicMock(side_effect=primary_error),
                ledger_factory=MagicMock(return_value=ledger),
                physical_state_repository_factory=MagicMock(
                    return_value=RecordingPhysicalRepository(events)
                ),
                prevalidated_settings=settings,
            )
        elapsed = time.monotonic() - started_at
        assert caught.value is primary_error
        assert client_close_entered.is_set()
        assert elapsed < 0.5
        assert events == ["client.close"]
        assert "rollback deadline exceeded (phase=Kiwoom client)" in caplog.text
    finally:
        release.set()

    assert downstream_closed.wait(timeout=1.0)
    assert events == ["client.close", "physical.close", "ledger.close"]


def test_runtime_construction_rollback_thread_start_failure_keeps_primary(
    monkeypatch,
    caplog,
    tmp_path,
):
    settings = _settings(tmp_path / "paper.sqlite3")
    primary_error = RuntimeError("engine unavailable")

    def fail_start(self):
        raise SystemExit(23)

    monkeypatch.setattr(runtime_module.threading.Thread, "start", fail_start)

    with pytest.raises(RuntimeError) as caught:
        _create_runtime(
            today=date(2026, 7, 18),
            config_module=_activated_config(settings),
            client_factory=MagicMock(return_value=MagicMock()),
            engine_factory=MagicMock(side_effect=primary_error),
            ledger_factory=MagicMock(return_value=RecordingLedger([])),
            physical_state_repository_factory=MagicMock(
                return_value=RecordingPhysicalRepository([])
            ),
            prevalidated_settings=settings,
        )

    assert caught.value is primary_error
    assert "coordination failed (phase=coordinator start, type=SystemExit)" in caplog.text


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

    def ledger_factory(db_path, clock):
        ledger = TradeLogger(db_path, clock=clock)
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


def test_runtime_real_ledger_stuck_worker_obeys_factory_deadline_and_remains_daemon(
    monkeypatch,
    tmp_path,
):
    settings = _settings(tmp_path / "stuck-worker.sqlite3")
    config_module = _activated_config(settings)
    monkeypatch.setattr(runtime_module, "NORMAL_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    execute_entered = threading.Event()
    release_execute = threading.Event()
    captured = {}
    deadline_callbacks = []

    class BlockingExecuteProxy:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, *args, **kwargs):
            execute_entered.set()
            release_execute.wait()
            return self.connection.execute(*args, **kwargs)

    def ledger_factory(db_path, clock):
        ledger = TradeLogger(db_path, clock=clock)
        original_set_deadline = ledger.set_shutdown_deadline

        def record_deadline(deadline_remaining):
            deadline_callbacks.append(deadline_remaining)
            original_set_deadline(deadline_remaining)

        ledger.set_shutdown_deadline = record_deadline
        ledger._worker_conn = BlockingExecuteProxy(ledger._worker_conn)
        ledger.submit_physical_state(
            "005930",
            {"current_velocity": 1.0, **PAPER_FORCES},
        )
        assert execute_entered.wait(timeout=1.0)
        captured["ledger"] = ledger
        return ledger

    primary_error = RuntimeError("engine unavailable")
    started_at = time.monotonic()
    try:
        with pytest.raises(RuntimeError) as caught:
            _create_runtime(
                today=date(2026, 7, 18),
                config_module=config_module,
                client_factory=MagicMock(return_value=MagicMock()),
                engine_factory=MagicMock(side_effect=primary_error),
                ledger_factory=ledger_factory,
                prevalidated_settings=settings,
            )
        elapsed = time.monotonic() - started_at
        ledger = captured["ledger"]
        assert caught.value is primary_error
        assert elapsed < 0.5
        assert len(deadline_callbacks) == 1
        assert ledger._shutdown_deadline is deadline_callbacks[0]
        assert deadline_callbacks[0]() == 0.0
        assert ledger._worker_thread.daemon is True
        assert ledger._worker_thread.is_alive()
        assert ledger.is_closed is False
    finally:
        release_execute.set()

    ledger._worker_thread.join(timeout=2.0)
    assert not ledger._worker_thread.is_alive()
    assert ledger._async_queue.unfinished_tasks == 0
