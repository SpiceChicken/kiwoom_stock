import json
import os
from pathlib import Path
import sqlite3
from unittest.mock import MagicMock

import pytest

from test_overnight_lifecycle import (
    _file_evidence,
    _insert_position_row,
    _old_trades_schema,
    _read_noatime,
    _run_downgrade_preflight,
    _wal_target,
    _without_ctime,
    database_module,
)
from kiwoom_stock.core.database import TradeLogger


@pytest.mark.parametrize(
    ("overnight_count", "expected_status", "expected_exit"),
    [(0, "PASS", 0), (2, "BLOCKED", 2)],
)
def test_downgrade_preflight_cli_is_read_only_and_reports_exact_count(
    tmp_path,
    overnight_count,
    expected_status,
    expected_exit,
):
    path = (tmp_path / "rollback-target.sqlite3").resolve()
    _old_trades_schema(path)
    _insert_position_row(path, stock_code="OPEN", include_lifecycle=False)
    for index in range(overnight_count):
        _insert_position_row(
            path,
            stock_code=f"OVERNIGHT-{index}",
            status="OVERNIGHT",
            include_lifecycle=False,
        )
    before = _file_evidence(path)

    completed = _run_downgrade_preflight(path, tmp_path)

    assert completed.returncode == expected_exit
    assert completed.stderr == ""
    assert len(completed.stdout.splitlines()) == 1
    assert json.loads(completed.stdout) == {
        "active_overnight_count": overnight_count,
        "check": "overnight-downgrade-preflight",
        "database_identity": str(path),
        "database_writes": 0,
        "failure_category": None,
        "read_only": True,
        "schema_version": 1,
        "status": expected_status,
    }
    assert _file_evidence(path) == before
    assert not (tmp_path / "trades.db").exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    ("case", "expected_category"),
    [
        ("missing", "FILE_MISSING"),
        ("missing-table", "SCHEMA_MISSING"),
        ("wrong-status-shape", "SCHEMA_MALFORMED"),
        ("corrupt", "OPEN_OR_QUERY_FAILED"),
    ],
)
def test_downgrade_preflight_failures_are_sanitized_and_create_nothing(
    tmp_path,
    case,
    expected_category,
):
    path = (tmp_path / f"{case}.sqlite3").resolve()
    if case == "missing-table":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
    elif case == "wrong-status-shape":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE trades (status INTEGER)")
    elif case == "corrupt":
        path.write_bytes(b"not a sqlite database")
    existed_before = path.exists()
    family_before = _file_evidence(path)

    completed = _run_downgrade_preflight(path, tmp_path)

    assert completed.returncode == 1
    assert completed.stderr == ""
    payload = json.loads(completed.stdout)
    assert payload["status"] == "FAILED"
    assert payload["active_overnight_count"] is None
    assert payload["failure_category"] == expected_category
    assert payload["read_only"] is True
    assert payload["database_writes"] == 0
    assert path.exists() is existed_before
    assert _file_evidence(path) == family_before
    assert not (tmp_path / "trades.db").exists()


def test_downgrade_preflight_rejects_relative_path_without_default_fallback(tmp_path):
    relative = Path("relative.sqlite3")

    completed = _run_downgrade_preflight(relative, tmp_path)

    assert completed.returncode == 1
    assert json.loads(completed.stdout)["failure_category"] == "INVALID_PATH"
    assert not (tmp_path / relative).exists()
    assert not (tmp_path / "trades.db").exists()


def test_downgrade_preflight_ignores_source_lock_and_reads_committed_snapshot(tmp_path):
    path = (tmp_path / "locked.sqlite3").resolve()
    _old_trades_schema(path)
    locker = sqlite3.connect(path)
    try:
        locker.execute("BEGIN EXCLUSIVE")
        before = _file_evidence(path)
        completed = _run_downgrade_preflight(path, tmp_path)
    finally:
        locker.rollback()
        locker.close()

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    assert payload["status"] == "PASS"
    assert payload["failure_category"] is None
    assert _file_evidence(path) == before


