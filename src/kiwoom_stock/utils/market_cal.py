"""Strict shared XKRX calendar and KST instant helpers."""

import logging
from datetime import date, datetime, timedelta
from typing import Optional, cast
from zoneinfo import ZoneInfo

import exchange_calendars as xcals


logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


class KrxCalendarError(RuntimeError):
    """The local XKRX calendar could not authoritatively answer a query."""


def require_aware_kst(value: datetime, name: str = "instant") -> datetime:
    """Require one aware instant expressed with the canonical KST offset."""

    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset() != timedelta(hours=9)
    ):
        raise KrxCalendarError(f"{name} must be an aware KST datetime")
    return value.astimezone(KST)


def seoul_now() -> datetime:
    return datetime.now(KST)


def is_krx_session(target_date: date) -> bool:
    """Strictly classify an XKRX session date; calendar failure is not closed."""

    # ``datetime`` is intentionally replaceable in legacy startup tests, so use
    # an exact date check instead of consulting that module-level symbol here.
    if type(target_date) is not date:
        raise KrxCalendarError("XKRX session label must be a date")
    try:
        return bool(
            xcals.get_calendar("XKRX").is_session(target_date.isoformat())
        )
    except Exception as error:
        raise KrxCalendarError(
            f"XKRX session decision is unavailable: {error}"
        ) from error


def current_krx_session(now: datetime) -> Optional[date]:
    """Return the current regular-open XKRX session, otherwise ``None``."""

    instant = require_aware_kst(now)
    try:
        calendar = xcals.get_calendar("XKRX")
        if not calendar.is_open_on_minute(instant):
            return None
        label = calendar.minute_to_session(instant)
        return cast(date, label.date())
    except Exception as error:
        raise KrxCalendarError("current XKRX session is unavailable") from error


def next_krx_session(session_date: date) -> date:
    """Return the next XKRX session after a validated session label."""

    if not is_krx_session(session_date):
        raise KrxCalendarError("owning session date is not an XKRX session")
    try:
        label = xcals.get_calendar("XKRX").next_session(
            session_date.isoformat()
        )
        return cast(date, label.date())
    except Exception as error:
        raise KrxCalendarError("next XKRX session is unavailable") from error


def krx_session_ordinals(start: date, end: date) -> dict[date, int]:
    """Return bounded, calendar-derived ordinals (never calendar-day ordinals)."""
    if type(start) is not date or type(end) is not date or end < start:
        raise KrxCalendarError("invalid XKRX session range")
    try:
        calendar = xcals.get_calendar("XKRX")
        sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
        return {cast(date, label.date()): int(calendar.sessions.get_loc(label)) for label in sessions}
    except Exception as error:
        raise KrxCalendarError("XKRX session range is unavailable") from error


def require_krx_session_range(start: date, end: date) -> dict[date, int]:
    """Fail closed when either boundary is not a regular session."""
    ordinals = krx_session_ordinals(start, end)
    if start not in ordinals or end not in ordinals:
        raise KrxCalendarError("XKRX range boundaries must be regular sessions")
    return ordinals


def is_krx_open_on(target_date: date) -> bool:
    """Legacy conservative wrapper around the strict session classifier."""

    try:
        opened = is_krx_session(target_date)
    except KrxCalendarError as error:
        logger.error("[Market Cal] local calendar failure: %s", error)
        return False
    if not opened:
        logger.info("%s is not an XKRX session", target_date.isoformat())
    return opened


def is_krx_open_today() -> bool:
    """Check the system-local date through the legacy conservative adapter."""

    return is_krx_open_on(datetime.now().date())
