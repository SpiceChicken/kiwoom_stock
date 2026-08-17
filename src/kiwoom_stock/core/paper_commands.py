"""Paper buy and position-transition command persistence boundary."""

from __future__ import annotations

from datetime import date, datetime
import sqlite3
from typing import Any, Callable, Dict, Optional

from kiwoom_stock.application.ports import (
    PaperTradePersistenceError,
    PositionTransitionReceipt,
)
from kiwoom_stock.domain.models import Position, PositionStatus


def record_buy_command(
    connection: sqlite3.Connection,
    data: Dict[str, Any],
    *,
    strict_required_string: Callable[[object, str], str],
    strict_finite_number: Callable[..., float],
    strict_buy_time: Callable[[object], datetime],
    strict_session_date: Callable[[object], date],
    strict_state_time: Callable[[object], datetime],
) -> int:
    """Validate and commit one paper buy without owning the facade lifecycle."""

    stock_code = strict_required_string(data.get("stock_code"), "stock_code")
    stock_name = strict_required_string(data.get("stock_name"), "stock_name")
    buy_price = strict_finite_number(data.get("buy_price"), "buy_price", positive=True)
    buy_time = strict_required_string(data.get("buy_time"), "buy_time")
    strict_buy_time(buy_time)
    buy_regime = strict_required_string(data.get("buy_regime"), "buy_regime")
    forces = {
        field_name: strict_finite_number(data.get(field_name), field_name)
        for field_name in (
            "thrust",
            "gravity",
            "drag",
            "magnetic",
            "jerk",
            "impulse",
            "net_force",
        )
    }
    owner_raw = data.get("owning_session_date")
    changed_raw = data.get("state_changed_at")
    if owner_raw is None and changed_raw is None:
        owner_text = None
        changed_text = None
    elif owner_raw is None or changed_raw is None:
        raise PaperTradePersistenceError("partial buy state metadata")
    else:
        owner_text = strict_session_date(owner_raw).isoformat()
        changed_text = strict_state_time(changed_raw).isoformat()
    try:
        cursor = connection.execute(
            """
            INSERT INTO trades (
                stock_code, stock_name, buy_price,
                thrust, gravity, drag, magnetic, jerk, impulse, net_force,
                buy_time, buy_regime, status, owning_session_date, state_changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
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
                owner_text,
                changed_text,
            ),
        )
        row_id = int(cursor.lastrowid) if cursor.lastrowid is not None else 0
        if row_id <= 0:
            raise PaperTradePersistenceError("paper buy did not return an id")
        connection.commit()
        return row_id
    except Exception as error:
        connection.rollback()
        if isinstance(error, PaperTradePersistenceError):
            raise
        raise PaperTradePersistenceError("paper buy commit failed") from error


def transition_position_command(
    connection: sqlite3.Connection,
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
    strict_session_date: Callable[[object], date],
    strict_state_time: Callable[[object], datetime],
) -> PositionTransitionReceipt:
    """Commit one compare-and-set paper position transition."""

    owner = strict_session_date(owning_session_date)
    changed = strict_state_time(state_changed_at)
    try:
        cursor = connection.execute(
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
        connection.commit()
    except Exception as error:
        connection.rollback()
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
