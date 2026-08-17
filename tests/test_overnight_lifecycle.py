from dataclasses import replace
from datetime import date, datetime
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.ports import PaperTradePersistenceError
from kiwoom_stock.core import database as database_module
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.domain.models import (
    PositionDecision,
    PositionDecisionResult,
    PositionStatus,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.manager import StockManager
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.utils.market_cal import KrxCalendarError


KST = ZoneInfo("Asia/Seoul")
VALID_FORCES = {
    "thrust": 0.1,
    "gravity": -0.2,
    "drag": -0.3,
    "magnetic": 0.4,
    "jerk": 0.5,
    "impulse": 0.6,
    "net_force": 0.7,
}


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _manager(ledger, clock, guard=None):
    return StockManager(
        MagicMock(),
        ledger,
        {},
        clock=clock,
        paper_transition_guard=guard or MagicMock(),
        strict_paper_errors=True,
    )


def _buy(manager, *, stock_code="005930", price=10_000.0):
    manager.stock_names[stock_code] = stock_code
    success, data = manager.apply_paper_buy(
        {
            "stock_code": stock_code,
            "price": price,
            "regime": "STABLE_BULL",
            "forces": {},
        }
    )
    assert success is True
    assert data is not None
    return manager.active_positions[stock_code]


def _row(path, stock_code="005930"):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return dict(
            connection.execute(
                "SELECT * FROM trades WHERE stock_code = ?",
                (stock_code,),
            ).fetchone()
        )


def _old_trades_schema(path):
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE trades (
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
                status TEXT DEFAULT 'OPEN'
            )
            """
        )


def _insert_position_row(
    path,
    *,
    stock_code="005930",
    status="OPEN",
    include_lifecycle=True,
    **overrides,
):
    values = {
        "id": None,
        "stock_code": stock_code,
        "stock_name": "Samsung",
        "buy_price": 10_000.0,
        **VALID_FORCES,
        "buy_time": "2026-08-03 09:30:00",
        "buy_regime": "STABLE_BULL",
        "sell_price": None,
        "profit_rate": None,
        "sell_time": None,
        "sell_reason": None,
        "status": status,
        "owning_session_date": "2026-08-03",
        "state_changed_at": "2026-08-03T09:30:00+09:00",
    }
    values.update(overrides)
    columns = [
        "id",
        "stock_code",
        "stock_name",
        "buy_price",
        *VALID_FORCES,
        "buy_time",
        "buy_regime",
        "sell_price",
        "profit_rate",
        "sell_time",
        "sell_reason",
        "status",
    ]
    if include_lifecycle:
        columns.extend(("owning_session_date", "state_changed_at"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"INSERT INTO trades ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )


def _old_schema_snapshot(path):
    with sqlite3.connect(path) as connection:
        return (
            tuple(connection.execute("PRAGMA table_info(trades)")),
            tuple(connection.execute("SELECT * FROM trades ORDER BY id")),
            tuple(
                connection.execute(
                    "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
                )
            ),
        )


class _InitializationConnectionProxy:
    def __init__(
        self,
        connection,
        *,
        fail_second_alter=False,
        corrupt_post_shape=False,
        mismatch_backfill_rowcount=False,
    ):
        self.connection = connection
        self.fail_second_alter = fail_second_alter
        self.corrupt_post_shape = corrupt_post_shape
        self.mismatch_backfill_rowcount = mismatch_backfill_rowcount
        self.trades_shape_reads = 0

    @property
    def row_factory(self):
        return self.connection.row_factory

    @row_factory.setter
    def row_factory(self, value):
        self.connection.row_factory = value

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, *args, **kwargs):
        normalized = " ".join(sql.split())
        if (
            self.fail_second_alter
            and normalized
            == "ALTER TABLE trades ADD COLUMN state_changed_at TEXT"
        ):
            raise sqlite3.OperationalError("injected second ALTER failure")
        if normalized == "PRAGMA table_info(trades)":
            self.trades_shape_reads += 1
            cursor = self.connection.execute(sql, *args, **kwargs)
            if self.corrupt_post_shape and self.trades_shape_reads == 2:
                return [row for row in cursor if row[1] != "state_changed_at"]
            return cursor
        return self.connection.execute(sql, *args, **kwargs)

    def executemany(self, sql, parameters):
        cursor = self.connection.executemany(sql, parameters)
        if not self.mismatch_backfill_rowcount:
            return cursor
        return SimpleNamespace(rowcount=0)


def test_fresh_buy_persists_open_session_metadata_and_reloads_exactly(tmp_path):
    path = tmp_path / "fresh-buy.sqlite3"
    now = datetime(2026, 8, 3, 10, 5, tzinfo=KST)
    clock = MutableClock(now)
    guard = MagicMock()
    ledger = TradeLogger(path, clock=clock)
    manager = _manager(ledger, clock, guard)

    position = _buy(manager)

    assert position.status is PositionStatus.OPEN
    assert position.owning_session_date == date(2026, 8, 3)
    assert position.state_changed_at == now
    guard.assert_called_once_with()
    ledger.close()

    raw = _row(path)
    assert raw["status"] == "OPEN"
    assert raw["owning_session_date"] == "2026-08-03"
    assert raw["state_changed_at"] == "2026-08-03T10:05:00+09:00"

    reopened = TradeLogger(path, clock=clock)
    try:
        restored = reopened.load_active_positions()["005930"]
        assert restored["status"] is PositionStatus.OPEN
        assert restored["owning_session_date"] == date(2026, 8, 3)
        assert restored["state_changed_at"] == now
    finally:
        reopened.close()


def test_strategy_marks_overnight_without_position_mutation_then_manager_commits(
    tmp_path,
):
    path = tmp_path / "mark-overnight.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=KST))
    guard = MagicMock()
    ledger = TradeLogger(path, clock=clock)
    manager = _manager(ledger, clock, guard)
    position = _buy(manager)
    guard.reset_mock()
    strategy = TradingStrategy({"debug_mode": False}, clock=clock)
    before = vars(position).copy()

    decision = strategy.decide_position(
        position,
        10_000.0,
        {
            "current_velocity": 3.0,
            "thrust": 2.1,
            "magnetic": 0.1,
            "jerk": 0.0,
        },
    )

    assert decision.decision is PositionDecision.MARK_OVERNIGHT
    assert vars(position) == before
    manager.apply_paper_mark_overnight(position)
    assert position.status is PositionStatus.OVERNIGHT
    assert position.state_changed_at == clock.value
    guard.assert_called_once_with()
    assert _row(path)["status"] == "OVERNIGHT"
    ledger.close()


@pytest.mark.parametrize("failure_kind", ["commit", "rowcount"])
def test_mark_overnight_failure_leaves_database_and_memory_at_prior_state(
    tmp_path,
    failure_kind,
):
    path = tmp_path / f"mark-failure-{failure_kind}.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 15, 20, tzinfo=KST))
    guard = MagicMock()
    ledger = TradeLogger(path, clock=clock)
    manager = _manager(ledger, clock, guard)
    position = _buy(manager)
    guard.reset_mock()
    before = vars(position).copy()
    if failure_kind == "commit":
        ledger.conn.execute(
            """
            CREATE TRIGGER reject_overnight
            BEFORE UPDATE OF status ON trades
            WHEN NEW.status = 'OVERNIGHT'
            BEGIN SELECT RAISE(ABORT, 'injected commit failure'); END
            """
        )
        ledger.conn.commit()
    else:
        position.id += 1000
        before = vars(position).copy()

    with pytest.raises(PaperTradePersistenceError):
        manager.apply_paper_mark_overnight(position)

    assert vars(position) == before
    assert _row(path)["status"] == "OPEN"
    guard.assert_called_once_with()
    ledger.close()


def test_load_active_positions_restores_open_and_overnight_but_not_closed(tmp_path):
    path = tmp_path / "mixed.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    ledger = TradeLogger(path, clock=clock)
    metadata = {
        "buy_price": 10_000.0,
        "buy_time": "2026-08-03 10:00:00",
        "buy_regime": "STABLE_BULL",
        "owning_session_date": date(2026, 8, 3),
        "state_changed_at": clock.value,
        **VALID_FORCES,
    }
    for code in ("OPEN", "OVERNIGHT", "CLOSED"):
        ledger.record_buy({"stock_code": code, "stock_name": code, **metadata})
    ledger.conn.execute(
        "UPDATE trades SET status = 'OVERNIGHT' WHERE stock_code = 'OVERNIGHT'"
    )
    ledger.conn.execute(
        "UPDATE trades SET status = 'CLOSED' WHERE stock_code = 'CLOSED'"
    )
    ledger.conn.commit()

    active = ledger.load_active_positions()

    assert set(active) == {"OPEN", "OVERNIGHT"}
    assert active["OPEN"]["status"] is PositionStatus.OPEN
    assert active["OVERNIGHT"]["status"] is PositionStatus.OVERNIGHT
    with pytest.warns(DeprecationWarning):
        open_only = ledger.load_open_positions()
    assert set(open_only) == {"OPEN"}
    ledger.close()


def test_legacy_position_status_lifecycle_set_is_open_overnight_closed(tmp_path):
    path = tmp_path / "legacy-status-set.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    ledger = TradeLogger(path, clock=clock)
    try:
        assert {status.value for status in PositionStatus} == {"OPEN", "OVERNIGHT", "CLOSED"}
    finally:
        ledger.close()


def _seed_overnight(path, clock, guard):
    ledger = TradeLogger(path, clock=clock)
    manager = _manager(ledger, clock, guard)
    position = _buy(manager)
    clock.value = datetime(2026, 8, 3, 15, 20, tzinfo=KST)
    manager.apply_paper_mark_overnight(position)
    return ledger, manager, position


def test_same_owner_session_is_idempotent_across_repeated_calls_and_restart(tmp_path):
    path = tmp_path / "same-session.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, _ = _seed_overnight(path, clock, guard)
    guard.reset_mock()
    before = _row(path)

    assert manager.reconcile_overnight_positions() == 0
    assert manager.reconcile_overnight_positions() == 0
    assert _row(path) == before
    guard.assert_not_called()
    ledger.close()

    reopened = TradeLogger(path, clock=clock)
    restarted = _manager(reopened, clock, guard)
    assert restarted.reconcile_overnight_positions() == 0
    assert _row(path) == before
    guard.assert_not_called()
    reopened.close()


@pytest.mark.parametrize(
    "closed_instant",
    [
        datetime(2026, 8, 4, 8, 59, tzinfo=KST),
        datetime(2026, 8, 4, 15, 31, tzinfo=KST),
        datetime(2026, 8, 8, 10, 0, tzinfo=KST),
        datetime(2026, 8, 17, 10, 0, tzinfo=KST),
    ],
    ids=["pre-open", "post-close", "weekend", "holiday"],
)
def test_closed_market_instants_do_not_reopen_or_mutate(tmp_path, closed_instant):
    path = tmp_path / f"closed-{closed_instant.date()}.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, position = _seed_overnight(path, clock, guard)
    guard.reset_mock()
    before_row = _row(path)
    before_memory = vars(position).copy()
    clock.value = closed_instant

    assert manager.reconcile_overnight_positions() == 0

    assert vars(position) == before_memory
    assert _row(path) == before_row
    guard.assert_not_called()
    ledger.close()


@pytest.mark.parametrize(
    ("observed_at", "expected_owner"),
    [
        (datetime(2026, 8, 4, 9, 0, tzinfo=KST), date(2026, 8, 4)),
        (datetime(2026, 8, 6, 10, 0, tzinfo=KST), date(2026, 8, 6)),
    ],
    ids=["next-open", "missed-session-catch-up"],
)
def test_next_or_later_open_session_reopens_exactly_once(
    tmp_path,
    observed_at,
    expected_owner,
):
    path = tmp_path / f"reopen-{expected_owner}.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, position = _seed_overnight(path, clock, guard)
    guard.reset_mock()
    clock.value = observed_at

    assert manager.reconcile_overnight_positions() == 1
    first = _row(path)
    assert position.status is PositionStatus.OPEN
    assert position.owning_session_date == expected_owner
    assert position.state_changed_at == observed_at
    assert first["status"] == "OPEN"
    assert first["owning_session_date"] == expected_owner.isoformat()
    assert first["state_changed_at"] == observed_at.isoformat()
    assert manager.reconcile_overnight_positions() == 0
    assert _row(path) == first
    guard.assert_called_once_with()
    ledger.close()

    reopened = TradeLogger(path, clock=clock)
    restarted = _manager(reopened, clock, guard)
    assert restarted.reconcile_overnight_positions() == 0
    assert _row(path) == first
    guard.assert_called_once_with()
    reopened.close()


def test_legacy_open_backfill_is_strict_additive_and_idempotent(tmp_path):
    path = tmp_path / "legacy-open.sqlite3"
    _old_trades_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO trades (
                stock_code, stock_name, buy_price,
                thrust, gravity, drag, magnetic, jerk, impulse, net_force,
                buy_time, buy_regime, status
            ) VALUES ('005930', 'Samsung', 10000,
                      0.1, -0.2, -0.3, 0.4, 0.5, 0.6, 0.7,
                      '2026-08-03 09:30:00', 'STABLE_BULL', 'OPEN')
            """
        )
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    first = TradeLogger(path, clock=clock)
    try:
        restored = first.load_active_positions()["005930"]
        assert restored["owning_session_date"] == date(2026, 8, 3)
        assert restored["state_changed_at"] == datetime(
            2026, 8, 3, 9, 30, tzinfo=KST
        )
    finally:
        first.close()
    first_row = _row(path)
    assert first_row["owning_session_date"] == "2026-08-03"
    assert first_row["state_changed_at"] == "2026-08-03T09:30:00+09:00"

    second = TradeLogger(path, clock=clock)
    try:
        assert second.load_active_positions()["005930"][
            "state_changed_at"
        ] == datetime(2026, 8, 3, 9, 30, tzinfo=KST)
        assert second.conn.total_changes == 0
    finally:
        second.close()
    assert _row(path) == first_row


