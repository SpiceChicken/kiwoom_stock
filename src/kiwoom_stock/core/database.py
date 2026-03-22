# src/kiwoom_stock/core/database.py
import sqlite3
import threading
import queue
import logging
from datetime import datetime
from typing import List, Dict, Optional

from kiwoom_stock.monitoring.manager import Position

logger = logging.getLogger(__name__)

class TradeLogger:
    def __init__(self, db_name="trades.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_table()
        
        # L2 Backup용 비동기 작업 큐 및 데몬 스레드 초기화
        self._async_queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._async_worker, daemon=True)
        self._worker_thread.start()

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
    def _async_worker(self):
        worker_conn = sqlite3.connect("trades.db", check_same_thread=False)
        while True:
            try:
                task = self._async_queue.get()
                if task is None: break
                
                stock_code, forces, timestamp_str = task
                query = """
                INSERT INTO physics_state 
                (stock_code, velocity, thrust, gravity, drag, magnetic, jerk, impulse, net_force, last_updated)
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
                    stock_code,
                    forces["current_velocity"],
                    forces["thrust"], forces["gravity"], forces["drag"],
                    forces["magnetic"], forces["jerk"], forces["impulse"],
                    forces["net_force"],
                    timestamp_str
                )
                worker_conn.execute(query, params)
                worker_conn.commit()
                
            except Exception as e:
                logger.error(f"비동기 DB 로깅 실패: {e}")
            finally:
                self._async_queue.task_done()

    async def async_log_physical_state(self, stock_code: str, forces_dict: Dict[str, float]):
        """[수정] 단일 속도 대신 전체 힘 딕셔너리를 큐에 넣습니다."""
        timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')
        self._async_queue.put((stock_code, forces_dict, timestamp_str))
        
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