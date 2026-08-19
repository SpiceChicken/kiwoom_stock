from datetime import datetime, timezone
import sqlite3
from datetime import date, timedelta

import pytest

from kiwoom_stock.settings import Settings
from kiwoom_stock.infrastructure.shadow_telemetry import (
    ShadowCycleTelemetry, ShadowTelemetryReader, ShadowTelemetryStore,
    shadow_config_payload, shadow_config_sha256,
)


def row(**updates):
    value = dict(
        activation_id="activation-1", session_date_kst="2026-08-19", cycle_index=1,
        observed_at=datetime(2026, 8, 19, 1, 0, tzinfo=timezone.utc),
        stock_code="005930", proxy_code="069500", source_sha="a" * 40,
        image_digest="sha256:" + "b" * 64, config_sha256="c" * 64,
        strategy_slot="baseline", current_price=100.0, vwap=99.0, strength=101.0,
        trend_rsi=55.0, atr_percent=0.5, down_atr_percent=0.2, volume_ratio=1.2,
        forces={name: 0.0 for name in ("thrust", "gravity", "drag", "magnetic", "jerk", "impulse", "net_force", "current_velocity", "volume_drop_ratio")},
        decision={name: "SAFE" for name in ("market_regime", "strategy_reason_code", "strategy_intent", "paper_action", "position_before", "trading_window", "session_phase", "net_force_band", "current_velocity_band", "jerk_band", "strength_band", "trend_rsi_band", "thrust_band", "price_vwap_relation")},
        position_after="FLAT",
        continuity={"schema_version": 1, "hydration_source": "initial", "previous_observed_at": None, "history_depth": 0, "baseline_source": "row_4_fixed_cadence", "baseline_sample_index": 4, "baseline_time_estimated": True},
    )
    value.update(updates)
    return ShadowCycleTelemetry(**value)


def test_store_is_idempotent_and_fails_closed_on_conflict(tmp_path):
    store = ShadowTelemetryStore(tmp_path / "shadow-telemetry.db")
    first = row()
    assert store.append(first) == first.hash()
    assert store.append(first) == first.hash()
    with pytest.raises(ValueError, match="conflicting"):
        store.append(row(current_price=101.0))
    assert len(store.rows("activation-1", "2026-08-19")) == 1


def test_export_is_deterministic_and_bounded(tmp_path):
    store = ShadowTelemetryStore(tmp_path / "shadow-telemetry.db")
    store.append(row())
    artifact, manifest = store.export("activation-1", "2026-08-19")
    assert manifest["row_count"] == 1
    assert manifest["compressed_sha256"]
    assert artifact == store.export("activation-1", "2026-08-19")[0]


def test_store_does_not_touch_trade_database(tmp_path):
    trade = tmp_path / "shadow-trades.db"
    connection = sqlite3.connect(trade)
    connection.execute("create table marker(value text)")
    connection.execute("insert into marker values ('unchanged')")
    connection.commit()
    ShadowTelemetryStore(tmp_path / "shadow-telemetry.db").append(row())
    assert connection.execute("select * from marker").fetchone() == ("unchanged",)


def test_reader_uses_sqlite_read_only_uri_and_exports_writer_database(tmp_path):
    database = tmp_path / "shadow-telemetry.db"
    writer = ShadowTelemetryStore(database)
    writer.append(row())
    writer.close()

    reader = ShadowTelemetryReader(database)
    artifact, manifest = reader.export("activation-1", "2026-08-19")
    assert artifact and manifest["row_count"] == 1
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        reader.connection.execute("CREATE TABLE should_not_exist(value TEXT)")
    reader.close()


def test_reader_fails_closed_for_missing_or_malformed_database(tmp_path):
    with pytest.raises(ValueError, match="missing"):
        ShadowTelemetryReader(tmp_path / "missing.db")
    malformed = tmp_path / "malformed.db"
    connection = sqlite3.connect(malformed)
    connection.execute("create table unrelated(value text)")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="schema"):
        ShadowTelemetryReader(malformed)


def test_shadow_config_hash_is_allowlisted_and_stable():
    settings = Settings.from_mapping({"KIWOOM_PROCESS_NAME": "telemetry-test"})
    same = Settings.from_mapping({"KIWOOM_PROCESS_NAME": "telemetry-test"})
    assert shadow_config_sha256(settings) == shadow_config_sha256(same)
    serialized = str(shadow_config_payload(settings))
    assert "credentials" not in serialized.lower()
    assert "webhook" not in serialized.lower()
    assert "token" not in serialized.lower()
    assert "/" not in serialized


def test_shadow_config_hash_changes_when_strategy_threshold_changes():
    base = {
        "KIWOOM_PROCESS_NAME": "telemetry-test",
        "KIWOOM_TARGET_STOP_UNIT_VERSION": "percentage-points-v1",
        "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS": "2.0",
        "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS": "1.0",
    }
    changed = {**base, "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS": "2.5"}
    assert shadow_config_sha256(Settings.from_mapping(base)) != shadow_config_sha256(
        Settings.from_mapping(changed)
    )


def test_finalized_session_retention_keeps_only_twenty_sessions(tmp_path):
    store = ShadowTelemetryStore(tmp_path / "shadow-telemetry.db")
    first_date = date(2026, 1, 1)
    for index in range(21):
        session = (first_date + timedelta(days=index)).isoformat()
        activation = f"activation-{index + 1}"
        store.append(row(activation_id=activation, session_date_kst=session))
        store.finalize_session(activation, session)
    count = store.connection.execute(
        "select count(*) from shadow_telemetry_sessions_v1"
    ).fetchone()[0]
    assert count == 20
    assert not store.rows("activation-1", "2026-01-01")


def test_export_rejects_canonical_row_hash_tampering(tmp_path):
    database = tmp_path / "shadow-telemetry.db"
    store = ShadowTelemetryStore(database)
    store.append(row())
    store.connection.execute(
        "update shadow_cycle_telemetry_v1 set row_sha256=?", ("d" * 64,)
    )
    store.connection.commit()
    with pytest.raises(ValueError, match="canonical row hash"):
        store.export("activation-1", "2026-08-19")


def test_database_high_water_bound_fails_closed(tmp_path, monkeypatch):
    import kiwoom_stock.infrastructure.shadow_telemetry as telemetry

    monkeypatch.setattr(telemetry, "MAX_DATABASE_BYTES", 1)
    with pytest.raises(ValueError, match="32 MiB bound"):
        telemetry.ShadowTelemetryStore(tmp_path / "shadow-telemetry.db")
