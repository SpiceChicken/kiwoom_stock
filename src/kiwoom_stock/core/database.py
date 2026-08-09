# src/kiwoom_stock/core/database.py
import copy
from dataclasses import dataclass, field
from datetime import date, datetime
import errno
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import stat
import sqlite3
import tempfile
import threading
import warnings
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple, Union

from kiwoom_stock.application.ports import (
    PaperTradePersistenceError,
    PhysicalStateCommitUnknownError,
    PhysicalStatePersistenceError,
    PositionTransitionReceipt,
)
from kiwoom_stock.domain.models import Position, PositionStatus
from kiwoom_stock.domain.state import (
    PHYSICAL_TRACKER_SCHEMA_VERSION,
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
    PhysicalStateValidationError,
    PhysicalStateWrite,
    PhysicalTrackerState,
)
from kiwoom_stock.utils.market_cal import (
    KST,
    KrxCalendarError,
    is_krx_session,
    require_aware_kst,
    seoul_now,
)

logger = logging.getLogger(__name__)


class TradeLoggerLifecycleError(RuntimeError):
    """TradeLogger could not complete one or more shutdown phases."""


@dataclass
class _PhysicalStateCompletion:
    event: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    receipt: Optional[PhysicalStateBatchCommitReceipt] = None
    error: Optional[PhysicalStatePersistenceError] = None


@dataclass(frozen=True)
class _PhysicalStateTaskItem:
    stock_code: str
    forces: Tuple[Tuple[str, Any], ...]
    timestamp_str: str
    tracker_state: Optional[PhysicalTrackerState] = None


@dataclass(frozen=True)
class _PhysicalStateAttestationItem:
    stock_code: str
    generation: str
    committed_at: datetime
    legacy_timestamp: str


@dataclass(frozen=True)
class _PhysicalStateBatchAttestation:
    generation: str
    committed_at: datetime
    items: Tuple[_PhysicalStateAttestationItem, ...]


@dataclass(frozen=True)
class _PhysicalStateTask:
    items: Tuple[_PhysicalStateTaskItem, ...]
    completion: Optional[_PhysicalStateCompletion] = None
    attestation: Optional[_PhysicalStateBatchAttestation] = None


_QUEUE_SENTINEL = object()
_PHYSICAL_ACK_TIMEOUT_SECONDS = 5.0
_ACTIVE_FORCE_FIELDS = (
    "thrust",
    "gravity",
    "drag",
    "magnetic",
    "jerk",
    "impulse",
    "net_force",
)


@dataclass(frozen=True)
class OvernightDowngradePreflightEvidence:
    """Sanitized result of inspecting one exact SQLite file without writes."""

    status: str
    database_identity: Optional[str]
    active_overnight_count: Optional[int]
    failure_category: Optional[str] = None
    schema_version: int = 1
    check: str = "overnight-downgrade-preflight"
    read_only: bool = True
    database_writes: int = 0

    def __post_init__(self) -> None:
        if self.status not in {"PASS", "BLOCKED", "FAILED"}:
            raise ValueError("unsupported downgrade preflight status")
        if self.status in {"PASS", "BLOCKED"}:
            if (
                type(self.active_overnight_count) is not int
                or self.active_overnight_count < 0
                or self.failure_category is not None
            ):
                raise ValueError("invalid successful downgrade preflight evidence")
        elif self.active_overnight_count is not None or not self.failure_category:
            raise ValueError("invalid failed downgrade preflight evidence")

    @property
    def exit_code(self) -> int:
        if self.status == "PASS":
            return 0
        if self.status == "BLOCKED":
            return 2
        return 1

    def to_safe_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "check": self.check,
            "status": self.status,
            "database_identity": self.database_identity,
            "active_overnight_count": self.active_overnight_count,
            "read_only": self.read_only,
            "database_writes": self.database_writes,
            "failure_category": self.failure_category,
        }


class _SourceSnapshotError(RuntimeError):
    """Bounded internal failure for source-family snapshot attestation."""

    def __init__(self, category: str):
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class _SourceFileSnapshot:
    metadata: Tuple[int, int, int, int, int, int, int, int, int]
    digest: str
    content: bytes


def _source_file_metadata(
    metadata: os.stat_result,
) -> Tuple[int, int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_atime_ns,
        metadata.st_ctime_ns,
    )


def _snapshot_source_family(
    main_path: Path,
    *,
    missing_main_category: str,
) -> Dict[str, _SourceFileSnapshot]:
    """Read an SQLite source family without following links or changing atime."""

    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_NOATIME"):
        raise _SourceSnapshotError("NOATIME_UNAVAILABLE")
    names = (
        main_path.name,
        main_path.name + "-wal",
        main_path.name + "-shm",
        main_path.name + "-journal",
    )
    expected: Dict[str, os.stat_result] = {}
    for index, name in enumerate(names):
        candidate = main_path.parent / name
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            if index == 0:
                raise _SourceSnapshotError(missing_main_category)
            continue
        except OSError:
            raise _SourceSnapshotError("SOURCE_CHANGED")
        if name == main_path.name + "-journal":
            raise _SourceSnapshotError("SOURCE_BUSY")
        if stat.S_ISLNK(metadata.st_mode):
            raise _SourceSnapshotError("UNSAFE_FILE_TYPE")
        if not stat.S_ISREG(metadata.st_mode):
            category = "NOT_REGULAR_FILE" if index == 0 else "UNSAFE_FILE_TYPE"
            raise _SourceSnapshotError(category)
        expected[name] = metadata

    snapshots: Dict[str, _SourceFileSnapshot] = {}
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NOATIME
    flags |= getattr(os, "O_CLOEXEC", 0)
    for name in names:
        expected_metadata = expected.get(name)
        if expected_metadata is None:
            continue
        candidate = main_path.parent / name
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(candidate, flags)
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or _source_file_metadata(opened_metadata)
                != _source_file_metadata(expected_metadata)
            ):
                raise _SourceSnapshotError("SOURCE_CHANGED")
            chunks = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            completed_metadata = os.fstat(descriptor)
            if (
                _source_file_metadata(completed_metadata)
                != _source_file_metadata(opened_metadata)
                or len(content) != completed_metadata.st_size
            ):
                raise _SourceSnapshotError("SOURCE_CHANGED")
        except _SourceSnapshotError:
            raise
        except OSError as error:
            if error.errno in {errno.EPERM, errno.EACCES}:
                raise _SourceSnapshotError("NOATIME_UNAVAILABLE")
            raise _SourceSnapshotError("SOURCE_CHANGED")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        try:
            final_metadata = candidate.lstat()
        except OSError:
            raise _SourceSnapshotError("SOURCE_CHANGED")
        if _source_file_metadata(final_metadata) != _source_file_metadata(completed_metadata):
            raise _SourceSnapshotError("SOURCE_CHANGED")
        snapshots[name] = _SourceFileSnapshot(
            metadata=_source_file_metadata(completed_metadata),
            digest=hashlib.sha256(content).hexdigest(),
            content=content,
        )

    present_after = set()
    for name in names:
        try:
            (main_path.parent / name).lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise _SourceSnapshotError("SOURCE_CHANGED")
        present_after.add(name)
    if present_after != set(expected):
        raise _SourceSnapshotError("SOURCE_CHANGED")
    return snapshots


