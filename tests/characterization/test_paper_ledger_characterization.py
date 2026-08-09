"""Characterize the SQLite paper ledger without external services or live orders."""

from contextlib import contextmanager
from datetime import date, datetime
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kiwoom_stock.core import database as database_module
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.monitoring.manager import Position, StockManager


PAPER_FORCES = {
    "thrust": 0.1,
    "gravity": -0.2,
    "drag": -0.3,
    "magnetic": 0.4,
    "jerk": 0.5,
    "impulse": 0.6,
    "net_force": 0.7,
}


def _frozen_datetime(value: datetime):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return value
            return value.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return value

    return FrozenDateTime


@contextmanager
def _working_directory(path: Path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _ForbiddenExternalFacade:
    """Tripwire for broker, network, Slack, S3, or Gemini capability access."""

    def __init__(self):
        self.attempts = []

    def __getattr__(self, name):
        self.attempts.append(name)
        raise AssertionError(f"paper ledger touched forbidden external capability: {name}")


class _PositionOnlyDatabase:
    def load_active_positions(self):
        return {}


class PaperLedgerCharacterizationTests(unittest.TestCase):
    def test_fractional_return_is_persisted_and_aggregated_without_pre_rounding(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            db_path = Path(temporary_directory) / "raw-return.sqlite3"
            state_time = datetime(
                2026, 8, 7, 11, 0, 0, tzinfo=ZoneInfo("Asia/Seoul")
            )
            clock = lambda: state_time
            first = TradeLogger(db_path, clock=clock)
            try:
                buy_id = first.record_buy(
                    {
                        "stock_code": "005930",
                        "stock_name": "Samsung",
                        "buy_price": 10_000.0,
                        "buy_time": "2026-08-07 10:00:00",
                        "buy_regime": "STABLE_BULL",
                        "owning_session_date": date(2026, 8, 7),
                        "state_changed_at": state_time,
                        **PAPER_FORCES,
                    }
                )
                position = Position(
                    id=buy_id,
                    stock_code="005930",
                    stock_name="Samsung",
                    buy_price=10_000.0,
                    buy_time="2026-08-07 10:00:00",
                    buy_regime="STABLE_BULL",
                    sell_price=10_255.5,
                    sell_reason="Fixed Target (2.555 %p; percentage-points-v1)",
                    owning_session_date=date(2026, 8, 7),
                    state_changed_at=state_time,
                )
                first.record_sell(position)
                first.conn.execute(
                    "INSERT INTO trades (status, sell_time, profit_rate) "
                    "VALUES ('CLOSED', '2026-08-06 14:00:00', 99.0)"
                )
                first.conn.commit()
            finally:
                first.close()

            reopened = TradeLogger(db_path, clock=clock)
            try:
                row = reopened.conn.execute(
                    "SELECT profit_rate, sell_reason FROM trades WHERE id = ?",
                    (buy_id,),
                ).fetchone()
                self.assertEqual(row["profit_rate"], 2.555)
                self.assertNotEqual(row["profit_rate"], 2.55)
                self.assertEqual(
                    reopened.get_cumulative_realized_trade_return_score(
                        date(2026, 8, 7)
                    ),
                    2.555,
                )
                self.assertIn("2.555 %p", row["sell_reason"])
                self.assertEqual(
                    reopened.get_cumulative_realized_trade_return_score(
                        date(2026, 8, 6)
                    ),
                    99.0,
                )
                with self.assertRaisesRegex(ValueError, "XKRX session"):
                    reopened.get_cumulative_realized_trade_return_score(
                        date(2026, 8, 9)
                    )
                with self.assertRaisesRegex(TypeError, "must be a date"):
                    reopened.get_cumulative_realized_trade_return_score(
                        datetime(2026, 8, 7, 10, 0)
                    )
                with self.assertWarns(DeprecationWarning):
                    self.assertEqual(reopened.get_today_realized_pnl(), 2.555)
            finally:
                reopened.close()

    def test_schema_custom_path_recovery_and_open_closed_transition(self):
        buy_time = datetime(2026, 7, 17, 10, 0, 0)
        sell_time = datetime(2026, 7, 17, 11, 0, 0)
        kst = ZoneInfo("Asia/Seoul")
        lifecycle_time = [buy_time.replace(tzinfo=kst)]
        external = _ForbiddenExternalFacade()

        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            db_path = temp_root / "paper-ledger.sqlite3"
            accidental_default_path = temp_root / "trades.db"

            with _working_directory(temp_root), patch.object(
                socket.socket,
                "connect",
                autospec=True,
            ) as network_connect:
                first = TradeLogger(str(db_path), clock=lambda: lifecycle_time[0])
                try:
                    table_names = {
                        row[0]
                        for row in first.conn.execute(
                            "SELECT name FROM sqlite_master WHERE type = 'table'"
                        ).fetchall()
                    }
                    trade_columns = {
                        row[1]: row
                        for row in first.conn.execute("PRAGMA table_info(trades)").fetchall()
                    }
                    physics_columns = {
                        row[1]: row
                        for row in first.conn.execute("PRAGMA table_info(physics_state)").fetchall()
                    }

                    self.assertTrue({"trades", "physics_state"}.issubset(table_names))
                    self.assertEqual(
                        set(trade_columns),
                        {
                            "id",
                            "stock_code",
                            "stock_name",
                            "buy_price",
                            "thrust",
                            "gravity",
                            "drag",
                            "magnetic",
                            "jerk",
                            "impulse",
                            "net_force",
                            "buy_time",
                            "buy_regime",
                            "sell_price",
                            "profit_rate",
                            "sell_time",
                            "sell_reason",
                            "status",
                            "owning_session_date",
                            "state_changed_at",
                        },
                    )
                    self.assertEqual(
                        set(physics_columns),
                        {
                            "stock_code",
                            "velocity",
                            "thrust",
                            "gravity",
                            "drag",
                            "magnetic",
                            "jerk",
                            "impulse",
                            "net_force",
                            "last_updated",
                        },
                    )
                    self.assertEqual(trade_columns["status"][4], "'OPEN'")
                    self.assertEqual(
                        {
                            name: (row[2], row[3], row[4], row[5])
                            for name, row in trade_columns.items()
                        },
                        {
                            "id": ("INTEGER", 0, None, 1),
                            "stock_code": ("TEXT", 0, None, 0),
                            "stock_name": ("TEXT", 0, None, 0),
                            "buy_price": ("REAL", 0, None, 0),
                            "thrust": ("REAL", 0, None, 0),
                            "gravity": ("REAL", 0, None, 0),
                            "drag": ("REAL", 0, None, 0),
                            "magnetic": ("REAL", 0, None, 0),
                            "jerk": ("REAL", 0, None, 0),
                            "impulse": ("REAL", 0, None, 0),
                            "net_force": ("REAL", 0, None, 0),
                            "buy_time": ("TEXT", 0, None, 0),
                            "buy_regime": ("TEXT", 0, None, 0),
                            "sell_price": ("REAL", 0, None, 0),
                            "profit_rate": ("REAL", 0, None, 0),
                            "sell_time": ("TEXT", 0, None, 0),
                            "sell_reason": ("TEXT", 0, None, 0),
                            "status": ("TEXT", 0, "'OPEN'", 0),
                            "owning_session_date": ("TEXT", 0, None, 0),
                            "state_changed_at": ("TEXT", 0, None, 0),
                        },
                    )
                    self.assertEqual(
                        {
                            name: (row[2], row[3], row[4], row[5])
                            for name, row in physics_columns.items()
                        },
                        {
                            "stock_code": ("TEXT", 0, None, 1),
                            "velocity": ("REAL", 0, None, 0),
                            "thrust": ("REAL", 0, None, 0),
                            "gravity": ("REAL", 0, None, 0),
                            "drag": ("REAL", 0, None, 0),
                            "magnetic": ("REAL", 0, None, 0),
                            "jerk": ("REAL", 0, None, 0),
                            "impulse": ("REAL", 0, None, 0),
                            "net_force": ("REAL", 0, None, 0),
                            "last_updated": ("TEXT", 0, None, 0),
                        },
                    )

                    manager = StockManager(
                        external,
                        first,
                        filter_config={},
                        clock=lambda: lifecycle_time[0],
                    )
                    manager.stock_names["005930"] = "Samsung"
                    verdict = {
                        "stock_code": "005930",
                        "price": 50_000.0,
                        "regime": "STABLE_BULL",
                        "forces": {
                            "thrust": 1.1,
                            "gravity": -0.2,
                            "drag": -0.1,
                            "magnetic": 0.3,
                            "jerk": 0.4,
                            "impulse": 0.5,
                            "net_force": 2.0,
                        },
                    }
                    bought, buy_data = manager.process_buy_order(verdict)

                    self.assertTrue(bought)
                    self.assertIsNotNone(buy_data)
                    self.assertEqual(set(first.load_open_positions()), {"005930"})
                finally:
                    first.close()

                second = TradeLogger(str(db_path), clock=lambda: lifecycle_time[0])
                try:
                    recovered = StockManager(
                        external,
                        second,
                        filter_config={},
                        clock=lambda: lifecycle_time[0],
                    )
                    self.assertEqual(set(recovered.active_positions), {"005930"})
                    self.assertEqual(recovered.active_positions["005930"].buy_price, 50_000.0)

                    sell_verdict = {"stock_code": "005930", "price": 55_000.0}
                    lifecycle_time[0] = sell_time.replace(tzinfo=kst)
                    sold, sold_position = recovered.process_sell_order(
                        sell_verdict,
                        "characterized exit",
                    )

                    self.assertTrue(sold)
                    self.assertIsNotNone(sold_position)
                    self.assertEqual(sold_position.calc_profit_rate, 10.0)
                    self.assertEqual(second.load_open_positions(), {})

                    row = second.conn.execute(
                        "SELECT * FROM trades WHERE stock_code = ?",
                        ("005930",),
                    ).fetchone()
                    self.assertEqual(row["status"], "CLOSED")
                    self.assertEqual(row["buy_time"], "2026-07-17 10:00:00")
                    self.assertEqual(row["sell_time"], "2026-07-17 11:00:00")
                    self.assertEqual(row["sell_price"], 55_000.0)
                    self.assertEqual(row["profit_rate"], 10.0)
                    self.assertEqual(row["sell_reason"], "characterized exit")
                    self.assertEqual(row["owning_session_date"], "2026-07-17")
                    self.assertEqual(
                        row["state_changed_at"],
                        "2026-07-17T11:00:00+09:00",
                    )
                    self.assertEqual(
                        {
                            name: row[name]
                            for name in (
                                "thrust",
                                "gravity",
                                "drag",
                                "magnetic",
                                "jerk",
                                "impulse",
                                "net_force",
                            )
                        },
                        {
                            "thrust": 1.1,
                            "gravity": -0.2,
                            "drag": -0.1,
                            "magnetic": 0.3,
                            "jerk": 0.4,
                            "impulse": 0.5,
                            "net_force": 2.0,
                        },
                    )

                    with patch.object(database_module, "datetime", _frozen_datetime(sell_time)):
                        self.assertEqual(
                            second.get_cumulative_realized_trade_return_score(
                                date(2026, 7, 17)
                            ),
                            10.0,
                        )
                finally:
                    second.close()

                self.assertEqual(external.attempts, [])
                network_connect.assert_not_called()
                self.assertTrue(db_path.is_file())
                self.assertFalse(accidental_default_path.exists())

    def test_worker_uses_custom_database_without_hard_coded_default_fallback(self):
        script = textwrap.dedent(
            '''
            import json
            import sqlite3
            import sys

            from kiwoom_stock.core.database import TradeLogger

            custom_path = sys.argv[1]
            logger = TradeLogger(custom_path)
            forces = {
                "current_velocity": 1.25,
                "thrust": 0.1,
                "gravity": -0.2,
                "drag": -0.3,
                "magnetic": 0.4,
                "jerk": 0.5,
                "impulse": 0.6,
                "net_force": 1.1,
            }
            logger.submit_physical_state("005930", forces)
            logger.close()

            custom_connection = sqlite3.connect(custom_path)
            custom_row = custom_connection.execute(
                "SELECT stock_code, velocity, net_force FROM physics_state"
            ).fetchone()
            custom_connection.close()
            print(
                json.dumps(
                    {
                        "custom_row": custom_row,
                        "worker_alive": logger._worker_thread.is_alive(),
                    }
                )
            )
            '''
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            temp_root = Path(temporary_directory)
            custom_path = temp_root / "custom.sqlite3"
            completed = subprocess.run(
                [sys.executable, "-c", script, str(custom_path)],
                cwd=temp_root,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            result = json.loads(completed.stdout.strip().splitlines()[-1])

            self.assertEqual(result["custom_row"], ["005930", 1.25, 1.1])
            self.assertFalse(result["worker_alive"])
            self.assertTrue(custom_path.is_file())
            self.assertFalse((temp_root / "trades.db").exists())

    def test_cumulative_trade_return_score_is_unweighted_percentage_sum(self):
        manager = StockManager(_ForbiddenExternalFacade(), _PositionOnlyDatabase(), filter_config={})
        manager.active_positions = {
            "A": Position(
                id=1,
                stock_code="A",
                stock_name="A",
                buy_price=100.0,
                sell_price=110.0,
                buy_time="2026-07-17 10:00:00",
                buy_regime="STABLE_BULL",
            ),
            "B": Position(
                id=2,
                stock_code="B",
                stock_name="B",
                buy_price=100_000.0,
                sell_price=95_000.0,
                buy_time="2026-07-17 10:00:00",
                buy_regime="STABLE_BULL",
            ),
        }

        self.assertEqual(
            manager.calculate_cumulative_trade_return_score(
                realized_trade_return_score=1.25
            ),
            6.25,
        )


if __name__ == "__main__":
    unittest.main()
