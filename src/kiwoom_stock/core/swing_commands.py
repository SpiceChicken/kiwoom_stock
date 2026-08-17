"""Append command writers for the isolated swing ledger."""

from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Callable

from kiwoom_stock.application.ports import (
    SwingCommitReceipt,
    SwingCommandKind,
    SwingIdempotencyConflictError,
    SwingIdentityConflictError,
)
from kiwoom_stock.core.swing_schema import GENESIS_HASH
from kiwoom_stock.domain.accounting import AccountingPolicy


def register_portfolio_write(
    connection: sqlite3.Connection,
    *,
    portfolio_id: str,
    policy: AccountingPolicy,
    idempotency_key: str,
    envelope: str,
    digest: str,
    policy_hash: str,
    existing_command: Callable[[str, str], SwingCommitReceipt | None],
) -> SwingCommitReceipt:
    """Write one registration command inside the caller-owned transaction."""

    existing = connection.execute(
        "SELECT * FROM swing_portfolios_v1 WHERE portfolio_id=?",
        (portfolio_id,),
    ).fetchone()
    if existing is not None:
        if existing["policy_hash"] != policy_hash:
            raise SwingIdentityConflictError("registration policy differs")
        replay = existing_command(idempotency_key, digest)
        if replay is None:
            raise SwingIdempotencyConflictError(
                "original registration envelope is missing"
            )
        return replay
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    portfolio_row = (
        portfolio_id,
        policy.initial_cash_krw,
        policy.policy_version,
        policy_hash,
        now,
        "",
    )
    portfolio_row = portfolio_row[:-1] + (
        _canonical_payload(portfolio_row[:-1]),
    )
    connection.execute(
        "INSERT INTO swing_portfolios_v1 VALUES (?,?,?,?,?,?)",
        portfolio_row,
    )
    command_row = (
        portfolio_id,
        idempotency_key,
        SwingCommandKind.REGISTER_PORTFOLIO.value,
        envelope,
        digest,
        0,
        0,
        0,
        0,
        None,
        None,
        0,
        now,
        GENESIS_HASH,
        "",
    )
    command_row = command_row[:-1] + (_canonical_payload(command_row[:-1]),)
    connection.execute(
        "INSERT INTO swing_commands_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        command_row,
    )
    return SwingCommitReceipt(
        portfolio_id,
        SwingCommandKind.REGISTER_PORTFOLIO,
        idempotency_key,
        digest,
        0,
        None,
        None,
        0,
    )


def _canonical_payload(value: object) -> str:
    """Keep the command writer independent from the ledger facade import."""

    import hashlib
    import json

    def wire(item: object) -> object:
        if isinstance(item, tuple):
            return [wire(value) for value in item]
        if isinstance(item, dict):
            return {str(key): wire(value) for key, value in sorted(item.items())}
        return item

    return hashlib.sha256(
        json.dumps(wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
