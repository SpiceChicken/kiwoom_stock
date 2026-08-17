"""Canonical, candidate-only schema and fail-closed migration for P3."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

from kiwoom_stock.application.ports import SwingPersistenceError, SwingSchemaIncompatibleError

SWING_SCHEMA_VERSION = "swing-p4-episode-v1"
GENESIS_HASH = "0" * 64
_TABLES = (
    "swing_schema_meta_v1", "swing_portfolios_v1", "swing_position_identities_v1",
    "swing_commands_v1", "swing_fills_v1", "swing_lifecycle_events_v1",
    "swing_daily_marks_v1", "swing_position_snapshots_v1", "swing_portfolio_snapshots_v1",
    "swing_episode_events_v1", "swing_episode_snapshots_v1",
)


def _ddl() -> tuple[str, ...]:
    return (
        "CREATE TABLE IF NOT EXISTS swing_schema_meta_v1 (schema_name TEXT PRIMARY KEY, schema_version TEXT NOT NULL, shape_hash TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS swing_portfolios_v1 (portfolio_id TEXT PRIMARY KEY CHECK(length(trim(portfolio_id)) > 0), initial_cash_krw INTEGER NOT NULL CHECK(initial_cash_krw >= 0), policy_version TEXT NOT NULL CHECK(length(trim(policy_version)) > 0), policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64), created_at TEXT NOT NULL, row_hash TEXT NOT NULL CHECK(length(row_hash)=64))",
        "CREATE TABLE IF NOT EXISTS swing_position_identities_v1 (portfolio_id TEXT NOT NULL, position_id TEXT NOT NULL CHECK(length(trim(position_id)) > 0), symbol TEXT NOT NULL CHECK(length(trim(symbol)) > 0), created_at TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), row_hash TEXT NOT NULL CHECK(length(row_hash)=64), PRIMARY KEY(portfolio_id, position_id, symbol), UNIQUE(portfolio_id, position_id), FOREIGN KEY(portfolio_id) REFERENCES swing_portfolios_v1(portfolio_id))",
        "CREATE TABLE IF NOT EXISTS swing_commands_v1 (portfolio_id TEXT NOT NULL, idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) > 0), command_kind TEXT NOT NULL CHECK(command_kind IN ('REGISTER_PORTFOLIO','APPEND_FILL','APPEND_MARK','APPEND_CORPORATE_ACTION')), payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64), expected_portfolio_sequence INTEGER NOT NULL CHECK(expected_portfolio_sequence >= 0), expected_position_sequence INTEGER NOT NULL CHECK(expected_position_sequence >= 0), expected_mark_revision INTEGER NOT NULL CHECK(expected_mark_revision >= 0), committed_portfolio_sequence INTEGER NOT NULL CHECK(committed_portfolio_sequence >= 0), committed_position_sequence INTEGER, committed_mark_revision INTEGER, committed_event_sequence INTEGER NOT NULL CHECK(committed_event_sequence >= 0), created_at TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), row_hash TEXT NOT NULL CHECK(length(row_hash)=64), PRIMARY KEY(portfolio_id,idempotency_key), FOREIGN KEY(portfolio_id) REFERENCES swing_portfolios_v1(portfolio_id))",
        "CREATE TABLE IF NOT EXISTS swing_fills_v1 (fill_id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, position_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), quantity INTEGER NOT NULL CHECK(quantity > 0), raw_price_krw INTEGER NOT NULL CHECK(raw_price_krw > 0), decision_at TEXT NOT NULL, fill_at TEXT NOT NULL, cost_scenario TEXT NOT NULL, policy_hash TEXT NOT NULL CHECK(length(policy_hash)=64), gross_commission_krw INTEGER NOT NULL CHECK(gross_commission_krw >= 0), gross_tax_krw INTEGER NOT NULL CHECK(gross_tax_krw >= 0), gross_slippage_krw INTEGER NOT NULL CHECK(gross_slippage_krw >= 0), base_commission_krw INTEGER NOT NULL CHECK(base_commission_krw >= 0), base_tax_krw INTEGER NOT NULL CHECK(base_tax_krw >= 0), base_slippage_krw INTEGER NOT NULL CHECK(base_slippage_krw >= 0), stress_commission_krw INTEGER NOT NULL CHECK(stress_commission_krw >= 0), stress_tax_krw INTEGER NOT NULL CHECK(stress_tax_krw >= 0), stress_slippage_krw INTEGER NOT NULL CHECK(stress_slippage_krw >= 0), gross_cash_delta_krw INTEGER NOT NULL, net_cash_delta_krw INTEGER NOT NULL, command_hash TEXT NOT NULL CHECK(length(command_hash)=64), previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), row_hash TEXT NOT NULL CHECK(length(row_hash)=64), FOREIGN KEY(portfolio_id,position_id,symbol) REFERENCES swing_position_identities_v1(portfolio_id,position_id,symbol))",
        "CREATE TABLE IF NOT EXISTS swing_lifecycle_events_v1 (portfolio_id TEXT NOT NULL, event_sequence INTEGER NOT NULL CHECK(event_sequence > 0), event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL CHECK(event_type IN ('FILL','MARK','CORPORATE_ACTION')), position_id TEXT NOT NULL, symbol TEXT NOT NULL, payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), event_hash TEXT NOT NULL CHECK(length(event_hash)=64), PRIMARY KEY(portfolio_id,event_sequence), FOREIGN KEY(portfolio_id,position_id,symbol) REFERENCES swing_position_identities_v1(portfolio_id,position_id,symbol))",
        "CREATE TABLE IF NOT EXISTS swing_daily_marks_v1 (portfolio_id TEXT NOT NULL, position_id TEXT NOT NULL, symbol TEXT NOT NULL, session_date TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0), mark_id TEXT NOT NULL UNIQUE, price_krw INTEGER CHECK(price_krw IS NULL OR price_krw > 0), quality TEXT NOT NULL CHECK(quality IN ('OFFICIAL_CLOSE','PROVISIONAL_LAST_VALID_REGULAR','SUSPENDED_CARRY_FORWARD','MISSING')), source_id TEXT NOT NULL CHECK(length(trim(source_id)) > 0), available_at TEXT NOT NULL, computed_at TEXT NOT NULL, supersedes_id TEXT, payload_json TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), row_hash TEXT NOT NULL CHECK(length(row_hash)=64), PRIMARY KEY(portfolio_id,position_id,symbol,session_date,revision), FOREIGN KEY(portfolio_id,position_id,symbol) REFERENCES swing_position_identities_v1(portfolio_id,position_id,symbol))",
        "CREATE TABLE IF NOT EXISTS swing_position_snapshots_v1 (portfolio_id TEXT NOT NULL, position_id TEXT NOT NULL, symbol TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0), quantity INTEGER NOT NULL CHECK((status='CLOSED' AND quantity=0) OR (status='OPEN' AND quantity>0)), status TEXT NOT NULL CHECK(status IN ('OPEN','CLOSED')), cost_basis_krw INTEGER NOT NULL CHECK(cost_basis_krw >= 0), latest_mark_id TEXT, state_json TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64), PRIMARY KEY(portfolio_id,position_id,symbol,sequence), FOREIGN KEY(portfolio_id,position_id,symbol) REFERENCES swing_position_identities_v1(portfolio_id,position_id,symbol))",
        "CREATE TABLE IF NOT EXISTS swing_portfolio_snapshots_v1 (portfolio_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0), cash_krw INTEGER NOT NULL CHECK(cash_krw >= 0), market_value_krw INTEGER NOT NULL CHECK(market_value_krw >= 0), equity_krw INTEGER NOT NULL CHECK(equity_krw = cash_krw + market_value_krw + receivables_krw - liabilities_krw), receivables_krw INTEGER NOT NULL CHECK(receivables_krw >= 0), liabilities_krw INTEGER NOT NULL CHECK(liabilities_krw >= 0), completeness TEXT NOT NULL CHECK(completeness IN ('COMPLETE','INCOMPLETE')), gate TEXT, state_json TEXT NOT NULL, snapshot_json TEXT NOT NULL, previous_hash TEXT NOT NULL CHECK(length(previous_hash)=64), snapshot_hash TEXT NOT NULL CHECK(length(snapshot_hash)=64), PRIMARY KEY(portfolio_id,sequence), FOREIGN KEY(portfolio_id) REFERENCES swing_portfolios_v1(portfolio_id))",
        "CREATE TABLE IF NOT EXISTS swing_episode_events_v1 (event_id TEXT PRIMARY KEY, portfolio_id TEXT NOT NULL, payload_json TEXT NOT NULL, FOREIGN KEY(portfolio_id) REFERENCES swing_portfolios_v1(portfolio_id))",
        "CREATE TABLE IF NOT EXISTS swing_episode_snapshots_v1 (portfolio_id TEXT NOT NULL, episode_id TEXT NOT NULL, sequence INTEGER NOT NULL CHECK(sequence > 0), payload_json TEXT NOT NULL, PRIMARY KEY(portfolio_id,episode_id,sequence), FOREIGN KEY(portfolio_id) REFERENCES swing_portfolios_v1(portfolio_id))",
    )


_SHAPE = "\n".join(_ddl())
SWING_SHAPE_HASH = hashlib.sha256(_SHAPE.encode()).hexdigest()


def _savepoint() -> str:
    return "swing_migration_" + secrets.token_hex(12)


def _shape_matches(connection: sqlite3.Connection) -> bool:
    def normalize_sql(value: str | None) -> str:
        normalized = " ".join((value or "").lower().split())
        return normalized.replace("create table if not exists ", "create table ")

    def columns(statement: str) -> list[str]:
        body = statement.split("(", 1)[1].rsplit(")", 1)[0]
        result: list[str] = []
        depth = 0
        token = ""
        for char in body + ",":
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            if char == "," and depth == 0:
                first = token.strip().split()[0]
                if first.upper().split("(", 1)[0] not in {"PRIMARY", "UNIQUE", "FOREIGN", "CHECK"}:
                    result.append(first)
                token = ""
            else:
                token += char
        return result
    for statement, table in zip(_ddl(), _TABLES):
        actual_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if actual_sql is None or normalize_sql(actual_sql[0]) != normalize_sql(statement):
            return False
        expected_columns = columns(statement)
        actual = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
        if actual != expected_columns:
            return False
    trigger_rows = {
        row[0]: normalize_sql(row[1])
        for row in connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger'")
    }
    for table in _TABLES:
        for action in ("update", "delete"):
            name = f"{table}_immutable_{action}"
            expected = (
                f"CREATE TRIGGER {name} BEFORE {action.upper()} ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'swing candidate is append-only'); END"
            )
            if name not in trigger_rows or trigger_rows[name] != normalize_sql(expected):
                return False
    open_trigger = trigger_rows.get("swing_one_open_symbol_v1", "")
    if "before insert on swing_position_snapshots_v1" not in open_trigger or "one symbol may have one active lot" not in open_trigger:
        return False
    return True


def migrate_swing_schema(connection: sqlite3.Connection) -> None:
    """Create P3 schema; reject P2/unknown candidate shapes, never silently upgrade."""
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SwingSchemaIncompatibleError("foreign_keys must be enabled before migration/outer transaction")
    savepoint = _savepoint()
    outer = connection.in_transaction
    try:
        connection.execute(f"SAVEPOINT {savepoint}")
        existing = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        candidate = set(_TABLES)
        if existing & candidate:
            if not candidate <= existing or not _shape_matches(connection):
                raise SwingSchemaIncompatibleError("existing candidate schema shape is incompatible")
            meta = connection.execute(
                "SELECT schema_version, shape_hash FROM swing_schema_meta_v1 WHERE schema_name='swing'").fetchone()
            if meta is None or tuple(meta) != (SWING_SCHEMA_VERSION, SWING_SHAPE_HASH):
                raise SwingSchemaIncompatibleError("existing candidate schema metadata is incompatible")
        else:
            for statement in _ddl():
                connection.execute(statement)
        now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        connection.execute("INSERT OR IGNORE INTO swing_schema_meta_v1 VALUES ('swing', ?, ?, ?)",
                           (SWING_SCHEMA_VERSION, SWING_SHAPE_HASH, now))
        for table in _TABLES:
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'swing candidate is append-only'); END")
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS {table}_immutable_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'swing candidate is append-only'); END")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS swing_one_open_symbol_v1 BEFORE INSERT ON swing_position_snapshots_v1 WHEN NEW.status='OPEN' AND EXISTS (SELECT 1 FROM swing_position_snapshots_v1 p WHERE p.portfolio_id=NEW.portfolio_id AND p.symbol=NEW.symbol AND p.status='OPEN' AND p.position_id<>NEW.position_id AND p.sequence=(SELECT MAX(x.sequence) FROM swing_position_snapshots_v1 x WHERE x.portfolio_id=p.portfolio_id AND x.position_id=p.position_id AND x.symbol=p.symbol)) BEGIN SELECT RAISE(ABORT, 'one symbol may have one active lot'); END""")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        if not outer:
            connection.commit()
    except SwingSchemaIncompatibleError:
        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
    except sqlite3.Error as exc:
        try:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            pass
        raise SwingPersistenceError("candidate schema migration failed") from exc
