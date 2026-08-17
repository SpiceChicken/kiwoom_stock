"""Read query boundary for the paper ledger facade."""

from __future__ import annotations

from datetime import date
import sqlite3
from typing import Optional, cast


def fetch_active_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return rows requiring strict active-position decoding."""

    return cast(
        list[sqlite3.Row],
        connection.execute(
            "SELECT * FROM trades WHERE status IS NULL OR status != 'CLOSED'"
        ).fetchall(),
    )


def fetch_cumulative_score(
    connection: sqlite3.Connection,
    session_date: date,
) -> sqlite3.Row:
    """Return the aggregate row for one explicit XKRX session date."""

    return cast(
        sqlite3.Row,
        connection.execute(
            """
            SELECT SUM(profit_rate) AS cumulative_realized_trade_return_score
            FROM trades
            WHERE status = 'CLOSED' AND sell_time LIKE ?
            """,
            (f"{session_date.isoformat()}%",),
        ).fetchone(),
    )


def fetch_last_sell_row(
    connection: sqlite3.Connection,
    stock_code: str,
) -> Optional[sqlite3.Row]:
    """Return the most recent closed sell row for one symbol."""

    return cast(
        Optional[sqlite3.Row],
        connection.execute(
            """
            SELECT sell_time
            FROM trades
            WHERE stock_code = ? AND status = 'CLOSED'
            ORDER BY sell_time DESC
            LIMIT 1
            """,
            (stock_code,),
        ).fetchone(),
    )


def fetch_traded_targets(
    connection: sqlite3.Connection,
    target_date: str,
) -> list[sqlite3.Row]:
    """Return distinct trade rows touching one target date."""

    return connection.execute(
        """
        SELECT DISTINCT *
        FROM trades
        WHERE buy_time LIKE ? OR sell_time LIKE ?
        """,
        (f"{target_date}%", f"{target_date}%"),
    ).fetchall()
