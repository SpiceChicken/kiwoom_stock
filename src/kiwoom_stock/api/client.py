"""Kiwoom API composition with lazy, explicit authentication readiness."""

from typing import Optional
import time

import requests

from kiwoom_stock.api.auth import Authenticator, Sleeper, UtcClock, _utc_now
from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.services.market import MarketService
from kiwoom_stock.application.credentials import KiwoomClientCredentials
from kiwoom_stock.settings import KiwoomEndpoint


class KiwoomClient:
    """Compose one hardened session and one process-local token owner.

    Construction performs no DNS, socket, or HTTP work. Call
    :meth:`ensure_auth_ready` when eager readiness is explicitly required;
    ordinary read paths authenticate lazily through ``BaseClient``.
    """

    def __init__(
        self,
        *,
        credentials: KiwoomClientCredentials,
        endpoint: KiwoomEndpoint,
        session: Optional[requests.Session] = None,
        clock: UtcClock = _utc_now,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._owns_session = session is None
        shared_session = session if session is not None else requests.Session()
        shared_session.trust_env = False
        self._session = shared_session
        self._auth = Authenticator(
            credentials,
            endpoint,
            session=shared_session,
            clock=clock,
            sleeper=sleeper,
        )
        self._base = BaseClient(
            self._auth,
            endpoint,
            session=shared_session,
            sleeper=sleeper,
        )
        self.market = MarketService(self._base)
        self._closed = False

    def ensure_auth_ready(self) -> None:
        """Explicitly issue/validate a token without redundant socket probing."""

        self._auth.ensure_ready()

    def close(self) -> None:
        """Clear local token state; never auto-revoke over the network."""

        if self._closed:
            return
        self._closed = True
        try:
            self._auth.close()
        finally:
            if self._owns_session:
                self._session.close()
