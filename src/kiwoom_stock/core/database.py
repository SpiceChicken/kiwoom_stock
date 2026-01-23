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
        """지표별 상세 점수 컬럼 추가"""
        query = """
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            stock_name TEXT,
            buy_price REAL,
            buy_score REAL,
            -- 상세 지표 점수 컬럼 추가 --
            alpha_score REAL,
            supply_score REAL,
            vwap_score REAL,
            trend_score REAL,
            -----------------------
            buy_time TEXT,
            buy_regime TEXT,
            sell_price REAL,
            profit_rate REAL,
            sell_time TEXT,
            sell_reason TEXT,
            status TEXT DEFAULT 'OPEN'
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
        """상세 점수를 포함하여 매수 기록"""
        query = """
        INSERT INTO trades (
            stock_code, stock_name, buy_price, buy_score, 
            alpha_score, supply_score, vwap_score, trend_score,
            buy_time, buy_regime
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            data['stock_code'], data['stock_name'], data['buy_price'], data['buy_score'],
            data['alpha_score'], data['supply_score'], data['vwap_score'], data['trend_score'],
            data['buy_time'], data['buy_regime']
        )
        cursor = self.conn.execute(query, params)
        self.conn.commit()
        return cursor.lastrowid

    def record_sell(self, db_id: int, sell_price: float, profit_rate: float, reason: str):
        """매도 시 해당 레코드를 'CLOSED' 상태로 업데이트합니다."""
        query = """
        UPDATE trades 
        SET status = 'CLOSED', sell_price = ?, sell_time = ?, profit_rate = ?, sell_reason = ?
        WHERE id = ?
        """
        self.conn.execute(query, (
            sell_price, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            profit_rate, reason, db_id
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
            print(f"오늘 수익률 조회 실패: {e}")
            return 0.0