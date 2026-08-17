"""File-level writer ownership contracts for the paper/swing SQLite ledgers."""

from pathlib import Path

import pytest

from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy
from kiwoom_stock.infrastructure.sqlite_write_owner import (
    SqliteWriterOwner,
    SqliteWriterOwnershipError,
)


def test_same_sqlite_file_has_one_nonblocking_writer_and_other_file_is_independent(
    tmp_path: Path,
):
    first_path = tmp_path / "first.sqlite3"
    second_path = tmp_path / "second.sqlite3"
    first = SqliteWriterOwner(first_path)
    try:
        with pytest.raises(SqliteWriterOwnershipError, match="already has"):
            SqliteWriterOwner(first_path)
        other = SqliteWriterOwner(second_path)
        other.close()
    finally:
        first.close()

    reopened = SqliteWriterOwner(first_path)
    reopened.close()


def test_trade_logger_releases_writer_ownership_after_close(tmp_path: Path):
    path = tmp_path / "trades.sqlite3"
    first = TradeLogger(path)
    with pytest.raises(SqliteWriterOwnershipError, match="already has"):
        TradeLogger(path)

    first.close()
    reopened = TradeLogger(path)
    reopened.close()


def test_read_only_swing_ledger_does_not_compete_with_writable_owner(tmp_path: Path):
    path = tmp_path / "candidate.sqlite3"
    policy = AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy("base-v1"),
        CostPolicy("stress-v1"),
    )
    writer = SwingLedger(path, portfolio_id="candidate", policy=policy)
    reader = SwingLedger(
        path,
        portfolio_id="candidate",
        policy=policy,
        read_only=True,
    )
    reader.close()
    writer.close()
