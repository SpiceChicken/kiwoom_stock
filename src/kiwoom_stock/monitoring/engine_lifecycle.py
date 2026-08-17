"""Session lifecycle boundaries used by the trading engine facade."""

from __future__ import annotations

from datetime import date, datetime
from typing import Callable, Optional

from kiwoom_stock.application.session import CycleContext
from kiwoom_stock.utils.market_cal import (
    KrxCalendarError,
    current_krx_session,
    require_aware_kst,
)


def resolve_cycle_context(
    wall_clock: Callable[[], datetime],
    *,
    session_resolver: Callable[[datetime], Optional[date]] = current_krx_session,
) -> Optional[CycleContext]:
    """Read one aware KST instant and resolve its XKRX session."""

    try:
        now = require_aware_kst(wall_clock(), "normal cycle clock")
        session_date = session_resolver(now)
    except KrxCalendarError:
        raise
    except Exception as error:
        raise KrxCalendarError("normal cycle context is unavailable") from error
    if session_date is None:
        return None
    return CycleContext(now=now, xkrx_session_date=session_date)
