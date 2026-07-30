# src/kiwoom_stock/core/database.py
import copy
from dataclasses import dataclass
from datetime import datetime
import logging
import os
import queue
import sqlite3
import threading
from typing import Any, Dict, Mapping, Optional, Tuple, Union

from kiwoom_stock.domain.models import Position

logger = logging.getLogger(__name__)


class PhysicalStatePersistenceError(RuntimeError):
    """One or more accepted physical-state snapshots could not be persisted."""


class TradeLoggerLifecycleError(RuntimeError):
    """TradeLogger could not complete one or more shutdown phases."""


@dataclass(frozen=True)
class _PhysicalStateTask:
    stock_code: str
    forces: Tuple[Tuple[str, Any], ...]
    timestamp_str: str


_QUEUE_SENTINEL = object()


class TradeLogger:
    def __init__(self, db_name: Union[str, os.PathLike[str]] = "trades.db"):
        self.db_path = os.path.normpath(os.path.abspath(os.fspath(db_name)))
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

    def _create_table(self):
        """기존 거래 내역 테이블과 물리학적 상태 저장을 위한 신규 테이블 생성"""
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
            status TEXT DEFAULT 'OPEN'
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
        self.conn.execute(query_trades)
        self.conn.execute(query_physics)
        self.conn.commit()

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

                    forces = dict(task.forces)
                    query = """
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
                    params = (
                        task.stock_code,
                        forces["current_velocity"],
                        forces["thrust"], forces["gravity"], forces["drag"],
                        forces["magnetic"], forces["jerk"], forces["impulse"],
                        forces["net_force"],
                        task.timestamp_str,
                    )
                    self._worker_conn.execute(query, params)
                    self._worker_conn.commit()
                except Exception as error:
                    self._record_worker_failure(error)
                    logger.error(f"비동기 DB 로깅 실패: {error}")
                finally:
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

    def _record_worker_failure(self, error: Exception) -> None:
        with self._state_lock:
            if self._worker_failure is None:
                self._worker_failure = (type(error).__name__, str(error))

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
            stock_code=stock_code,
            forces=tuple(
                (key, copy.deepcopy(value))
                for key, value in dict(forces).items()
            ),
            timestamp_str=datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f'),
        )
        with self._state_lock:
            if not self._accepting_submissions:
                raise RuntimeError("TradeLogger is closed and rejects new physical-state tasks")
            self._async_queue.put(task)

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
                self._worker_thread.join()
            except BaseException as error:
                self._record_close_failure("worker thread join", error)

        worker_stopped = not self._worker_thread.is_alive()
        queue_drained = self._async_queue.unfinished_tasks == 0

        if worker_stopped and not queue_drained:
            self._consume_orphaned_sentinels()
            queue_drained = self._async_queue.unfinished_tasks == 0

        if worker_stopped and queue_drained:
            try:
                self._async_queue.join()
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
            return {
                'velocity': row['velocity'],
                'timestamp': datetime.strptime(row['last_updated'], '%Y-%m-%d %H:%M:%S.%f')
            }
        return None

    def load_open_positions(self) -> Dict:
        """프로그램 시작 시 'OPEN' 상태인 종목들을 불러와 메모리에 복구합니다."""
        cursor = self.conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
        rows = cursor.fetchall()
        # { stock_code: {db 데이터} } 구조로 반환
        return {row['stock_code']: dict(row) for row in rows}

    def record_buy(self, data: Dict) -> int:
        """상세 점수를 포함하여 매수 기록"""
        query = """
        INSERT INTO trades (
            stock_code, stock_name, buy_price,
            thrust, gravity, drag, magnetic, jerk, impulse, net_force,
            buy_time, buy_regime
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            data.get('stock_code'), data.get('stock_name'), data.get('buy_price'),
            data.get('thrust', 0.0), data.get('gravity', 0.0), data.get('drag', 0.0), 
            data.get('magnetic', 0.0), data.get('jerk', 0.0), data.get('impulse', 0.0), data.get('net_force', 0.0),
            data.get('buy_time'), data.get('buy_regime')
        )
        cursor = self.conn.execute(query, params)
        self.conn.commit()
        return int(cursor.lastrowid) if cursor.lastrowid is not None else 0

    def record_sell(self, pos: Position):
        """매도 시 해당 레코드를 'CLOSED' 상태로 업데이트합니다."""
        profit_rate = pos.calc_profit_rate

        query = """
        UPDATE trades 
        SET status = 'CLOSED', sell_price = ?, sell_time = ?, profit_rate = ?, sell_reason = ?
        WHERE id = ?
        """
        self.conn.execute(query, (
            pos.sell_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            profit_rate, pos.sell_reason, pos.id
        ))
        self.conn.commit()

    def get_today_realized_pnl(self) -> float:
        """
        오늘 매도 완료(CLOSED)된 모든 종목의 누적 수익률 합계를 DB에서 직접 계산하여 반환합니다.
        프로그램 재시작 시에도 오늘 하루의 전체 손익을 정확히 추적할 수 있습니다.
        """
        # 1. 오늘 날짜 문자열 생성 (YYYY-MM-DD)
        today_str = datetime.now().strftime('%Y-%m-%d')
        
        # 2. SQL 쿼리: 오늘(sell_time LIKE 'YYYY-MM-DD%') 매도된 종목의 profit_rate 합산
        query = "SELECT SUM(profit_rate) as total_pnl FROM trades WHERE status = 'CLOSED' AND sell_time LIKE ?"
        
        try:
            cursor = self.conn.execute(query, (f"{today_str}%",))
            result = cursor.fetchone()
            
            # 3. 결과 반환 (오늘 거래가 없어서 결과가 None인 경우 0.0 반환)
            return result['total_pnl'] if result['total_pnl'] is not None else 0.0
        except Exception as e:
            # 로깅 시스템이 설정되어 있다면 활용 (예: logger.error)
            logger.info(f"오늘 수익률 조회 실패: {e}")
            return 0.0

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
            target_date_str = datetime.now().strftime('%Y-%m-%d')
        
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
