"""Shared HTTP headers for Kiwoom API transports."""

from __future__ import annotations

from typing import TypeVar

import requests


KIWOOM_USER_AGENT = "kiwoom-stock/1"

_Session = TypeVar("_Session", bound=requests.Session)


def configure_session(session: _Session) -> _Session:
    """Apply the bounded transport defaults shared by every Kiwoom session."""

    session.trust_env = False
    session.proxies = {}
    headers = getattr(session, "headers", None)
    if headers is not None:
        headers.update({"User-Agent": KIWOOM_USER_AGENT})
    return session