def _same_source_generation(
    before: Mapping[str, _SourceFileSnapshot],
    after: Mapping[str, _SourceFileSnapshot],
) -> bool:
    return {
        name: (item.metadata, item.digest) for name, item in before.items()
    } == {
        name: (item.metadata, item.digest) for name, item in after.items()
    }


class TradeLogger:
    @staticmethod
    def inspect_overnight_downgrade(
        database_path: Path,
    ) -> OvernightDowngradePreflightEvidence:
        """Inspect a byte snapshot without opening the source family in SQLite."""

        if not isinstance(database_path, Path) or not database_path.is_absolute():
            return OvernightDowngradePreflightEvidence(
                "FAILED", None, None, "INVALID_PATH"
            )
        canonical = Path(os.path.abspath(os.fspath(database_path)))
        identity = str(canonical)
        try:
            before = _snapshot_source_family(
                canonical,
                missing_main_category="FILE_MISSING",
            )
        except _SourceSnapshotError as error:
            return OvernightDowngradePreflightEvidence(
                "FAILED", identity, None, error.category
            )

        result_status = "FAILED"
        result_count: Optional[int] = None
        failure_category: Optional[str] = "OPEN_OR_QUERY_FAILED"
        try:
            with tempfile.TemporaryDirectory(prefix="kiwoom-downgrade-") as directory:
                snapshot_main = Path(directory) / canonical.name
                snapshot_main.write_bytes(before[canonical.name].content)
                wal = before.get(canonical.name + "-wal")
                if wal is not None:
                    Path(str(snapshot_main) + "-wal").write_bytes(wal.content)
                connection: Optional[sqlite3.Connection] = None
                try:
                    connection = sqlite3.connect(snapshot_main, timeout=0.0)
                    table = connection.execute(
                        "SELECT type FROM sqlite_master WHERE name = 'trades'"
                    ).fetchone()
                    if table is None:
                        failure_category = "SCHEMA_MISSING"
                    elif table[0] != "table":
                        failure_category = "SCHEMA_MALFORMED"
                    else:
                        status_columns = [
                            row
                            for row in connection.execute("PRAGMA table_info(trades)")
                            if row[1] == "status"
                        ]
                        if len(status_columns) != 1 or status_columns[0][2] != "TEXT":
                            failure_category = "SCHEMA_MALFORMED"
                        else:
                            count_row = connection.execute(
                                "SELECT COUNT(*) FROM trades WHERE status = 'OVERNIGHT'"
                            ).fetchone()
                            if (
                                count_row is None
                                or type(count_row[0]) is not int
                                or count_row[0] < 0
                            ):
                                failure_category = "QUERY_FAILED"
                            else:
                                result_count = count_row[0]
                                result_status = "PASS" if result_count == 0 else "BLOCKED"
                                failure_category = None
                except sqlite3.Error:
                    failure_category = "OPEN_OR_QUERY_FAILED"
                finally:
                    if connection is not None:
                        connection.close()
        except OSError:
            result_status = "FAILED"
            result_count = None
            failure_category = "OPEN_OR_QUERY_FAILED"

        try:
            after = _snapshot_source_family(
                canonical,
                missing_main_category="SOURCE_CHANGED",
            )
        except _SourceSnapshotError:
            return OvernightDowngradePreflightEvidence(
                "FAILED", identity, None, "SOURCE_CHANGED"
            )
        if not _same_source_generation(before, after):
            return OvernightDowngradePreflightEvidence(
                "FAILED", identity, None, "SOURCE_CHANGED"
            )
        return OvernightDowngradePreflightEvidence(
            result_status,
            identity,
            result_count,
            failure_category,
        )

    def __init__(
        self,
        db_name: Union[str, os.PathLike[str]] = "trades.db",
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.db_path = os.path.normpath(os.path.abspath(os.fspath(db_name)))
        self._clock = clock
        self._async_queue: queue.Queue[object] = queue.Queue()
        self._state_lock = threading.Lock()
        self._close_complete = threading.Event()
        self._accepting_submissions = True
        self._closing = False
        self._closed = False
        self._worker_failure: Optional[Tuple[str, str]] = None
        self._close_failure: Optional[Tuple[str, BaseException]] = None
        self._sentinel_enqueued = False
        self._main_connection_closed = False
        self._worker_connection_closed = False
        self._shutdown_deadline: Optional[Callable[[], float]] = None

        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        worker_conn: Optional[sqlite3.Connection] = None
        try:
            self._create_table()
            worker_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._worker_conn = worker_conn
            self._worker_thread = threading.Thread(
                target=self._async_worker,
                name="TradeLoggerPhysicalState",
                daemon=True,
            )
            self._worker_thread.start()
        except BaseException:
            if worker_conn is not None:
                worker_conn.close()
            self.conn.close()
            raise

    def _now(self) -> datetime:
        clock = getattr(self, '_clock', None)
        return clock() if clock is not None else seoul_now()

    def _state_now(self) -> datetime:
        try:
            return require_aware_kst(self._now(), "paper state clock")
        except KrxCalendarError as error:
            raise PaperTradePersistenceError(str(error)) from error

    def _create_table(self):
        """Atomically initialize schema and strict legacy lifecycle metadata."""
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
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(query_trades)
            self.conn.execute(query_physics)
            self.conn.execute(query_tracker_v1)
            trade_columns = {
                row[1]: row
                for row in self.conn.execute("PRAGMA table_info(trades)")
            }
            for column in ("owning_session_date", "state_changed_at"):
                existing = trade_columns.get(column)
                if existing is not None and existing[2] != "TEXT":
                    raise PaperTradePersistenceError(
                        f"trades.{column} must have TEXT affinity"
                    )
            for column in ("owning_session_date", "state_changed_at"):
                if column not in trade_columns:
                    self.conn.execute(
                        f"ALTER TABLE trades ADD COLUMN {column} TEXT"
                    )
            existing_tracker_columns = {
                row[1]
                for row in self.conn.execute(
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
                    self.conn.execute(
                        f"ALTER TABLE physical_tracker_state_v1 ADD COLUMN {column} REAL"
                    )

            _, backfills = self._validated_active_rows()
            if backfills:
                cursor = self.conn.executemany(
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
            self._verify_lifecycle_shape()
            _, pending_backfills = self._validated_active_rows()
            if pending_backfills:
                raise PaperTradePersistenceError(
                    "legacy OPEN metadata backfill verification failed"
                )
            self.conn.commit()
        except Exception as error:
            self.conn.rollback()
            if isinstance(error, PaperTradePersistenceError):
                raise
            raise PaperTradePersistenceError(
                "paper ledger initialization failed"
            ) from error

    # =========================================================
    # 비동기 물리 상태 백업 (L2 Backup)
    # =========================================================
    def _async_worker(self) -> None:
        try:
            while True:
                task = self._async_queue.get()
                try:
                    if task is _QUEUE_SENTINEL:
                        return
                    if not isinstance(task, _PhysicalStateTask):
                        raise TypeError("physical-state queue received an invalid task")
                    self._raise_worker_failure()
                    receipt = self._persist_physical_task(task)
                    if task.completion is not None:
                        with task.completion.lock:
                            task.completion.receipt = receipt
                except Exception as error:
                    try:
                        self._worker_conn.rollback()
                    except Exception:
                        pass
                    failure = self._record_worker_failure(error)
                    if (
                        isinstance(task, _PhysicalStateTask)
                        and task.completion is not None
                    ):
                        with task.completion.lock:
                            task.completion.error = failure
                    logger.error(f"비동기 DB 로깅 실패: {error}")
                finally:
                    if (
                        isinstance(task, _PhysicalStateTask)
                        and task.completion is not None
                    ):
                        task.completion.event.set()
                    self._async_queue.task_done()
        except BaseException as error:
            self._record_close_failure("physical-state worker", error)
        finally:
            try:
                self._worker_conn.close()
            except BaseException as error:
                self._record_close_failure("worker connection close", error)
            else:
                with self._state_lock:
                    self._worker_connection_closed = True

    def _persist_physical_task(
        self,
        task: _PhysicalStateTask,
    ) -> Optional[PhysicalStateBatchCommitReceipt]:
        if not task.items:
            raise PhysicalStateValidationError("physical-state task batch is empty")
        typed_task = self._validate_physical_task_attestation(task)
        self._worker_conn.execute("BEGIN IMMEDIATE")
        receipts = []
        for item in task.items:
            receipt = self._persist_physical_item(item)
            if receipt is not None:
                receipts.append(receipt)
        batch_receipt: Optional[PhysicalStateBatchCommitReceipt] = None
        if typed_task:
            if len(receipts) != len(task.items):
                raise PhysicalStateValidationError(
                    "physical-state task mixed legacy and typed writes"
                )
            batch_receipt = PhysicalStateBatchCommitReceipt(
                generation=receipts[0].generation,
                items=tuple(receipts),
                committed_at=receipts[0].committed_at,
            )
            self._validate_precommit_receipt(task, batch_receipt)
        elif receipts:
            raise PhysicalStateValidationError(
                "legacy physical-state task produced a typed receipt"
            )
        self._worker_conn.commit()
        return batch_receipt

    @staticmethod
    def _validate_physical_task_attestation(task: _PhysicalStateTask) -> bool:
        """Validate expected typed writes independently before opening SQL work."""

        typed_items = tuple(item.tracker_state is not None for item in task.items)
        if any(typed_items) and not all(typed_items):
            raise PhysicalStateValidationError(
                "physical-state task mixed legacy and typed writes"
            )
        typed_task = all(typed_items)
        if not typed_task:
            if task.attestation is not None:
                raise PhysicalStateValidationError(
                    "legacy physical-state task cannot carry typed attestation"
                )
            return False

        attestation = task.attestation
        if attestation is None or len(attestation.items) != len(task.items):
            raise PhysicalStateValidationError(
                "typed physical-state task attestation is incomplete"
            )
        derived_items = []
        for item in task.items:
            state = item.tracker_state
            assert state is not None
            state.assert_persistable()
            assert state.last_observed_at is not None
            if item.stock_code != state.stock_code:
                raise PhysicalStateValidationError(
                    "physical-state task stock code does not match staged state"
                )
            derived_items.append(
                _PhysicalStateAttestationItem(
                    stock_code=state.stock_code,
                    generation=state.last_observed_at.isoformat(),
                    committed_at=state.updated_at,
                    legacy_timestamp=state.updated_at.strftime(
                        "%Y-%m-%d %H:%M:%S.%f"
                    ),
                )
            )
            if item.timestamp_str != derived_items[-1].legacy_timestamp:
                raise PhysicalStateValidationError(
                    "physical-state task legacy timestamp is inconsistent"
                )
        derived = tuple(derived_items)
        if (
            derived != attestation.items
            or attestation.generation != derived[0].generation
            or attestation.committed_at != derived[0].committed_at
            or any(
                expected.generation != attestation.generation
                or expected.committed_at != attestation.committed_at
                for expected in derived
            )
        ):
            raise PhysicalStateValidationError(
                "physical-state task attestation does not match staged writes"
            )
        return True

    @staticmethod
    def _validate_precommit_receipt(
        task: _PhysicalStateTask,
        receipt: PhysicalStateBatchCommitReceipt,
    ) -> None:
        """Reject any coherent or partial false receipt before SQLite commit."""

        attestation = task.attestation
        assert attestation is not None
        actual_items = tuple(
            _PhysicalStateAttestationItem(
                stock_code=item.stock_code,
                generation=item.generation,
                committed_at=item.committed_at,
                legacy_timestamp=task.items[index].timestamp_str,
            )
            for index, item in enumerate(receipt.items)
        )
        if (
            receipt.generation != attestation.generation
            or receipt.committed_at != attestation.committed_at
            or actual_items != attestation.items
        ):
            raise PhysicalStateValidationError(
                "physical-state durable receipt failed precommit attestation"
            )

    def _persist_physical_item(
        self,
        item: _PhysicalStateTaskItem,
    ) -> Optional[PhysicalStateCommitReceipt]:
        forces = dict(item.forces)
        legacy_query = """
        INSERT INTO physics_state
        (stock_code, velocity, thrust, gravity, drag, magnetic, jerk, impulse,
         net_force, last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code) DO UPDATE SET
            velocity=excluded.velocity,
            thrust=excluded.thrust,
            gravity=excluded.gravity,
            drag=excluded.drag,
            magnetic=excluded.magnetic,
            jerk=excluded.jerk,
            impulse=excluded.impulse,
            net_force=excluded.net_force,
            last_updated=excluded.last_updated
        """
        legacy_params = (
            item.stock_code,
            forces["current_velocity"],
            forces["thrust"], forces["gravity"], forces["drag"],
            forces["magnetic"], forces["jerk"], forces["impulse"],
            forces["net_force"],
            item.timestamp_str,
        )
        if item.tracker_state is None:
            self._worker_conn.execute(legacy_query, legacy_params)
            return None

        state = item.tracker_state
        state.assert_persistable()
        assert state.last_observed_at is not None
        generation = state.last_observed_at.isoformat()
        canonical_query = """
        INSERT INTO physical_tracker_state_v1
        (stock_code, schema_version, velocity, last_cumulative_volume, last_price,
         interval_volume_history, last_observed_at, updated_at,
         projection_generation, projection_velocity, projection_thrust,
         projection_gravity, projection_drag, projection_magnetic,
         projection_jerk, projection_impulse, projection_net_force,
         projection_last_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code) DO UPDATE SET
            schema_version=excluded.schema_version,
            velocity=excluded.velocity,
            last_cumulative_volume=excluded.last_cumulative_volume,
            last_price=excluded.last_price,
            interval_volume_history=excluded.interval_volume_history,
            last_observed_at=excluded.last_observed_at,
            updated_at=excluded.updated_at,
            projection_generation=excluded.projection_generation,
            projection_velocity=excluded.projection_velocity,
            projection_thrust=excluded.projection_thrust,
            projection_gravity=excluded.projection_gravity,
            projection_drag=excluded.projection_drag,
            projection_magnetic=excluded.projection_magnetic,
            projection_jerk=excluded.projection_jerk,
            projection_impulse=excluded.projection_impulse,
            projection_net_force=excluded.projection_net_force,
            projection_last_updated=excluded.projection_last_updated
        """
        canonical_params = (
            state.stock_code,
            state.schema_version,
            state.velocity,
            state.last_cumulative_volume,
            state.last_price,
            json.dumps(state.interval_volume_history),
            state.last_observed_at.isoformat(),
            state.updated_at.isoformat(),
            generation,
            state.velocity,
            forces["thrust"],
            forces["gravity"],
            forces["drag"],
            forces["magnetic"],
            forces["jerk"],
            forces["impulse"],
            forces["net_force"],
            item.timestamp_str,
        )
        self._worker_conn.execute(canonical_query, canonical_params)
        self._worker_conn.execute(legacy_query, legacy_params)
        return PhysicalStateCommitReceipt(
            stock_code=state.stock_code,
            generation=generation,
            committed_at=state.updated_at,
        )

    def _record_worker_failure(
        self,
        error: Exception,
    ) -> PhysicalStatePersistenceError:
        with self._state_lock:
            if self._worker_failure is None:
                self._worker_failure = (type(error).__name__, str(error))
            error_type, message = self._worker_failure
        return PhysicalStatePersistenceError(
            f"physical-state persistence failed ({error_type}): {message}"
        )

    def _raise_worker_failure(self) -> None:
        with self._state_lock:
            failure = self._worker_failure
        if failure is not None:
            error_type, message = failure
            raise PhysicalStatePersistenceError(
                f"physical-state persistence failed ({error_type}): {message}"
            )

    def _record_close_failure(self, phase: str, error: BaseException) -> None:
        with self._state_lock:
            current = self._close_failure
            process_control = isinstance(error, (KeyboardInterrupt, SystemExit))
            current_is_process_control = (
                current is not None
                and isinstance(current[1], (KeyboardInterrupt, SystemExit))
            )
            if current is None or (process_control and not current_is_process_control):
                self._close_failure = (phase, error)

    def _raise_close_failure(self) -> None:
        with self._state_lock:
            failure = self._close_failure
        if failure is None:
            return

        phase, error = failure
        if isinstance(error, KeyboardInterrupt):
            raise KeyboardInterrupt(*error.args)
        if isinstance(error, SystemExit):
            raise SystemExit(*error.args)
        raise TradeLoggerLifecycleError(
            f"TradeLogger close failed during {phase} "
            f"({type(error).__name__}): {error}"
        ) from error

    def submit_physical_state(
        self,
        stock_code: str,
        forces: Mapping[str, Any],
    ) -> None:
        """Enqueue an immutable snapshot for the single SQLite queue worker."""
        task = _PhysicalStateTask(
            items=(
                _PhysicalStateTaskItem(
                    stock_code=stock_code,
                    forces=tuple(
                        (key, copy.deepcopy(value))
                        for key, value in dict(forces).items()
                    ),
                    timestamp_str=self._now().strftime('%Y-%m-%d %H:%M:%S.%f'),
                ),
            ),
        )
        self._enqueue_physical_task(task)

    def submit_physical_tracker_state(
        self,
        state: PhysicalTrackerState,
        forces: Mapping[str, Any],
    ) -> PhysicalStateCommitReceipt:
        """Compatibility batch-of-one durable submission."""

        write = PhysicalStateWrite(
            state=state,
            forces=tuple(dict(forces).items()),
        )
        return self.submit_physical_tracker_state_batch((write,)).items[0]

    def submit_physical_tracker_state_batch(
        self,
        writes: Sequence[PhysicalStateWrite],
    ) -> PhysicalStateBatchCommitReceipt:
        """Commit every typed member in one worker-owned SQLite transaction."""

        if not isinstance(writes, Sequence) or isinstance(
            writes,
            (str, bytes, bytearray),
        ):
            raise TypeError("physical-state writes must be a sequence")
        immutable_writes = tuple(copy.deepcopy(write) for write in writes)
        if not immutable_writes:
            raise PhysicalStateValidationError("physical-state batch is empty")
        if any(not isinstance(write, PhysicalStateWrite) for write in immutable_writes):
            raise TypeError("physical-state batch contains an invalid write")
        generations = {
            write.state.last_observed_at.isoformat()
            for write in immutable_writes
            if write.state.last_observed_at is not None
        }
        committed_at_values = {write.state.updated_at for write in immutable_writes}
        stock_codes = [write.state.stock_code for write in immutable_writes]
        if len(generations) != 1 or len(committed_at_values) != 1:
            raise PhysicalStateValidationError(
                "physical-state batch generation is inconsistent"
            )
        if len(stock_codes) != len(set(stock_codes)):
            raise PhysicalStateValidationError(
                "physical-state batch stock codes must be unique"
            )
        completion = _PhysicalStateCompletion()
        task_items = tuple(
            _PhysicalStateTaskItem(
                stock_code=write.state.stock_code,
                forces=write.forces,
                timestamp_str=write.state.updated_at.strftime(
                    '%Y-%m-%d %H:%M:%S.%f'
                ),
                tracker_state=write.state,
            )
            for write in immutable_writes
        )
        attestation_items = tuple(
            _PhysicalStateAttestationItem(
                stock_code=item.stock_code,
                generation=item.tracker_state.last_observed_at.isoformat(),
                committed_at=item.tracker_state.updated_at,
                legacy_timestamp=item.timestamp_str,
            )
            for item in task_items
            if item.tracker_state is not None
            and item.tracker_state.last_observed_at is not None
        )
        task = _PhysicalStateTask(
            items=task_items,
            attestation=_PhysicalStateBatchAttestation(
                generation=attestation_items[0].generation,
                committed_at=attestation_items[0].committed_at,
                items=attestation_items,
            ),
            completion=completion,
        )
        self._enqueue_physical_task(task)
        timeout = self._physical_ack_timeout()
        if not completion.event.wait(timeout=timeout):
            with completion.lock:
                if completion.error is not None:
                    raise completion.error
                if completion.receipt is not None:
                    return completion.receipt
            unknown = PhysicalStateCommitUnknownError(
                "physical-state commit acknowledgement timed out; durable result unknown"
            )
            self._record_worker_failure(unknown)
            raise unknown
        with completion.lock:
            completion_error = completion.error
            receipt = completion.receipt
        if completion_error is not None:
            raise completion_error
        if receipt is None:
            unknown = PhysicalStateCommitUnknownError(
                "physical-state worker returned no durable commit receipt"
            )
            self._record_worker_failure(unknown)
            raise unknown
        return receipt

    def _enqueue_physical_task(self, task: _PhysicalStateTask) -> None:
        """Atomically reject every submission after close or the first failure."""

        with self._state_lock:
            failure = self._worker_failure
            if failure is not None:
                error_type, message = failure
                raise PhysicalStatePersistenceError(
                    f"physical-state persistence failed ({error_type}): {message}"
                )
            if not self._accepting_submissions:
                raise RuntimeError(
                    "TradeLogger is closed and rejects new physical-state tasks"
                )
            self._async_queue.put(task)

    def _physical_ack_timeout(self) -> float:
        remaining = self._remaining_shutdown_budget()
        if remaining is None:
            return _PHYSICAL_ACK_TIMEOUT_SECONDS
        if remaining <= 0.0:
            return 0.0
        return min(_PHYSICAL_ACK_TIMEOUT_SECONDS, remaining)

    async def async_log_physical_state(
        self,
        stock_code: str,
        forces_dict: Dict[str, float],
    ) -> None:
        """Compatibility shim that delegates to synchronous queue submission."""
        self.submit_physical_state(stock_code, forces_dict)

    def flush(self) -> None:
        """Drain all accepted queue tasks and surface the first worker failure."""
        self._async_queue.join()
        self._raise_worker_failure()

    def set_shutdown_deadline(self, deadline_remaining: Callable[[], float]) -> None:
        """Install a cooperative close budget for bounded shadow shutdown."""

        self._shutdown_deadline = deadline_remaining

    @property
    def is_closed(self) -> bool:
        """Return whether every owned worker, queue, and connection is terminal."""
        with self._state_lock:
            return self._closed

    def close(self) -> None:
        """Idempotently drain the queue, stop the worker, and close both connections."""
        with self._state_lock:
            if self._closed:
                close_owner = False
                close_event = None
            elif self._closing:
                close_owner = False
                close_event = self._close_complete
            else:
                self._accepting_submissions = False
                self._closing = True
                self._close_complete = threading.Event()
                close_owner = True
                close_event = self._close_complete

        if not close_owner and close_event is not None:
            close_event.wait()
        elif close_owner:
            try:
                self._close_owned_resources()
            except BaseException as error:
                self._record_close_failure("close orchestration", error)
            finally:
                with self._state_lock:
                    self._closing = False
                assert close_event is not None
                close_event.set()

        self._raise_close_failure()
        self._raise_worker_failure()

        with self._state_lock:
            closed = self._closed
        if not closed:
            raise TradeLoggerLifecycleError(
                "TradeLogger close did not reach a terminal state"
            )

    def _close_owned_resources(self) -> None:
        for _ in range(2):
            self._close_resource_pass()
            with self._state_lock:
                if self._closed:
                    return

    def _close_resource_pass(self) -> None:
        with self._state_lock:
            sentinel_enqueued = self._sentinel_enqueued

        if not sentinel_enqueued and self._worker_thread.is_alive():
            try:
                self._async_queue.put(_QUEUE_SENTINEL)
            except BaseException as error:
                self._record_close_failure("sentinel enqueue", error)
            else:
                with self._state_lock:
                    self._sentinel_enqueued = True
                sentinel_enqueued = True

        if sentinel_enqueued:
            try:
                remaining = self._remaining_shutdown_budget()
                if remaining is None:
                    self._worker_thread.join()
                else:
                    self._worker_thread.join(timeout=remaining)
            except BaseException as error:
                self._record_close_failure("worker thread join", error)

        worker_stopped = not self._worker_thread.is_alive()
        if not worker_stopped and self._shutdown_deadline is not None:
            self._record_close_failure(
                "worker thread join",
                RuntimeError("database worker did not stop before the shutdown deadline"),
            )
        queue_drained = self._async_queue.unfinished_tasks == 0

        if worker_stopped and not queue_drained:
            self._consume_orphaned_sentinels()
            queue_drained = self._async_queue.unfinished_tasks == 0

        if worker_stopped and queue_drained:
            try:
                self._join_queue_with_budget()
            except BaseException as error:
                self._record_close_failure("queue drain", error)
        elif worker_stopped:
            self._record_close_failure(
                "queue drain",
                RuntimeError(
                    f"{self._async_queue.unfinished_tasks} queue task(s) remain unfinished"
                ),
            )

        if worker_stopped:
            with self._state_lock:
                worker_connection_closed = self._worker_connection_closed
            if not worker_connection_closed:
                try:
                    self._worker_conn.close()
                except BaseException as error:
                    self._record_close_failure("worker connection close", error)
                else:
                    with self._state_lock:
                        self._worker_connection_closed = True

        with self._state_lock:
            main_connection_closed = self._main_connection_closed
        if not main_connection_closed:
            try:
                self.conn.close()
            except BaseException as error:
                self._record_close_failure("main connection close", error)
            else:
                with self._state_lock:
                    self._main_connection_closed = True

        with self._state_lock:
            terminal = (
                not self._worker_thread.is_alive()
                and self._async_queue.unfinished_tasks == 0
                and self._async_queue.empty()
                and self._worker_connection_closed
                and self._main_connection_closed
            )
            self._closed = terminal

    def _remaining_shutdown_budget(self) -> Optional[float]:
        if self._shutdown_deadline is None:
            return None
        try:
            remaining = float(self._shutdown_deadline())
        except Exception:
            return 0.0
        return max(0.0, remaining)

    def _join_queue_with_budget(self) -> None:
        if self._shutdown_deadline is None:
            self._async_queue.join()
            return
        while self._async_queue.unfinished_tasks:
            remaining = self._remaining_shutdown_budget()
            if remaining is None or remaining <= 0:
                raise RuntimeError("database queue did not drain before the shutdown deadline")
            with self._async_queue.all_tasks_done:
                self._async_queue.all_tasks_done.wait(timeout=min(0.05, remaining))

    def _consume_orphaned_sentinels(self) -> None:
        with self._state_lock:
            submissions_latched = not self._accepting_submissions
        if not submissions_latched:
            return

        with self._async_queue.mutex:
            queued_items = tuple(self._async_queue.queue)
            only_sentinels = bool(queued_items) and all(
                item is _QUEUE_SENTINEL for item in queued_items
            )
            if not only_sentinels:
                return
            for _ in queued_items:
                self._async_queue._get()
            self._async_queue.unfinished_tasks -= len(queued_items)
            if self._async_queue.unfinished_tasks == 0:
                self._async_queue.all_tasks_done.notify_all()
            self._async_queue.not_full.notify_all()
        
    def get_last_physical_state(self, stock_code: str) -> Optional[dict]:
        # [수정] 크래시 복구를 위해 velocity 추출
        query = "SELECT velocity, last_updated FROM physics_state WHERE stock_code = ?"
        cursor = self.conn.execute(query, (stock_code,))
        row = cursor.fetchone()

        if row:
            timestamp = datetime.strptime(
                row['last_updated'], '%Y-%m-%d %H:%M:%S.%f'
            )
            current = self._now()
            if timestamp.tzinfo is None and current.tzinfo is not None:
                timestamp = timestamp.replace(tzinfo=current.tzinfo)
            return {
                'velocity': row['velocity'],
                'timestamp': timestamp
            }
        return None

    def load_physical_state(self, stock_code: str) -> PhysicalStateLoadResult:
        """Decode canonical v1 only when its legacy projection still matches."""

        if not isinstance(stock_code, str) or not stock_code:
            raise PhysicalStateValidationError("physical tracker stock_code is required")
        canonical = self.conn.execute(
            """
            SELECT stock_code, schema_version, velocity, last_cumulative_volume,
                   last_price, interval_volume_history, last_observed_at, updated_at,
                   projection_generation, projection_velocity,
                   projection_thrust, projection_gravity, projection_drag,
                   projection_magnetic, projection_jerk, projection_impulse,
                   projection_net_force,
                   projection_last_updated
            FROM physical_tracker_state_v1 WHERE stock_code = ?
            """,
            (stock_code,),
        ).fetchone()
        legacy = self.conn.execute(
            """
            SELECT velocity, thrust, gravity, drag, magnetic, jerk, impulse,
                   net_force, last_updated
            FROM physics_state WHERE stock_code = ?
            """,
            (stock_code,),
        ).fetchone()
        if canonical is None:
            return PhysicalStateLoadResult(
                (
                    PhysicalStateHydrationSource.LEGACY_COLD_START
                    if legacy is not None
                    else PhysicalStateHydrationSource.INITIAL
                ),
                None,
            )
        if legacy is None:
            raise PhysicalStateValidationError("canonical snapshot has no legacy projection")
        if canonical["schema_version"] != PHYSICAL_TRACKER_SCHEMA_VERSION:
            raise PhysicalStateValidationError(
                f"unsupported physical tracker schema: {canonical['schema_version']}"
            )
        required = (
            "velocity",
            "last_cumulative_volume",
            "last_price",
            "interval_volume_history",
            "last_observed_at",
            "updated_at",
            "projection_generation",
            "projection_velocity",
            "projection_thrust",
            "projection_gravity",
            "projection_drag",
            "projection_magnetic",
            "projection_jerk",
            "projection_impulse",
            "projection_net_force",
            "projection_last_updated",
        )
        if any(canonical[name] is None for name in required):
            raise PhysicalStateValidationError("partial physical tracker snapshot")
        if canonical["projection_generation"] != canonical["last_observed_at"]:
            raise PhysicalStateValidationError(
                "canonical projection generation does not match observation"
            )
        for projection_name in (
            "projection_velocity",
            "projection_thrust",
            "projection_gravity",
            "projection_drag",
            "projection_magnetic",
            "projection_jerk",
            "projection_impulse",
            "projection_net_force",
        ):
            projection_value = canonical[projection_name]
            if (
                isinstance(projection_value, bool)
                or not isinstance(projection_value, (int, float))
                or not math.isfinite(float(projection_value))
            ):
                raise PhysicalStateValidationError(
                    f"invalid canonical legacy projection: {projection_name}"
                )
        projection_pairs = (
            ("velocity", "projection_velocity"),
            ("thrust", "projection_thrust"),
            ("gravity", "projection_gravity"),
            ("drag", "projection_drag"),
            ("magnetic", "projection_magnetic"),
            ("jerk", "projection_jerk"),
            ("impulse", "projection_impulse"),
            ("net_force", "projection_net_force"),
            ("last_updated", "projection_last_updated"),
        )
        if any(
            legacy[legacy_name] != canonical[projection_name]
            for legacy_name, projection_name in projection_pairs
        ):
            return PhysicalStateLoadResult(
                PhysicalStateHydrationSource.LEGACY_COLD_START,
                None,
            )
        try:
            history_raw = json.loads(canonical["interval_volume_history"])
            if not isinstance(history_raw, list):
                raise TypeError("volume history is not a list")
            if any(type(value) not in (int, float) for value in history_raw):
                raise TypeError("volume history contains a non-numeric value")
            state = PhysicalTrackerState(
                schema_version=canonical["schema_version"],
                stock_code=canonical["stock_code"],
                velocity=canonical["velocity"],
                last_cumulative_volume=canonical["last_cumulative_volume"],
                last_price=canonical["last_price"],
                interval_volume_history=tuple(history_raw),
                last_observed_at=datetime.fromisoformat(canonical["last_observed_at"]),
                updated_at=datetime.fromisoformat(canonical["updated_at"]),
            )
            state.assert_persistable()
        except (TypeError, ValueError) as error:
            raise PhysicalStateValidationError(
                f"invalid physical tracker snapshot: {error}"
            ) from error
        return PhysicalStateLoadResult(PhysicalStateHydrationSource.PERSISTED, state)

    @staticmethod
    def _strict_session_date(value: object) -> date:
        if isinstance(value, date) and not isinstance(value, datetime):
            session_date = value
        elif isinstance(value, str):
            try:
                session_date = date.fromisoformat(value)
            except ValueError as error:
                raise PaperTradePersistenceError(
                    "owning_session_date must use YYYY-MM-DD"
                ) from error
            if session_date.isoformat() != value:
                raise PaperTradePersistenceError(
                    "owning_session_date must use YYYY-MM-DD"
                )
        else:
            raise PaperTradePersistenceError(
                "owning_session_date must be a date or YYYY-MM-DD"
            )
        try:
            if not is_krx_session(session_date):
                raise PaperTradePersistenceError(
                    "owning_session_date is not an XKRX session"
                )
        except KrxCalendarError as error:
            raise PaperTradePersistenceError(str(error)) from error
        return session_date

    def _verify_lifecycle_shape(self) -> None:
        columns = list(self.conn.execute("PRAGMA table_info(trades)"))
        for column in ("owning_session_date", "state_changed_at"):
            matches = [row for row in columns if row[1] == column]
            if len(matches) != 1 or matches[0][2] != "TEXT":
                raise PaperTradePersistenceError(
                    f"trades.{column} must exist exactly once as TEXT"
                )

    @staticmethod
    def _strict_required_string(value: object, name: str) -> str:
        if type(value) is not str or value == "":
            raise PaperTradePersistenceError(
                f"active position {name} must be a non-empty string"
            )
        return value

    @staticmethod
    def _strict_finite_number(
        value: object,
        name: str,
        *,
        positive: bool = False,
    ) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            qualifier = "positive finite" if positive else "finite"
            raise PaperTradePersistenceError(
                f"active position {name} must be a {qualifier} number"
            )
        numeric = float(value)
        if not math.isfinite(numeric) or (positive and numeric <= 0.0):
            qualifier = "positive finite" if positive else "finite"
            raise PaperTradePersistenceError(
                f"active position {name} must be a {qualifier} number"
            )
        return numeric

    @staticmethod
    def _strict_buy_time(value: object) -> datetime:
        if type(value) is not str:
            raise PaperTradePersistenceError(
                "buy_time must use YYYY-MM-DD HH:MM:SS"
            )
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise PaperTradePersistenceError(
                "buy_time must use YYYY-MM-DD HH:MM:SS"
            ) from error
        if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
            raise PaperTradePersistenceError(
                "buy_time must use YYYY-MM-DD HH:MM:SS"
            )
        return parsed

    @staticmethod
    def _strict_state_time(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise PaperTradePersistenceError(
                    "state_changed_at must use aware ISO-8601 KST"
                ) from error
            if parsed.isoformat() != value:
                raise PaperTradePersistenceError(
                    "state_changed_at must use canonical ISO-8601"
                )
        else:
            raise PaperTradePersistenceError(
                "state_changed_at must be an aware KST datetime"
            )
        try:
            return require_aware_kst(parsed, "state_changed_at")
        except KrxCalendarError as error:
            raise PaperTradePersistenceError(str(error)) from error

    def _legacy_open_metadata(self, row: Mapping[str, Any]) -> Tuple[date, datetime]:
        parsed = self._strict_buy_time(row.get("buy_time"))
        state_time = parsed.replace(tzinfo=KST)
        session_date = self._strict_session_date(state_time.date())
        return session_date, state_time

    def _decode_active_row(
        self,
        raw: Mapping[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[Tuple[str, str, int, str]]]:
        data = dict(raw)
        row_id = data.get("id")
        if type(row_id) is not int or row_id <= 0:
            raise PaperTradePersistenceError(
                "active position id must be a positive int"
            )
        stock_code = self._strict_required_string(
            data.get("stock_code"), "stock_code"
        )
        data["stock_name"] = self._strict_required_string(
            data.get("stock_name"), "stock_name"
        )
        data["buy_regime"] = self._strict_required_string(
            data.get("buy_regime"), "buy_regime"
        )
        self._strict_buy_time(data.get("buy_time"))
        data["buy_price"] = self._strict_finite_number(
            data.get("buy_price"), "buy_price", positive=True
        )
        for field_name in _ACTIVE_FORCE_FIELDS:
            data[field_name] = self._strict_finite_number(
                data.get(field_name), field_name
            )
        if data.get("sell_price") is not None:
            data["sell_price"] = self._strict_finite_number(
                data["sell_price"], "sell_price", positive=True
            )
        if data.get("profit_rate") is not None:
            data["profit_rate"] = self._strict_finite_number(
                data["profit_rate"], "profit_rate"
            )
        for field_name in ("sell_time", "sell_reason"):
            if data.get(field_name) is not None:
                data[field_name] = self._strict_required_string(
                    data[field_name], field_name
                )

        status_raw = data.get("status")
        if type(status_raw) is not str or status_raw not in {
            PositionStatus.OPEN.value,
            PositionStatus.OVERNIGHT.value,
        }:
            raise PaperTradePersistenceError(
                f"unsupported active position status: {status_raw}"
            )
        status = PositionStatus(status_raw)
        owner_raw = data.get("owning_session_date")
        changed_raw = data.get("state_changed_at")
        backfill: Optional[Tuple[str, str, int, str]] = None
        if owner_raw is None and changed_raw is None:
            if status is PositionStatus.OVERNIGHT:
                raise PaperTradePersistenceError(
                    "legacy OVERNIGHT position metadata cannot be inferred"
                )
            owner, changed = self._legacy_open_metadata(data)
            backfill = (
                owner.isoformat(),
                changed.isoformat(),
                row_id,
                stock_code,
            )
        elif owner_raw is None or changed_raw is None:
            raise PaperTradePersistenceError("partial active position metadata")
        else:
            if type(owner_raw) is not str:
                raise PaperTradePersistenceError(
                    "owning_session_date must use YYYY-MM-DD"
                )
            if type(changed_raw) is not str:
                raise PaperTradePersistenceError(
                    "state_changed_at must use aware ISO-8601 KST"
                )
            owner = self._strict_session_date(owner_raw)
            changed = self._strict_state_time(changed_raw)
        data["status"] = status
        data["owning_session_date"] = owner
        data["state_changed_at"] = changed
        return data, backfill

    def _validated_active_rows(
        self,
    ) -> Tuple[list[Dict[str, Any]], list[Tuple[str, str, int, str]]]:
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status IS NULL OR status != 'CLOSED'"
        ).fetchall()
        decoded: list[Dict[str, Any]] = []
        backfills: list[Tuple[str, str, int, str]] = []
        seen_codes: set[str] = set()
        for raw in rows:
            data, backfill = self._decode_active_row(dict(raw))
            stock_code = data["stock_code"]
            if stock_code in seen_codes:
                raise PaperTradePersistenceError(
                    f"duplicate active position for stock_code: {stock_code}"
                )
            seen_codes.add(stock_code)
            if backfill is not None:
                backfills.append(backfill)
            decoded.append(data)
        return decoded, backfills

    def load_active_positions(self) -> Dict[str, Dict[str, Any]]:
        """Load a complete strict OPEN/OVERNIGHT mapping without writes."""

        decoded, backfills = self._validated_active_rows()
        if backfills:
            raise PaperTradePersistenceError(
                "legacy OPEN metadata was not initialized"
            )
        return {row["stock_code"]: row for row in decoded}

    def load_open_positions(self) -> Dict:
        """Deprecated concrete compatibility reader with exact OPEN-only semantics."""

        warnings.warn(
            "load_open_positions is deprecated; use load_active_positions",
            DeprecationWarning,
            stacklevel=2,
        )
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN'"
        ).fetchall()
        return {row["stock_code"]: dict(row) for row in rows}

    def record_buy(self, data: Dict) -> int:
        """상세 점수를 포함하여 매수 기록"""
        stock_code = self._strict_required_string(
            data.get("stock_code"), "stock_code"
        )
        stock_name = self._strict_required_string(
            data.get("stock_name"), "stock_name"
        )
        buy_price = self._strict_finite_number(
            data.get("buy_price"), "buy_price", positive=True
        )
        buy_time = self._strict_required_string(
            data.get("buy_time"), "buy_time"
        )
        self._strict_buy_time(buy_time)
        buy_regime = self._strict_required_string(
            data.get("buy_regime"), "buy_regime"
        )
        forces = {
            field_name: self._strict_finite_number(
                data.get(field_name), field_name
            )
            for field_name in _ACTIVE_FORCE_FIELDS
        }
        owner_raw = data.get("owning_session_date")
        changed_raw = data.get("state_changed_at")
        if owner_raw is None and changed_raw is None:
            owner_text = None
            changed_text = None
        elif owner_raw is None or changed_raw is None:
            raise PaperTradePersistenceError("partial buy state metadata")
        else:
            owner_text = self._strict_session_date(owner_raw).isoformat()
            changed_text = self._strict_state_time(changed_raw).isoformat()
        query = """
        INSERT INTO trades (
            stock_code, stock_name, buy_price,
            thrust, gravity, drag, magnetic, jerk, impulse, net_force,
            buy_time, buy_regime, status, owning_session_date, state_changed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            stock_code,
            stock_name,
            buy_price,
            forces["thrust"],
            forces["gravity"],
            forces["drag"],
            forces["magnetic"],
            forces["jerk"],
            forces["impulse"],
            forces["net_force"],
            buy_time,
            buy_regime,
            PositionStatus.OPEN.value,
            owner_text, changed_text,
        )
        try:
            cursor = self.conn.execute(query, params)
            row_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0
            if row_id <= 0:
                raise PaperTradePersistenceError("paper buy did not return an id")
            self.conn.commit()
            return row_id
        except Exception as error:
            self.conn.rollback()
            if isinstance(error, PaperTradePersistenceError):
                raise
            raise PaperTradePersistenceError("paper buy commit failed") from error

    def _transition_position(
        self,
        pos: Position,
        *,
        expected: PositionStatus,
        status: PositionStatus,
        owning_session_date: date,
        state_changed_at: datetime,
        sell_price: object = None,
        sell_time: Optional[str] = None,
        profit_rate: object = None,
        sell_reason: Optional[str] = None,
    ) -> PositionTransitionReceipt:
        owner = self._strict_session_date(owning_session_date)
        changed = self._strict_state_time(state_changed_at)
        try:
            cursor = self.conn.execute(
                """
                UPDATE trades
                SET status = ?, owning_session_date = ?, state_changed_at = ?,
                    sell_price = COALESCE(?, sell_price),
                    sell_time = COALESCE(?, sell_time),
                    profit_rate = COALESCE(?, profit_rate),
                    sell_reason = COALESCE(?, sell_reason)
                WHERE id = ? AND stock_code = ? AND status = ?
                """,
                (
                    status.value,
                    owner.isoformat(),
                    changed.isoformat(),
                    sell_price,
                    sell_time,
                    profit_rate,
                    sell_reason,
                    pos.id,
                    pos.stock_code,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise PaperTradePersistenceError(
                    "paper position transition identity/status mismatch"
                )
            self.conn.commit()
        except Exception as error:
            self.conn.rollback()
            if isinstance(error, PaperTradePersistenceError):
                raise
            raise PaperTradePersistenceError(
                "paper position transition commit failed"
            ) from error
        return PositionTransitionReceipt(
            position_id=pos.id,
            stock_code=pos.stock_code,
            previous_status=expected,
            status=status,
            owning_session_date=owner,
            state_changed_at=changed,
        )

    def record_sell(
        self,
        pos: Position,
        *,
        state_changed_at: Optional[datetime] = None,
    ) -> PositionTransitionReceipt:
        """Persist the unrounded domain return while closing the paper row."""

        if pos.status is not PositionStatus.OPEN:
            raise PaperTradePersistenceError("only OPEN can become CLOSED")
        if pos.owning_session_date is None:
            raise PaperTradePersistenceError("active sell requires owning session metadata")
        changed = self._state_now() if state_changed_at is None else state_changed_at
        return self._transition_position(
            pos,
            expected=PositionStatus.OPEN,
            status=PositionStatus.CLOSED,
            owning_session_date=pos.owning_session_date,
            state_changed_at=changed,
            sell_price=pos.sell_price,
            sell_time=changed.strftime("%Y-%m-%d %H:%M:%S"),
            profit_rate=pos.calc_profit_rate,
            sell_reason=pos.sell_reason,
        )

    def mark_position_overnight(
        self,
        position: Position,
        *,
        state_changed_at: datetime,
    ) -> PositionTransitionReceipt:
        if position.owning_session_date is None:
            raise PaperTradePersistenceError("OPEN position has no owning session")
        return self._transition_position(
            position,
            expected=PositionStatus.OPEN,
            status=PositionStatus.OVERNIGHT,
            owning_session_date=position.owning_session_date,
            state_changed_at=state_changed_at,
        )

    def reopen_position(
        self,
        position: Position,
        *,
        owning_session_date: date,
        state_changed_at: datetime,
    ) -> PositionTransitionReceipt:
        return self._transition_position(
            position,
            expected=PositionStatus.OVERNIGHT,
            status=PositionStatus.OPEN,
            owning_session_date=owning_session_date,
            state_changed_at=state_changed_at,
        )

    def get_cumulative_realized_trade_return_score(
        self,
        session_date: date,
    ) -> float:
        """Sum CLOSED per-trade percentage-point returns for an explicit XKRX date."""
        if isinstance(session_date, datetime) or not isinstance(session_date, date):
            raise TypeError("session_date must be a date")
        if not is_krx_session(session_date):
            raise ValueError("session_date must be an XKRX session date")
        query = (
            "SELECT SUM(profit_rate) AS cumulative_realized_trade_return_score "
            "FROM trades WHERE status = 'CLOSED' AND sell_time LIKE ?"
        )
        result = self.conn.execute(
            query,
            (f"{session_date.isoformat()}%",),
        ).fetchone()
        score = result["cumulative_realized_trade_return_score"]
        return float(score) if score is not None else 0.0

    def get_today_realized_pnl(self) -> float:
        """Deprecated compatibility wrapper for one migration window."""
        warnings.warn(
            "get_today_realized_pnl() is deprecated; pass an explicit session date "
            "to get_cumulative_realized_trade_return_score().",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.get_cumulative_realized_trade_return_score(self._now().date())

    def get_last_sell_time(self, stock_code: str) -> Optional[datetime]:
        """해당 종목의 가장 최근 매도(CLOSED) 기록 시간을 반환합니다."""
        query = """
            SELECT sell_time 
            FROM trades 
            WHERE stock_code = ? AND status = 'CLOSED' 
            ORDER BY sell_time DESC 
            LIMIT 1
        """
        cursor = self.conn.execute(query, (stock_code,))
        
        # 1. fetchone()으로 데이터 한 행을 가져옴
        row = cursor.fetchone()
        
        # 2. 데이터가 존재하고 컬럼값이 있는지 확인
        if row and row['sell_time']:
            return datetime.strptime(row['sell_time'], '%Y-%m-%d %H:%M:%S')
            
        return None

    def get_today_traded_targets(self, target_date_str: Optional[str] = None):
        """
        특정 일자(매수/매도) 이력이 있는 종목들의 코드와 이름을 딕셔너리로 묶어서 반환합니다.
        :param target_date_str: '%Y-%m-%d' 양식의 날짜 문자열. 미입력 시 오늘 날짜 사용.
        """
        if target_date_str is None:
            target_date_str = self._now().strftime('%Y-%m-%d')
        
        # DISTINCT를 사용하여 동일한 종목이 여러 번 거래되었더라도 한 번만 가져옵니다.
        query = """
            SELECT DISTINCT *
            FROM trades 
            WHERE buy_time LIKE ? OR sell_time LIKE ?
        """
        
        try:
            cursor = self.conn.execute(query, (f"{target_date_str}%", f"{target_date_str}%"))
            rows = cursor.fetchall()
            
            return rows
        except Exception as e:
            logger.info(f"오늘 거래 종목 타겟 추출 실패: {e}")
            return {}