@pytest.mark.parametrize(
    ("status", "owner", "changed", "error"),
    [
        ("OVERNIGHT", None, None, "cannot be inferred"),
        ("UNKNOWN", None, None, "unsupported"),
        ("OPEN", "2026-08-08", "2026-08-08T10:00:00+09:00", "not an XKRX"),
        ("OPEN", "2026-08-03", None, "partial"),
        ("OPEN", "bad-date", "2026-08-03T10:00:00+09:00", "YYYY-MM-DD"),
        ("OPEN", "2026-08-03", "2026-08-03 10:00:00", "canonical"),
    ],
)
def test_malformed_active_rows_fail_without_overwrite(
    tmp_path,
    status,
    owner,
    changed,
    error,
):
    path = tmp_path / f"malformed-{status}-{error}.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    ledger = TradeLogger(path, clock=clock)
    ledger.conn.execute(
        """
        INSERT INTO trades (
            stock_code, stock_name, buy_price,
            thrust, gravity, drag, magnetic, jerk, impulse, net_force,
            buy_time, buy_regime, status,
            owning_session_date, state_changed_at
        ) VALUES ('005930', 'Samsung', 10000,
                  0.1, -0.2, -0.3, 0.4, 0.5, 0.6, 0.7,
                  '2026-08-03 09:30:00', 'STABLE_BULL', ?, ?, ?)
        """,
        (status, owner, changed),
    )
    ledger.conn.commit()
    before = _row(path)

    with pytest.raises(PaperTradePersistenceError, match=error):
        ledger.load_active_positions()

    assert _row(path) == before
    ledger.close()


