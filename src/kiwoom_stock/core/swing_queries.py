"""Read-only projection queries for the swing ledger."""

from __future__ import annotations

from datetime import date
import sqlite3
from typing import cast


def fetch_latest_portfolio_snapshot(
    connection: sqlite3.Connection,
    portfolio_id: str,
) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        connection.execute(
            """
            SELECT snapshot_hash, sequence, snapshot_json
            FROM swing_portfolio_snapshots_v1
            WHERE portfolio_id=?
            ORDER BY sequence DESC
            LIMIT 1
            """,
            (portfolio_id,),
        ).fetchone(),
    )


def fetch_latest_position_sequences(
    connection: sqlite3.Connection,
    portfolio_id: str,
) -> tuple[tuple[str, int], ...]:
    return tuple(
        (row["position_id"], row["sequence"])
        for row in connection.execute(
            """
            SELECT position_id, MAX(sequence) sequence
            FROM swing_position_snapshots_v1
            WHERE portfolio_id=?
            GROUP BY position_id
            """,
            (portfolio_id,),
        )
    )


def fetch_daily_mark_revisions(
    connection: sqlite3.Connection,
    portfolio_id: str,
) -> tuple[tuple[str, str, date, int], ...]:
    return tuple(
        (
            row["position_id"],
            row["symbol"],
            date.fromisoformat(row["session_date"]),
            row["revision"],
        )
        for row in connection.execute(
            """
            SELECT position_id, symbol, session_date, revision
            FROM swing_daily_marks_v1
            WHERE portfolio_id=?
            ORDER BY rowid
            """,
            (portfolio_id,),
        )
    )
