"""Durable, bounded and secret-free telemetry for shadow cycles.

This store is deliberately separate from ``shadow-trades.db``.  It is a
small append-only (per session) evidence store, not a trading ledger.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

MAX_DATABASE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 4 * 1024 * 1024
MAX_PAGE_BYTES = 12 * 1024
MAX_RETAINED_SESSIONS = 20
SCHEMA_VERSION = 1
TABLE_NAME = "shadow_cycle_telemetry_v1"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FORCES = ("thrust", "gravity", "drag", "magnetic", "jerk", "impulse",
           "net_force", "current_velocity", "volume_drop_ratio")
_METRICS = ("current_price", "vwap", "strength", "trend_rsi", "atr_percent",
            "down_atr_percent", "volume_ratio")
_DECISION = ("market_regime", "strategy_reason_code", "strategy_intent",
             "paper_action", "position_before", "trading_window", "session_phase",
             "net_force_band", "current_velocity_band", "jerk_band", "strength_band",
             "trend_rsi_band", "thrust_band", "price_vwap_relation")
_CONTINUITY = ("schema_version", "hydration_source", "previous_observed_at",
               "history_depth", "baseline_source", "baseline_sample_index",
               "baseline_time_estimated")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TELEMETRY_COLUMNS = {
    "schema_version", "activation_id", "session_date_kst", "cycle_index",
    "observed_at", "stock_code", "proxy_code", "source_sha", "image_digest",
    "config_sha256", "strategy_slot", "candidate_id", *_METRICS,
    "forces_json", "decision_json", "position_after", "paper_position_id",
    "continuity_json", "row_sha256", "committed_at",
}
_SESSION_COLUMNS = {"activation_id", "session_date_kst", "finalized_at"}


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ValueError("configuration allowlist contains a non-JSON value")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def shadow_config_payload(settings: Any) -> dict[str, Any]:
    """Return the deliberately small, non-sensitive shadow configuration view.

    This is an explicit allowlist.  In particular, it never serializes a
    Settings object, legacy mappings, paths, credentials, headers, endpoints,
    webhook values, or storage identifiers.
    """
    runtime = settings.runtime
    execution = settings.execution
    monitoring = settings.monitoring
    strategy = settings.strategy
    candidate = settings.swing_candidate
    target_stop = strategy.target_stop_policy
    from kiwoom_stock.application.execution import (
        SHADOW_MAX_CYCLES, SHADOW_MAX_HTTP_ATTEMPTS, SHADOW_PROXY_CODE,
        SHADOW_STOCK_CODE,
    )

    return {
        "execution": {"mode": getattr(execution.mode, "value", execution.mode)},
        "runtime": {
            "app_env": runtime.app_env,
            "process_name": runtime.process_name,
        },
        "monitoring": {
            "fast_interval_seconds": monitoring.fast_interval_seconds,
            "slow_interval_seconds": monitoring.slow_interval_seconds,
            "max_workers": monitoring.max_workers,
            "max_stocks": monitoring.max_stocks,
            "etf_keywords": list(monitoring.etf_keywords),
            "market_proxy_code": monitoring.market_proxy_code,
        },
        "strategy": {
            "debug_mode": strategy.debug_mode,
            "day_trade_exit_time": strategy.day_trade_exit_time,
            "entry_deadline": strategy.entry_deadline,
            "cumulative_trade_return_score_floor": strategy.cumulative_trade_return_score_floor,
            "target_stop_unit_version": target_stop.unit_version,
            "target_profit_percentage_points": target_stop.target_profit_percentage_points,
            "stop_loss_percentage_points": target_stop.stop_loss_percentage_points,
            "regimes": _json_safe(strategy.regimes),
        },
        "swing_candidate": {
            "enabled": candidate.enabled,
            "portfolio_id": candidate.portfolio_id,
            "strategy_semantics_version": candidate.strategy_semantics_version,
        },
        "fixed_shadow": {
            "stock_code": SHADOW_STOCK_CODE,
            "proxy_code": SHADOW_PROXY_CODE,
            "max_cycles": SHADOW_MAX_CYCLES,
            "max_http_attempts": SHADOW_MAX_HTTP_ATTEMPTS,
        },
        "accounting_policy": {
            "version": "shadow-accounting-policy-v1",
            "paper_ledger_writes": True,
            "account_reads": False,
            "broker_orders": False,
            "oauth_revoke": False,
            "external_notifications": False,
            "reports": False,
        },
    }


def shadow_config_sha256(settings: Any) -> str:
    """Hash only :func:`shadow_config_payload` using canonical JSON."""
    return hashlib.sha256(_canonical(shadow_config_payload(settings)).encode()).hexdigest()


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class ShadowCycleTelemetry:
    """The complete safe input for one committed cycle.

    ``observed_at`` is an aware datetime.  ``session_date_kst`` is date-only
    (YYYY-MM-DD), never a datetime string.
    """

    activation_id: str
    session_date_kst: str
    cycle_index: int
    observed_at: datetime
    stock_code: str
    proxy_code: str
    source_sha: str
    image_digest: str
    config_sha256: str
    strategy_slot: str
    current_price: float
    vwap: float
    strength: float
    trend_rsi: float
    atr_percent: float
    down_atr_percent: float
    volume_ratio: float
    forces: Mapping[str, float]
    decision: Mapping[str, str]
    position_after: str
    continuity: Mapping[str, Any]
    candidate_id: str | None = None
    paper_position_id: str | None = None
    committed_at: datetime | None = None
    row_sha256: str = ""

    def __post_init__(self) -> None:
        if not _IDENTITY.fullmatch(self.activation_id) or not _IDENTITY.fullmatch(self.strategy_slot):
            raise ValueError("invalid telemetry identity")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", self.session_date_kst):
            raise ValueError("session_date_kst must be YYYY-MM-DD")
        date.fromisoformat(self.session_date_kst)
        if type(self.cycle_index) is not int or self.cycle_index < 1:
            raise ValueError("cycle_index must be positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in ("stock_code", "proxy_code", "source_sha", "image_digest", "config_sha256", "position_after"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value or "token" in value.lower():
                raise ValueError(f"invalid telemetry {name}")
        if _SHA256.fullmatch(self.config_sha256) is None:
            raise ValueError("config_sha256 must be lowercase SHA-256")
        if len(self.source_sha) != 40 or not re.fullmatch(r"[0-9a-f]{40}", self.source_sha):
            raise ValueError("source_sha must be a git SHA")
        for name in _METRICS:
            _finite(getattr(self, name), name)
        if set(self.forces) != set(_FORCES):
            raise ValueError("forces allowlist mismatch")
        for name in _FORCES:
            _finite(self.forces[name], f"forces.{name}")
        if set(self.decision) != set(_DECISION) or any(not isinstance(v, str) for v in self.decision.values()):
            raise ValueError("decision allowlist mismatch")
        if set(self.continuity) != set(_CONTINUITY):
            raise ValueError("continuity allowlist mismatch")
        if self.committed_at is not None and (self.committed_at.tzinfo is None or self.committed_at.utcoffset() is None):
            raise ValueError("committed_at must be timezone-aware")

    def payload(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "activation_id": self.activation_id, "session_date_kst": self.session_date_kst,
            "cycle_index": self.cycle_index, "observed_at": self.observed_at.isoformat(),
            "stock_code": self.stock_code, "proxy_code": self.proxy_code,
            "source_sha": self.source_sha, "image_digest": self.image_digest,
            "config_sha256": self.config_sha256, "strategy_slot": self.strategy_slot,
            **{name: getattr(self, name) for name in _METRICS},
            "forces": dict(self.forces), "decision": dict(self.decision),
            "position_after": self.position_after, "continuity": dict(self.continuity),
            "candidate_id": self.candidate_id, "paper_position_id": self.paper_position_id,
        }
        return result

    def canonical_json(self) -> str:
        return _canonical(self.payload())

    def hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()

    def as_record(self) -> dict[str, Any]:
        return {**self.payload(), "row_sha256": self.row_sha256 or self.hash(),
                "committed_at": (self.committed_at or datetime.now(timezone.utc)).isoformat()}


class ShadowTelemetryStore:
    """SQLite writer with bounded pages and fail-closed conflict handling."""

    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 2_000) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=busy_timeout_ms / 1000)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        self.connection.execute("PRAGMA journal_mode=DELETE")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA user_version=1")
        self.connection.execute("PRAGMA application_id=1397574740")
        self.connection.execute("PRAGMA max_page_count=8192")
        self.connection.executescript(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
          schema_version INTEGER NOT NULL CHECK(schema_version = 1), activation_id TEXT NOT NULL,
          session_date_kst TEXT NOT NULL CHECK(length(session_date_kst) = 10), cycle_index INTEGER NOT NULL CHECK(cycle_index > 0),
          observed_at TEXT NOT NULL, stock_code TEXT NOT NULL, proxy_code TEXT NOT NULL,
          source_sha TEXT NOT NULL, image_digest TEXT NOT NULL, config_sha256 TEXT NOT NULL,
          strategy_slot TEXT NOT NULL, candidate_id TEXT, current_price REAL NOT NULL, vwap REAL NOT NULL,
          strength REAL NOT NULL, trend_rsi REAL NOT NULL, atr_percent REAL NOT NULL, down_atr_percent REAL NOT NULL,
          volume_ratio REAL NOT NULL, forces_json TEXT NOT NULL, decision_json TEXT NOT NULL,
          position_after TEXT NOT NULL, paper_position_id TEXT, continuity_json TEXT NOT NULL,
          row_sha256 TEXT NOT NULL, committed_at TEXT NOT NULL,
          UNIQUE(activation_id, cycle_index, strategy_slot)
        );
        CREATE INDEX IF NOT EXISTS idx_shadow_telemetry_session ON {TABLE_NAME}(activation_id, session_date_kst, cycle_index);
        CREATE TABLE IF NOT EXISTS shadow_telemetry_sessions_v1 (
          activation_id TEXT NOT NULL, session_date_kst TEXT NOT NULL,
          finalized_at TEXT NOT NULL, PRIMARY KEY (activation_id, session_date_kst)
        );
        """)
        self.connection.commit()
        self._check_database_bound()

    def _check_database_bound(self) -> None:
        page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
        if page_size <= 0 or page_count <= 0 or page_size * page_count > MAX_DATABASE_BYTES:
            raise ValueError("telemetry database exceeds 32 MiB bound")

    def append(self, row: ShadowCycleTelemetry) -> str:
        expected = row.hash()
        supplied = row.row_sha256 or expected
        if supplied != expected:
            raise ValueError("telemetry row hash mismatch")
        record = row.as_record()
        columns = ("schema_version", "activation_id", "session_date_kst", "cycle_index", "observed_at", "stock_code", "proxy_code", "source_sha", "image_digest", "config_sha256", "strategy_slot", "candidate_id", *_METRICS, "forces_json", "decision_json", "position_after", "paper_position_id", "continuity_json", "row_sha256", "committed_at")
        values = [record.get(c) for c in columns]
        values[columns.index("forces_json")] = _canonical(row.forces)
        values[columns.index("decision_json")] = _canonical(row.decision)
        values[columns.index("continuity_json")] = _canonical(row.continuity)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(f"SELECT row_sha256 FROM {TABLE_NAME} WHERE activation_id=? AND cycle_index=? AND strategy_slot=?", (row.activation_id, row.cycle_index, row.strategy_slot)).fetchone()
            if existing is not None:
                if existing[0] != expected:
                    raise ValueError("conflicting telemetry row hash")
                self.connection.rollback()
                return expected
            self.connection.execute(f"INSERT INTO {TABLE_NAME} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", values)
            self._check_database_bound()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        return expected

    def commit_receipt(
        self, policy: Any, session_date_kst: date, receipt: Any, *, config_sha256: str | None = None
    ) -> str:
        """Adapt the existing safe runtime receipt without re-reading the API."""
        metrics = receipt.telemetry_metrics
        if not isinstance(metrics, Mapping):
            raise ValueError("shadow runtime omitted numeric telemetry")
        decision_obj = receipt.decision_telemetry
        continuity_obj = receipt.continuity
        if decision_obj is None or continuity_obj is None:
            raise ValueError("shadow runtime omitted cycle telemetry")
        decision = decision_obj.to_safe_dict()
        continuity = continuity_obj.to_safe_dict()
        if config_sha256 is None or _SHA256.fullmatch(config_sha256) is None:
            raise ValueError("explicit config_sha256 is required")
        row = ShadowCycleTelemetry(
            activation_id=policy.activation.activation_id,
            session_date_kst=session_date_kst.isoformat(),
            cycle_index=getattr(receipt, "cycle_index", 1),
            observed_at=(
                receipt.observed_at
                if isinstance(getattr(receipt, "observed_at", None), datetime)
                else datetime.now(timezone.utc)
            ),
            stock_code=policy.stock_code,
            proxy_code=policy.proxy_code,
            source_sha=policy.activation.source_sha,
            image_digest=policy.activation.image_digest,
            config_sha256=config_sha256,
            strategy_slot=getattr(policy, "strategy_slot", "baseline"),
            current_price=metrics["current_price"], vwap=metrics["vwap"],
            strength=metrics["strength"], trend_rsi=metrics["trend_rsi"],
            atr_percent=metrics["atr_percent"], down_atr_percent=metrics["down_atr_percent"],
            volume_ratio=metrics["volume_ratio"], forces=metrics["forces"],
            decision=decision, position_after=receipt.position_after or decision["position_before"],
            continuity=continuity,
        )
        return self.append(row)

    def rows(self, activation_id: str, session_date_kst: str) -> list[dict[str, Any]]:
        found = self.connection.execute(f"SELECT * FROM {TABLE_NAME} WHERE activation_id=? AND session_date_kst=? ORDER BY cycle_index, strategy_slot", (activation_id, session_date_kst)).fetchall()
        return [dict(row) for row in found]

    def finalize_session(self, activation_id: str, session_date_kst: str) -> None:
        """Mark a session complete and retain at most 20 finalized sessions."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT OR REPLACE INTO shadow_telemetry_sessions_v1 VALUES (?,?,?)",
                (activation_id, session_date_kst, datetime.now(timezone.utc).isoformat()),
            )
            old = self.connection.execute(
                "SELECT activation_id, session_date_kst FROM shadow_telemetry_sessions_v1 "
                "ORDER BY finalized_at DESC LIMIT -1 OFFSET ?", (MAX_RETAINED_SESSIONS,)
            ).fetchall()
            for old_activation, old_date in old:
                self.connection.execute(
                    f"DELETE FROM {TABLE_NAME} WHERE activation_id=? AND session_date_kst=?",
                    (old_activation, old_date),
                )
                self.connection.execute(
                    "DELETE FROM shadow_telemetry_sessions_v1 WHERE activation_id=? AND session_date_kst=?",
                    (old_activation, old_date),
                )
            self._check_database_bound()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def export(self, activation_id: str, session_date_kst: str) -> tuple[bytes, dict[str, Any]]:
        self._check_database_bound()
        rows = self.rows(activation_id, session_date_kst)
        cycles = [row["cycle_index"] for row in rows]
        if cycles != list(range(1, len(cycles) + 1)) or len(cycles) != len(set(cycles)):
            raise ValueError("telemetry cycles are missing, duplicate, or non-contiguous")
        records = [self._row_record(row) for row in rows]
        for record in records:
            if not re.fullmatch(r"[0-9a-f]{64}", str(record["row_sha256"])):
                raise ValueError("telemetry row hash is invalid")
            if self._row_hash(record) != record["row_sha256"]:
                raise ValueError("telemetry canonical row hash mismatch")
            if record["activation_id"] != activation_id or record["session_date_kst"] != session_date_kst:
                raise ValueError("telemetry row identity mismatch")
        identities = {(r["source_sha"], r["image_digest"], r["config_sha256"]) for r in records}
        if len(identities) > 1:
            raise ValueError("telemetry provenance identity mismatch")
        lines = b"".join((_canonical(record).encode() + b"\n") for record in records)
        compressed = gzip.compress(lines, mtime=0)
        if len(compressed) > MAX_EXPORT_BYTES:
            raise ValueError("telemetry export exceeds 4 MiB")
        page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
        finalized_count = int(self.connection.execute("SELECT count(*) FROM shadow_telemetry_sessions_v1").fetchone()[0])
        if finalized_count > MAX_RETAINED_SESSIONS:
            raise ValueError("finalized telemetry sessions exceed retention bound")
        identity = records[0] if records else {}
        manifest = {"schema_version": 1, "activation_id": activation_id, "session_date_kst": session_date_kst, "row_count": len(rows), "first_cycle": rows[0]["cycle_index"] if rows else None, "last_cycle": rows[-1]["cycle_index"] if rows else None, "source_sha": identity.get("source_sha"), "image_digest": identity.get("image_digest"), "config_sha256": identity.get("config_sha256"), "database_bytes": page_size * page_count, "database_page_size": page_size, "database_page_count": page_count, "finalized_session_count": finalized_count, "session_sha256": hashlib.sha256(lines).hexdigest(), "compressed_sha256": hashlib.sha256(compressed).hexdigest(), "compressed_bytes": len(compressed)}
        return compressed, manifest

    @staticmethod
    def _row_hash(record: Mapping[str, Any]) -> str:
        payload = {key: record[key] for key in (
            "schema_version", "activation_id", "session_date_kst", "cycle_index",
            "observed_at", "stock_code", "proxy_code", "source_sha", "image_digest",
            "config_sha256", "strategy_slot", "current_price", "vwap", "strength",
            "trend_rsi", "atr_percent", "down_atr_percent", "volume_ratio", "forces",
            "decision", "position_after", "continuity", "candidate_id", "paper_position_id",
        )}
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    @staticmethod
    def _row_record(row: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for column in ("forces_json", "decision_json", "continuity_json"):
            result[column.removesuffix("_json")] = json.loads(result.pop(column))
        return result

    def close(self) -> None:
        self.connection.close()


class ShadowTelemetryReader(ShadowTelemetryStore):
    """Strict read-only view used by stop/export on a mounted volume.

    This intentionally does not call the writer constructor: no directory
    creation, schema migration, write PRAGMA, transaction, or commit occurs.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise ValueError("telemetry database is missing")
        uri = f"file:{self.path.absolute()}?mode=ro"
        try:
            self.connection = sqlite3.connect(uri, uri=True)
            self.connection.row_factory = sqlite3.Row
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version != 1:
                raise ValueError("telemetry schema version is unsupported")
            table = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (TABLE_NAME,),
            ).fetchone()
            if table is None:
                raise ValueError("telemetry schema is missing")
            columns = {
                row[1] for row in self.connection.execute(
                    f"PRAGMA table_info({TABLE_NAME})"
                ).fetchall()
            }
            if (
                self.connection.execute("PRAGMA application_id").fetchone()[0]
                != 1397574740
                or columns != _TELEMETRY_COLUMNS
            ):
                raise ValueError("telemetry schema is malformed")
            session_table = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='shadow_telemetry_sessions_v1'"
            ).fetchone()
            session_columns = {
                row[1] for row in self.connection.execute(
                    "PRAGMA table_info(shadow_telemetry_sessions_v1)"
                ).fetchall()
            }
            if session_table is None or session_columns != _SESSION_COLUMNS:
                raise ValueError("telemetry session schema is malformed")
            page_size = int(self.connection.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(self.connection.execute("PRAGMA page_count").fetchone()[0])
            if page_size <= 0 or page_count <= 0 or page_size * page_count > MAX_DATABASE_BYTES:
                raise ValueError("telemetry database exceeds 32 MiB bound")
        except Exception:
            try:
                self.connection.close()
            except Exception:
                pass
            raise

    def append(self, row: ShadowCycleTelemetry) -> str:  # type: ignore[override]
        raise RuntimeError("telemetry reader is read-only")

    def finalize_session(self, activation_id: str, session_date_kst: str) -> None:  # type: ignore[override]
        raise RuntimeError("telemetry reader is read-only")

    def commit_receipt(self, *args: Any, **kwargs: Any) -> str:  # type: ignore[override]
        raise RuntimeError("telemetry reader is read-only")