def test_calendar_failure_blocks_reconcile_before_decision_and_memory_mutation(
    tmp_path,
):
    path = tmp_path / "calendar-failure.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    ledger, manager, position = _seed_overnight(path, clock, MagicMock())
    clock.value = datetime(2026, 8, 4, 10, 0, tzinfo=KST)
    manager._current_session_resolver = MagicMock(
        side_effect=KrxCalendarError("calendar unavailable")
    )
    before_row = _row(path)
    before_memory = vars(position).copy()
    engine = TradingEngine.__new__(TradingEngine)
    engine.stock_mgr = manager
    engine.strategy = MagicMock()
    engine.notifier = MagicMock()

    with pytest.raises(KrxCalendarError, match="calendar unavailable"):
        engine._process_decisions(
            [{"stock_code": "005930", "price": 10_300.0}]
        )

    assert vars(position) == before_memory
    assert _row(path) == before_row
    engine.strategy.decide_position.assert_not_called()
    engine.notifier.collect_status.assert_not_called()
    ledger.close()


def test_reopen_commit_failure_blocks_cycle_decision_and_keeps_prior_state(tmp_path):
    path = tmp_path / "reopen-failure.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, position = _seed_overnight(path, clock, guard)
    clock.value = datetime(2026, 8, 4, 10, 0, tzinfo=KST)
    ledger.conn.execute(
        """
        CREATE TRIGGER reject_reopen
        BEFORE UPDATE OF status ON trades
        WHEN NEW.status = 'OPEN'
        BEGIN SELECT RAISE(ABORT, 'injected reopen failure'); END
        """
    )
    ledger.conn.commit()
    before_row = _row(path)
    before_memory = vars(position).copy()
    engine = TradingEngine.__new__(TradingEngine)
    engine.stock_mgr = manager
    engine.strategy = MagicMock()
    engine.notifier = MagicMock()

    with pytest.raises(PaperTradePersistenceError, match="commit failed"):
        engine._process_decisions(
            [{"stock_code": "005930", "price": 10_300.0}]
        )

    assert vars(position) == before_memory
    assert _row(path) == before_row
    engine.strategy.decide_position.assert_not_called()
    engine.notifier.collect_status.assert_not_called()
    ledger.close()


