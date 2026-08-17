"""Paper ledger DDL and additive migration boundary."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable, Dict, List, Tuple

from kiwoom_stock.application.ports import PaperTradePersistenceError


ActiveRowValidator = Callable[
    [], Tuple[List[Dict[str, Any]], List[Tuple[str, str, int, str]]]
]
LifecycleShapeVerifier = Callable[[], None]


def initialize_paper_schema(
    connection: sqlite3.Connection,
    *,
    validate_active_rows: ActiveRowValidator,
    verify_lifecycle_shape: LifecycleShapeVerifier,
) -> None:
    """Atomically initialize schema and perform strict legacy backfills."""

    query_trades = """
    CREATE TABLE IF NOT EXISTS trades (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stock_code TEXT,
        stock_name TEXT,
        buy_price REAL,
        thrust REAL,
        gravity REAL,
        drag REAL,
        magnetic REAL,
        jerk REAL,
        impulse REAL,
        net_force REAL,
        buy_time TEXT,
        buy_regime TEXT,
        sell_price REAL,
        profit_rate REAL,
        sell_time TEXT,
        sell_reason TEXT,
        status TEXT DEFAULT 'OPEN',
        owning_session_date TEXT,
        state_changed_at TEXT
    )
    """
    query_physics = """
    CREATE TABLE IF NOT EXISTS physics_state (
        stock_code TEXT PRIMARY KEY,
        velocity REAL,
        thrust REAL,
        gravity REAL,
        drag REAL,
        magnetic REAL,
        jerk REAL,
        impulse REAL,
        net_force REAL,
        last_updated TEXT
    )
    """
    query_tracker_v1 = """
    CREATE TABLE IF NOT EXISTS physical_tracker_state_v1 (
        stock_code TEXT PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        velocity REAL NOT NULL,
        last_cumulative_volume REAL NOT NULL,
        last_price REAL NOT NULL,
        interval_volume_history TEXT NOT NULL,
        last_observed_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        projection_generation TEXT NOT NULL,
        projection_velocity REAL NOT NULL,
        projection_thrust REAL NOT NULL,
        projection_gravity REAL NOT NULL,
        projection_drag REAL NOT NULL,
        projection_magnetic REAL NOT NULL,
        projection_jerk REAL NOT NULL,
        projection_impulse REAL NOT NULL,
        projection_net_force REAL NOT NULL,
        projection_last_updated TEXT NOT NULL
    )
    """
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(query_trades)
        connection.execute(query_physics)
        connection.execute(query_tracker_v1)
        trade_columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_info(trades)")
        }
        for column in ("owning_session_date", "state_changed_at"):
            existing = trade_columns.get(column)
            if existing is not None and existing[2] != "TEXT":
                raise PaperTradePersistenceError(
                    f"trades.{column} must have TEXT affinity"
                )
        for column in ("owning_session_date", "state_changed_at"):
            if column not in trade_columns:
                connection.execute(f"ALTER TABLE trades ADD COLUMN {column} TEXT")
        existing_tracker_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(physical_tracker_state_v1)"
            )
        }
        for column in (
            "projection_thrust",
            "projection_gravity",
            "projection_drag",
            "projection_magnetic",
            "projection_jerk",
            "projection_impulse",
            "projection_net_force",
        ):
            if column not in existing_tracker_columns:
                connection.execute(
                    f"ALTER TABLE physical_tracker_state_v1 ADD COLUMN {column} REAL"
                )

        _, backfills = validate_active_rows()
        if backfills:
            cursor = connection.executemany(
                """
                UPDATE trades
                SET owning_session_date = ?, state_changed_at = ?
                WHERE id = ? AND stock_code = ? AND status = 'OPEN'
                  AND owning_session_date IS NULL
                  AND state_changed_at IS NULL
                """,
                backfills,
            )
            if cursor.rowcount != len(backfills):
                raise PaperTradePersistenceError(
                    "legacy OPEN metadata backfill identity mismatch"
                )
        verify_lifecycle_shape()
        _, pending_backfills = validate_active_rows()
        if pending_backfills:
            raise PaperTradePersistenceError(
                "legacy OPEN metadata backfill verification failed"
            )
        connection.commit()
    except Exception as error:
        connection.rollback()
        if isinstance(error, PaperTradePersistenceError):
            raise
        raise PaperTradePersistenceError("paper ledger initialization failed") from error