def test_downgrade_preflight_fails_source_busy_for_real_rollback_journal_spill(
    monkeypatch,
    tmp_path,
):
    path = (tmp_path / "rollback-journal.sqlite3").resolve()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size = 1024")
    assert connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    connection.execute("PRAGMA cache_size = 5")
    connection.execute("PRAGMA cache_spill = ON")
    connection.execute("CREATE TABLE trades (status TEXT, payload BLOB)")
    connection.executemany(
        "INSERT INTO trades (status, payload) VALUES (?, ?)",
        [
            ("OVERNIGHT" if index == 0 else "OPEN", bytes([index % 251]) * 700)
            for index in range(400)
        ],
    )
    connection.commit()
    committed_main = _read_noatime(path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE trades SET status = 'OPEN', payload = zeroblob(900)"
    )
    journal_path = Path(str(path) + "-journal")
    assert journal_path.exists()
    assert _read_noatime(path) != committed_main
    before = _file_evidence(path)
    sqlite_opens = MagicMock()

    try:
        completed = _run_downgrade_preflight(path, tmp_path)
        assert completed.returncode == 1
        assert completed.stderr == ""
        assert json.loads(completed.stdout) == {
            "active_overnight_count": None,
            "check": "overnight-downgrade-preflight",
            "database_identity": str(path),
            "database_writes": 0,
            "failure_category": "SOURCE_BUSY",
            "read_only": True,
            "schema_version": 1,
            "status": "FAILED",
        }
        assert _file_evidence(path) == before

        monkeypatch.setattr(database_module.sqlite3, "connect", sqlite_opens)
        evidence = TradeLogger.inspect_overnight_downgrade(path)

        assert evidence.status == "FAILED"
        assert evidence.active_overnight_count is None
        assert evidence.failure_category == "SOURCE_BUSY"
        assert evidence.exit_code == 1
        assert _file_evidence(path) == before
        sqlite_opens.assert_not_called()
    finally:
        connection.rollback()
        connection.close()


def test_downgrade_preflight_discards_transient_journal_race(
    monkeypatch,
    tmp_path,
):
    path = (tmp_path / "journal-race.sqlite3").resolve()
    _old_trades_schema(path)
    before = _file_evidence(path)
    journal_path = Path(str(path) + "-journal")
    real_snapshot = database_module._snapshot_source_family
    calls = 0

    def racing_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_snapshot(*args, **kwargs)
        journal_path.write_bytes(b"transient-journal")
        try:
            return real_snapshot(*args, **kwargs)
        finally:
            journal_path.unlink()

    monkeypatch.setattr(database_module, "_snapshot_source_family", racing_snapshot)
    evidence = TradeLogger.inspect_overnight_downgrade(path)

    assert evidence.status == "FAILED"
    assert evidence.failure_category == "SOURCE_CHANGED"
    assert _file_evidence(path) == before


@pytest.mark.parametrize(
    ("statuses", "expected_status", "expected_count"),
    [(["OPEN"], "PASS", 0), (["OPEN", "OVERNIGHT", "OVERNIGHT"], "BLOCKED", 2)],
)
@pytest.mark.parametrize("source_shm_present", [False, True])
def test_downgrade_preflight_reads_wal_only_rows_without_changing_source_family(
    monkeypatch,
    tmp_path,
    statuses,
    expected_status,
    expected_count,
    source_shm_present,
):
    live_path = (tmp_path / "live-wal.sqlite3").resolve()
    live_connection = _wal_target(live_path, statuses)
    target = live_path
    if not source_shm_present:
        target = (tmp_path / "wal-without-shm.sqlite3").resolve()
        target.write_bytes(live_path.read_bytes())
        Path(str(target) + "-wal").write_bytes(
            Path(str(live_path) + "-wal").read_bytes()
        )
        assert not Path(str(target) + "-shm").exists()
    else:
        assert Path(str(target) + "-shm").exists()

    before = _file_evidence(target)
    real_connect = database_module.sqlite3.connect
    sqlite_paths = []

    def recording_connect(database, *args, **kwargs):
        sqlite_paths.append(Path(os.fspath(database)).resolve())
        return real_connect(database, *args, **kwargs)

    try:
        monkeypatch.setattr(database_module.sqlite3, "connect", recording_connect)
        evidence = TradeLogger.inspect_overnight_downgrade(target)
        assert evidence.status == expected_status
        assert evidence.active_overnight_count == expected_count
        assert _file_evidence(target) == before
        assert sqlite_paths
        assert all(path not in {target, Path(str(target) + "-wal"), Path(str(target) + "-shm")} for path in sqlite_paths)
        temporary_parents = {path.parent for path in sqlite_paths}
        assert len(temporary_parents) == 1
        assert not next(iter(temporary_parents)).exists()
    finally:
        live_connection.close()