def test_schema_migration_rejects_wrong_additive_column_shape(tmp_path):
    path = tmp_path / "wrong-shape.sqlite3"
    _old_trades_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "ALTER TABLE trades ADD COLUMN owning_session_date INTEGER"
        )
        connection.execute("ALTER TABLE trades ADD COLUMN state_changed_at TEXT")

    with pytest.raises(PaperTradePersistenceError, match="TEXT affinity"):
        TradeLogger(path)


def _run_downgrade_preflight(database_path, cwd):
    repository_root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repository_root)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kiwoom_stock",
            "downgrade-preflight",
            "--database-path",
            str(database_path),
        ],
        cwd=cwd,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _file_evidence(path):
    evidence = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_CLOEXEC
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            evidence[candidate.name] = None
            continue
        descriptor = os.open(candidate, flags)
        try:
            content = b""
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                content += chunk
        finally:
            os.close(descriptor)
        evidence[candidate.name] = (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_gid,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_atime_ns,
            metadata.st_ctime_ns,
            hashlib.sha256(content).hexdigest(),
        )
    return evidence


def _read_noatime(path):
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME | os.O_CLOEXEC,
    )
    try:
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _without_ctime(evidence):
    return {
        name: None if item is None else item[:8] + item[9:]
        for name, item in evidence.items()
    }


