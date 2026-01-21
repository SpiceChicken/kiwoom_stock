import sqlite3
from datetime import datetime
from typing import List, Dict

###### QUERY ######
# 총 수익률 합계: SELECT SUM(profit_rate) FROM trades WHERE status='CLOSED'
# 레짐별 평균 수익률: SELECT buy_regime, AVG(profit_rate) FROM trades GROUP BY buy_regime
###################

class TradeLogger:
    def __init__(self, db_name="trades.db"):
        self.conn = sqlite3.connect(db_name, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row  # 결과를 딕셔너리 형태로 받기 위함
        self._create_table()

    def _create_table(self):
        query = """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            status TEXT DEFAULT 'OPEN',     -- 'OPEN' (보유 중), 'CLOSED' (매도 완료)
            buy_price REAL,
            buy_time TEXT,
            buy_score REAL,
            buy_regime TEXT,
            sell_price REAL,
            sell_time TEXT,
            profit_rate REAL,
            exit_reason TEXT
        )
        """
        self.conn.execute(query)
        self.conn.commit()

    def load_open_positions(self) -> Dict:
        """프로그램 시작 시 'OPEN' 상태인 종목들을 불러와 메모리에 복구합니다."""
        cursor = self.conn.execute("SELECT * FROM trades WHERE status = 'OPEN'")
        rows = cursor.fetchall()
        # { stock_code: {db 데이터} } 구조로 반환
        return {row['stock_code']: dict(row) for row in rows}

    def record_buy(self, data: Dict) -> int:
        """매수 신호 발생 시 새로운 'OPEN' 레코드를 생성합니다."""
        query = """
        INSERT INTO trades (stock_code, stock_name, status, buy_price, buy_time, buy_score, buy_regime)
        VALUES (?, ?, 'OPEN', ?, ?, ?, ?)
        """
        cursor = self.conn.execute(query, (
            data['stock_code'], data['stock_name'], data['buy_price'],
            data['buy_time'], data['buy_score'], data['buy_regime']
        ))
        self.conn.commit()
        return cursor.lastrowid  # 생성된 레코드의 PK 반환

    def record_sell(self, db_id: int, sell_price: float, profit_rate: float, reason: str):
        """매도 시 해당 레코드를 'CLOSED' 상태로 업데이트합니다."""
        query = """
        UPDATE trades 
        SET status = 'CLOSED', sell_price = ?, sell_time = ?, profit_rate = ?, exit_reason = ?
        WHERE id = ?
        """
        self.conn.execute(query, (
            sell_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            profit_rate, reason, db_id
        ))
        self.conn.commit()