@pytest.mark.parametrize("unsafe_kind", ["main-symlink", "wal-symlink", "shm-directory"])
def test_downgrade_preflight_rejects_unsafe_source_family_without_changes(
    tmp_path,
    unsafe_kind,
):
    path = (tmp_path / "unsafe.sqlite3").resolve()
    if unsafe_kind == "main-symlink":
        real = tmp_path / "real.sqlite3"
        _old_trades_schema(real)
        path.symlink_to(real)
    else:
        _old_trades_schema(path)
        unsafe = Path(str(path) + ("-wal" if unsafe_kind == "wal-symlink" else "-shm"))
        if unsafe_kind == "wal-symlink":
            unsafe.symlink_to(path)
        else:
            unsafe.mkdir()
    before = tuple(sorted(
        (candidate.name, candidate.lstat())
        for candidate in tmp_path.iterdir()
    ))

    evidence = TradeLogger.inspect_overnight_downgrade(path)

    assert evidence.status == "FAILED"
    assert evidence.failure_category == "UNSAFE_FILE_TYPE"
    assert tuple(sorted(
        (candidate.name, candidate.lstat())
        for candidate in tmp_path.iterdir()
    )) == before


def test_downgrade_preflight_fails_closed_when_noatime_is_unavailable(
    monkeypatch,
    tmp_path,
):
    path = (tmp_path / "no-atime.sqlite3").resolve()
    _old_trades_schema(path)
    before = _file_evidence(path)

    with monkeypatch.context() as scoped:
        scoped.delattr(database_module.os, "O_NOATIME")
        evidence = TradeLogger.inspect_overnight_downgrade(path)

    assert evidence.status == "FAILED"
    assert evidence.failure_category == "NOATIME_UNAVAILABLE"
    assert _file_evidence(path) == before


@pytest.mark.parametrize("race_kind", ["change", "create", "delete"])
def test_downgrade_preflight_detects_source_family_race_and_restores_fixture(
    monkeypatch,
    tmp_path,
    race_kind,
):
    path = (tmp_path / "source-race.sqlite3").resolve()
    _old_trades_schema(path)
    wal_path = Path(str(path) + "-wal")
    if race_kind == "delete":
        wal_path.write_bytes(b"existing-sidecar")
    before = _file_evidence(path)
    real_snapshot = database_module._snapshot_source_family
    calls = 0

    def racing_snapshot(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls != 2:
            return real_snapshot(*args, **kwargs)
        if race_kind == "change":
            original_stat = path.stat()
            original = _read_noatime(path)
            path.write_bytes(original + b"race")
            changed = real_snapshot(*args, **kwargs)
            path.write_bytes(original)
            os.utime(
                path,
                ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
            )
            return changed
        if race_kind == "create":
            shm_path = Path(str(path) + "-shm")
            shm_path.write_bytes(b"created-sidecar")
            changed = real_snapshot(*args, **kwargs)
            shm_path.unlink()
            return changed
        parked_wal = tmp_path / "parked-wal"
        wal_path.rename(parked_wal)
        changed = real_snapshot(*args, **kwargs)
        parked_wal.rename(wal_path)
        return changed

    monkeypatch.setattr(database_module, "_snapshot_source_family", racing_snapshot)
    evidence = TradeLogger.inspect_overnight_downgrade(path)

    assert evidence.to_safe_dict()["failure_category"] == "SOURCE_CHANGED"
    assert evidence.status == "FAILED"
    assert _without_ctime(_file_evidence(path)) == _without_ctime(before)