def _wal_target(path, statuses):
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute("CREATE TABLE trades (status TEXT)")
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.executemany(
        "INSERT INTO trades (status) VALUES (?)",
        [(status,) for status in statuses],
    )
    connection.commit()
    assert Path(str(path) + "-wal").stat().st_size > 0
    return connection


def _assert_failed_initialization_rolls_back(
    monkeypatch,
    path,
    proxy_factory,
    expected_message,
):
    before = _old_schema_snapshot(path)
    real_connect = database_module.sqlite3.connect
    worker_starts = MagicMock()
    with monkeypatch.context() as scoped:
        scoped.setattr(
            database_module.sqlite3,
            "connect",
            lambda *args, **kwargs: proxy_factory(
                real_connect(*args, **kwargs)
            ),
        )
        scoped.setattr(database_module.threading.Thread, "start", worker_starts)
        with pytest.raises(PaperTradePersistenceError, match=expected_message):
            TradeLogger(path)

    assert _old_schema_snapshot(path) == before
    columns = {row[1] for row in _old_schema_snapshot(path)[0]}
    assert "owning_session_date" not in columns
    assert "state_changed_at" not in columns
    worker_starts.assert_not_called()


def test_second_lifecycle_alter_failure_rolls_back_both_columns(monkeypatch, tmp_path):
    path = tmp_path / "second-alter.sqlite3"
    _old_trades_schema(path)
    _insert_position_row(path, include_lifecycle=False)

    _assert_failed_initialization_rolls_back(
        monkeypatch,
        path,
        lambda connection: _InitializationConnectionProxy(
            connection,
            fail_second_alter=True,
        ),
        "initialization failed",
    )


def test_legacy_backfill_failure_rolls_back_schema_and_row(monkeypatch, tmp_path):
    path = tmp_path / "backfill-failure.sqlite3"
    _old_trades_schema(path)
    _insert_position_row(path, include_lifecycle=False)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_lifecycle_backfill
            BEFORE UPDATE OF owning_session_date ON trades
            BEGIN SELECT RAISE(ABORT, 'injected backfill failure'); END
            """
        )

    _assert_failed_initialization_rolls_back(
        monkeypatch,
        path,
        lambda connection: _InitializationConnectionProxy(connection),
        "initialization failed",
    )


def test_post_shape_failure_rolls_back_schema_and_backfill(monkeypatch, tmp_path):
    path = tmp_path / "post-shape.sqlite3"
    _old_trades_schema(path)
    _insert_position_row(path, include_lifecycle=False)

    _assert_failed_initialization_rolls_back(
        monkeypatch,
        path,
        lambda connection: _InitializationConnectionProxy(
            connection,
            corrupt_post_shape=True,
        ),
        "must exist exactly once",
    )


def test_backfill_rowcount_mismatch_rolls_back_schema_and_row(monkeypatch, tmp_path):
    path = tmp_path / "rowcount-mismatch.sqlite3"
    _old_trades_schema(path)
    _insert_position_row(path, include_lifecycle=False)

    _assert_failed_initialization_rolls_back(
        monkeypatch,
        path,
        lambda connection: _InitializationConnectionProxy(
            connection,
            mismatch_backfill_rowcount=True,
        ),
        "identity mismatch",
    )


@pytest.mark.parametrize(
    ("field_name", "invalid_value", "error_field"),
    [
        ("id", 0, "id"),
        ("stock_code", "", "stock_code"),
        ("stock_name", None, "stock_name"),
        ("buy_regime", "", "buy_regime"),
        ("buy_time", "2026/08/03 09:30:00", "buy_time"),
        ("buy_price", None, "buy_price"),
        ("buy_price", "not-numeric", "buy_price"),
        ("buy_price", float("inf"), "buy_price"),
        ("buy_price", 0.0, "buy_price"),
        ("buy_price", -1.0, "buy_price"),
        ("sell_price", 0.0, "sell_price"),
        ("profit_rate", float("inf"), "profit_rate"),
        ("sell_time", "", "sell_time"),
        ("sell_reason", "", "sell_reason"),
    ],
)
def test_strict_active_decoder_rejects_malformed_fields_without_writes(
    tmp_path,
    field_name,
    invalid_value,
    error_field,
):
    path = tmp_path / f"invalid-{field_name}.sqlite3"
    initial = TradeLogger(path)
    initial.close()
    _insert_position_row(path, **{field_name: invalid_value})
    before = _old_schema_snapshot(path)

    with pytest.raises(PaperTradePersistenceError, match=error_field):
        TradeLogger(path)

    assert _old_schema_snapshot(path) == before


@pytest.mark.parametrize("force_name", tuple(VALID_FORCES))
@pytest.mark.parametrize("invalid_value", [None, "not-numeric", float("inf")])
def test_strict_active_decoder_rejects_each_malformed_force(
    tmp_path,
    force_name,
    invalid_value,
):
    path = tmp_path / f"invalid-{force_name}.sqlite3"
    initial = TradeLogger(path)
    initial.close()
    _insert_position_row(path, **{force_name: invalid_value})
    before = _row(path)

    with pytest.raises(PaperTradePersistenceError, match=force_name):
        TradeLogger(path)

    assert _row(path) == before


def test_strict_active_decoder_rejects_duplicate_code_before_manager_memory(tmp_path):
    path = tmp_path / "duplicate.sqlite3"
    initial = TradeLogger(path)
    initial.close()
    _insert_position_row(path, stock_code="DUPLICATE")
    _insert_position_row(path, stock_code="DUPLICATE")
    before = _old_schema_snapshot(path)

    with pytest.raises(PaperTradePersistenceError, match="duplicate"):
        TradeLogger(path)

    assert _old_schema_snapshot(path) == before


def test_valid_rows_before_final_malformed_row_publish_no_partial_mapping(tmp_path):
    path = tmp_path / "last-row-malformed.sqlite3"
    initial = TradeLogger(path)
    initial.close()
    _insert_position_row(path, stock_code="VALID")
    _insert_position_row(path, stock_code="MALFORMED", thrust=None)
    before = _old_schema_snapshot(path)

    with pytest.raises(PaperTradePersistenceError, match="thrust"):
        TradeLogger(path)

    assert _old_schema_snapshot(path) == before


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("id", True),
        ("id", 1.0),
        ("id", "1"),
        ("buy_price", True),
        ("thrust", True),
    ],
)
def test_private_active_decoder_rejects_bool_and_coerced_types(
    field_name,
    invalid_value,
):
    raw = {
        "id": 1,
        "stock_code": "005930",
        "stock_name": "Samsung",
        "buy_price": 10_000.0,
        **VALID_FORCES,
        "buy_time": "2026-08-03 09:30:00",
        "buy_regime": "STABLE_BULL",
        "sell_price": None,
        "profit_rate": None,
        "sell_time": None,
        "sell_reason": None,
        "status": "OPEN",
        "owning_session_date": "2026-08-03",
        "state_changed_at": "2026-08-03T09:30:00+09:00",
        field_name: invalid_value,
    }
    decoder = TradeLogger.__new__(TradeLogger)

    with pytest.raises(PaperTradePersistenceError, match=field_name):
        decoder._decode_active_row(raw)


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("buy_price", True),
        ("buy_price", float("nan")),
        ("thrust", True),
        ("gravity", "-0.2"),
    ],
)
def test_record_buy_rejects_bool_nonfinite_and_string_before_insert(
    tmp_path,
    field_name,
    invalid_value,
):
    path = tmp_path / f"invalid-write-{field_name}.sqlite3"
    ledger = TradeLogger(path)
    payload = {
        "stock_code": "005930",
        "stock_name": "Samsung",
        "buy_price": 10_000.0,
        "buy_time": "2026-08-03 09:30:00",
        "buy_regime": "STABLE_BULL",
        **VALID_FORCES,
        field_name: invalid_value,
    }
    try:
        with pytest.raises(PaperTradePersistenceError, match=field_name):
            ledger.record_buy(payload)
        assert ledger.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 0
    finally:
        ledger.close()


def test_direct_ledger_and_manager_reject_overnight_close_without_mutation(tmp_path):
    path = tmp_path / "overnight-close.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, position = _seed_overnight(path, clock, guard)
    guard.reset_mock()
    prior_row = _row(path)
    prior_memory = vars(position).copy()
    candidate = replace(position, sell_price=10_300.0, sell_reason="forbidden")

    with pytest.raises(PaperTradePersistenceError, match="only OPEN"):
        ledger.record_sell(candidate)
    with pytest.raises(PaperTradePersistenceError, match="only OPEN"):
        manager.apply_paper_sell(
            {"stock_code": "005930", "price": 10_300.0},
            "forbidden",
        )

    assert _row(path) == prior_row
    assert vars(position) == prior_memory
    assert manager.active_positions == {"005930": position}
    guard.assert_not_called()
    ledger.close()


def test_engine_rejects_adversarial_overnight_sell_before_any_mutation(tmp_path):
    path = tmp_path / "engine-overnight-close.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger, manager, position = _seed_overnight(path, clock, guard)
    guard.reset_mock()
    before_row = _row(path)
    before_memory = vars(position).copy()
    engine = TradingEngine.__new__(TradingEngine)
    engine.stock_mgr = manager
    engine.strategy = MagicMock()
    engine.strategy.decide_position.return_value = PositionDecisionResult(
        PositionDecision.SELL,
        "adversarial",
    )
    engine.notifier = MagicMock()

    with pytest.raises(PaperTradePersistenceError, match="reconcile to OPEN"):
        engine._process_decisions(
            [
                {
                    "stock_code": "005930",
                    "price": 10_300.0,
                    "atr_percent": 0.5,
                    "down_atr_percent": 0.5,
                    "forces": {},
                }
            ]
        )

    assert _row(path) == before_row
    assert vars(position) == before_memory
    guard.assert_not_called()
    engine.notifier.collect_status.assert_not_called()
    ledger.close()


def test_next_session_reopen_receipt_precedes_open_only_close_receipt(tmp_path):
    path = tmp_path / "reopen-then-close.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    ledger, manager, position = _seed_overnight(path, clock, MagicMock())
    clock.value = datetime(2026, 8, 4, 9, 0, tzinfo=KST)
    receipts = []
    original_reopen = ledger.reopen_position
    original_sell = ledger.record_sell

    def capture_reopen(*args, **kwargs):
        receipt = original_reopen(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    def capture_sell(*args, **kwargs):
        receipt = original_sell(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    ledger.reopen_position = capture_reopen
    ledger.record_sell = capture_sell

    assert manager.reconcile_overnight_positions() == 1
    success, sold = manager.apply_paper_sell(
        {"stock_code": "005930", "price": 10_300.0},
        "Fixed Target",
    )

    assert success is True
    assert sold is position
    assert [
        (receipt.previous_status, receipt.status) for receipt in receipts
    ] == [
        (PositionStatus.OVERNIGHT, PositionStatus.OPEN),
        (PositionStatus.OPEN, PositionStatus.CLOSED),
    ]
    assert _row(path)["status"] == "CLOSED"
    assert manager.active_positions == {}
    ledger.close()


def test_open_memory_and_overnight_database_race_rejects_close_receipt(tmp_path):
    path = tmp_path / "sell-status-race.sqlite3"
    clock = MutableClock(datetime(2026, 8, 3, 10, 0, tzinfo=KST))
    guard = MagicMock()
    ledger = TradeLogger(path, clock=clock)
    manager = _manager(ledger, clock, guard)
    position = _buy(manager)
    guard.reset_mock()
    ledger.conn.execute(
        "UPDATE trades SET status = 'OVERNIGHT' WHERE id = ?",
        (position.id,),
    )
    ledger.conn.commit()
    before_row = _row(path)
    before_memory = vars(position).copy()

    with pytest.raises(PaperTradePersistenceError, match="identity/status mismatch"):
        manager.apply_paper_sell(
            {"stock_code": "005930", "price": 10_300.0},
            "race",
        )

    assert _row(path) == before_row
    assert vars(position) == before_memory
    assert manager.active_positions == {"005930": position}
    guard.assert_called_once_with()
    ledger.close()
