"""Fail-closed Kiwoom OAuth token lifecycle."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
import re
from threading import RLock
import time
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

import requests

from kiwoom_stock.api.exceptions import KiwoomAuthError
from kiwoom_stock.application.credentials import (
    BearerToken,
    KiwoomClientCredentials,
)
from kiwoom_stock.api.http_headers import configure_session
from kiwoom_stock.settings import KiwoomEndpoint
from kiwoom_stock.utils import (
    register_sensitive_values,
    unregister_sensitive_values,
)


logger = logging.getLogger(__name__)
_SEOUL = ZoneInfo("Asia/Seoul")
_TOKEN_PATH = "/oauth2/token"
_REVOKE_PATH = "/oauth2/revoke"
_MAX_ISSUE_ATTEMPTS = 3
_FAILURE_CACHE_TTL = timedelta(seconds=1)
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")

UtcClock = Callable[[], datetime]
Sleeper = Callable[[float], None]


class RateLimitExceededError(KiwoomAuthError):
    """Token issuance remained rate limited after the bounded retry budget."""

    def __init__(self) -> None:
        super().__init__(
            "token issuance rate limit exceeded",
            status_code=429,
            category="rate_limited",
        )


class AuthenticatorClosedError(KiwoomAuthError):
    """The token owner was retired and cannot issue another token."""

    def __init__(self) -> None:
        super().__init__(
            "token owner is closed",
            category="owner_closed",
        )


class RevokeResult(str, Enum):
    """Observable result of one explicit ``au10002`` attempt."""

    REVOKED = "revoked"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, repr=False)
class _TokenLease:
    token: BearerToken
    expires_at: datetime
    usable_until: datetime
    generation: int


@dataclass(frozen=True, repr=False)
class AuthorizationSnapshot:
    """One authorization generation used for conditional 401 refresh."""

    header: str = field(repr=False)
    generation: int


@dataclass(frozen=True)
class _FailureSnapshot:
    message: str
    status_code: Optional[int]
    category: str
    retry_at: datetime
    rate_limited: bool = False

    def to_error(self) -> KiwoomAuthError:
        if self.rate_limited:
            return RateLimitExceededError()
        return KiwoomAuthError(
            self.message,
            status_code=self.status_code,
            category=self.category,
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _hardened_session(session: Optional[requests.Session]) -> requests.Session:
    result = session if session is not None else requests.Session()
    return configure_session(result)


class Authenticator:
    """Single owner for an in-memory Kiwoom access-token lease.

    Kiwoom publishes ``expires_dt`` without a timezone. Until an approved mock
    or staging validation confirms the provider semantics, this adapter applies
    the documented temporary policy: interpret ``%Y%m%d%H%M%S`` in
    ``Asia/Seoul`` and immediately normalize it to aware UTC.
    """

    def __init__(
        self,
        credentials: KiwoomClientCredentials,
        endpoint: KiwoomEndpoint,
        *,
        session: Optional[requests.Session] = None,
        clock: UtcClock = _utc_now,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        if not isinstance(credentials, KiwoomClientCredentials):
            raise TypeError("credentials must be KiwoomClientCredentials")
        if not isinstance(endpoint, KiwoomEndpoint):
            raise TypeError("endpoint must be KiwoomEndpoint")
        self._credentials: Optional[KiwoomClientCredentials] = credentials
        self._endpoint = endpoint
        self._owns_session = session is None
        self._session = _hardened_session(session)
        self._clock = clock
        self._sleeper = sleeper
        self._lock = RLock()
        self._lease: Optional[_TokenLease] = None
        self._failure: Optional[_FailureSnapshot] = None
        self._generation = 0
        self._closed = False
        self._credentials_registered = True
        register_sensitive_values(
            credentials.app_key.reveal_for_auth(),
            credentials.secret_key.reveal_for_auth(),
        )

    @property
    def endpoint(self) -> KiwoomEndpoint:
        """The typed, fixed-origin endpoint (never a free-form URL)."""

        return self._endpoint

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise KiwoomAuthError(
                "authentication clock must return an aware datetime",
                category="invalid_clock",
            )
        return now.astimezone(timezone.utc)

    def get_token(self) -> BearerToken:
        """Return a usable redacted token wrapper, refreshing single-flight."""

        with self._lock:
            if self._closed:
                raise AuthenticatorClosedError()
            now = self._now()
            if self._lease is not None and now < self._lease.usable_until:
                return self._lease.token
            if self._failure is not None and now < self._failure.retry_at:
                raise self._failure.to_error()
            self._failure = None
            self._drop_lease()
            try:
                self._lease = self._issue_token()
            except KiwoomAuthError as error:
                retry_at = self._now() + _FAILURE_CACHE_TTL
                self._failure = _FailureSnapshot(
                    message=error.message,
                    status_code=error.status_code,
                    category=error.category,
                    retry_at=retry_at,
                    rate_limited=isinstance(error, RateLimitExceededError),
                )
                raise self._failure.to_error() from None
            return self._lease.token

    def authorization_header(self) -> str:
        """Assemble the only permitted reveal of the current bearer token."""

        token = self.get_token()
        return f"Bearer {token.reveal_for_authorization()}"

    def authorization_snapshot(self) -> AuthorizationSnapshot:
        """Return a header together with the current lease generation."""

        with self._lock:
            token = self.get_token()
            lease = self._lease
            if lease is None:
                raise KiwoomAuthError(
                    "authorization lease is unavailable",
                    category="internal_state",
                )
            return AuthorizationSnapshot(
                header=f"Bearer {token.reveal_for_authorization()}",
                generation=lease.generation,
            )

    def refresh_after_401(self, rejected_generation: int) -> AuthorizationSnapshot:
        """Refresh only if the rejected generation is still current."""

        with self._lock:
            if self._closed:
                raise AuthenticatorClosedError()
            lease = self._lease
            if lease is not None and lease.generation == rejected_generation:
                self._drop_lease()
            return self.authorization_snapshot()

    def ensure_ready(self) -> None:
        """Explicitly prove authentication readiness without socket probing."""

        self.get_token()

    def invalidate(self) -> None:
        """Drop the local lease so a later read-only request may refresh once."""

        with self._lock:
            if self._closed:
                raise AuthenticatorClosedError()
            self._drop_lease()
            self._failure = None

    def close(self) -> None:
        """Retire local ownership without performing a network revoke."""

        with self._lock:
            if self._closed:
                return
            self._drop_lease()
            self._failure = None
            self._closed = True
            self._release_credentials()
            if self._owns_session:
                self._session.close()

    def revoke(self) -> RevokeResult:
        """Attempt ``au10002`` once, then permanently retire this owner.

        Timeout, HTTP failure, invalid JSON, or an ambiguous response is
        ``UNKNOWN`` and is never retried automatically. Local ownership is
        cleared before the request, so every outcome refuses future issuance.
        """

        with self._lock:
            if self._closed:
                return RevokeResult.UNKNOWN
            lease = self._lease
            self._lease = None
            self._failure = None
            self._closed = True
            if lease is None:
                self._release_credentials()
                if self._owns_session:
                    self._session.close()
                return RevokeResult.UNKNOWN

            raw_token = lease.token.reveal_for_authorization()
            credentials = self._require_credentials()
            payload = {
                "appkey": credentials.app_key.reveal_for_auth(),
                "secretkey": credentials.secret_key.reveal_for_auth(),
                "token": raw_token,
            }
            headers = {
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "au10002",
                "authorization": f"Bearer {raw_token}",
            }
            try:
                try:
                    response = self._session.post(
                        f"{self._endpoint.value}{_REVOKE_PATH}",
                        headers=headers,
                        json=payload,
                        timeout=10,
                        allow_redirects=False,
                        verify=True,
                    )
                except requests.RequestException:
                    logger.warning("Kiwoom token revoke result is unknown: transport failure")
                    return RevokeResult.UNKNOWN

                if response.status_code != 200:
                    logger.warning(
                        "Kiwoom token revoke result is unknown: HTTP status=%d",
                        response.status_code,
                    )
                    return RevokeResult.UNKNOWN
                data = self._safe_json(response)
                if data is None:
                    return RevokeResult.UNKNOWN
                return_code = data.get("return_code")
                if isinstance(return_code, bool) or not isinstance(return_code, int):
                    return RevokeResult.UNKNOWN
                if return_code == 0:
                    return RevokeResult.REVOKED
                return RevokeResult.REJECTED
            finally:
                unregister_sensitive_values(raw_token)
                self._release_credentials()
                if self._owns_session:
                    self._session.close()

    def _drop_lease(self) -> None:
        lease = self._lease
        self._lease = None
        if lease is not None:
            unregister_sensitive_values(lease.token.reveal_for_authorization())

    def _release_credentials(self) -> None:
        if not self._credentials_registered:
            return
        credentials = self._credentials
        if credentials is not None:
            unregister_sensitive_values(
                credentials.app_key.reveal_for_auth(),
                credentials.secret_key.reveal_for_auth(),
            )
        self._credentials = None
        self._credentials_registered = False

    def _require_credentials(self) -> KiwoomClientCredentials:
        credentials = self._credentials
        if credentials is None:
            raise AuthenticatorClosedError()
        return credentials

    def _issue_token(self) -> _TokenLease:
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        }
        credentials = self._require_credentials()
        payload = {
            "grant_type": "client_credentials",
            "appkey": credentials.app_key.reveal_for_auth(),
            "secretkey": credentials.secret_key.reveal_for_auth(),
        }
        response: Optional[requests.Response] = None
        for attempt in range(1, _MAX_ISSUE_ATTEMPTS + 1):
            try:
                response = self._session.post(
                    f"{self._endpoint.value}{_TOKEN_PATH}",
                    headers=headers,
                    json=payload,
                    timeout=10,
                    allow_redirects=False,
                    verify=True,
                )
            except requests.Timeout:
                raise KiwoomAuthError(
                    "token issuance timed out",
                    category="timeout",
                ) from None
            except requests.RequestException:
                raise KiwoomAuthError(
                    "token issuance transport failed",
                    category="transport",
                ) from None

            if response.status_code != 429:
                break
            logger.warning(
                "Kiwoom token issuance rate limited: attempt=%d/%d",
                attempt,
                _MAX_ISSUE_ATTEMPTS,
            )
            if attempt < _MAX_ISSUE_ATTEMPTS:
                self._sleeper(float(2 ** attempt))
        if response is None:
            raise KiwoomAuthError(
                "token issuance did not produce a response",
                category="internal_state",
            )
        if response.status_code == 429:
            raise RateLimitExceededError()
        if response.status_code != 200:
            raise KiwoomAuthError(
                "token issuance HTTP failure",
                status_code=response.status_code,
                category="http_status",
            )

        data = self._safe_json(response)
        if data is None:
            raise KiwoomAuthError(
                "token issuance returned invalid JSON",
                category="invalid_json",
            )
        return self._parse_lease(data, self._now())

    @staticmethod
    def _safe_json(response: requests.Response) -> Optional[Mapping[str, Any]]:
        try:
            data = response.json()
        except (ValueError, requests.RequestException):
            return None
        if not isinstance(data, Mapping):
            return None
        return data

    def _parse_lease(self, data: Mapping[str, Any], now: datetime) -> _TokenLease:
        return_code = data.get("return_code")
        if (
            isinstance(return_code, bool)
            or not isinstance(return_code, int)
            or return_code != 0
        ):
            raise KiwoomAuthError(
                "token issuance was rejected",
                category="provider_rejected",
            ) from None

        raw_token = data.get("token")
        if (
            not isinstance(raw_token, str)
            or not raw_token
            or raw_token != raw_token.strip()
            or _BEARER_TOKEN.fullmatch(raw_token) is None
        ):
            raise KiwoomAuthError(
                "token issuance response is missing a token",
                category="invalid_contract",
            )
        token_type = data.get("token_type")
        if not isinstance(token_type, str) or token_type.casefold() != "bearer":
            raise KiwoomAuthError(
                "token issuance response has an invalid token type",
                category="invalid_contract",
            )
        expires_dt = data.get("expires_dt")
        if (
            not isinstance(expires_dt, str)
            or len(expires_dt) != 14
            or not expires_dt.isascii()
            or not expires_dt.isdigit()
        ):
            raise KiwoomAuthError(
                "token issuance response has invalid expires_dt",
                category="invalid_contract",
            )
        try:
            local_expiry = datetime.strptime(expires_dt, "%Y%m%d%H%M%S").replace(
                tzinfo=_SEOUL
            )
        except ValueError:
            raise KiwoomAuthError(
                "token issuance response has malformed expires_dt",
                category="invalid_contract",
            ) from None
        expires_at = local_expiry.astimezone(timezone.utc)
        ttl = expires_at - now
        if ttl <= timedelta(0):
            raise KiwoomAuthError(
                "token issuance response has a non-future expiry",
                category="invalid_contract",
            )
        refresh_skew = min(timedelta(minutes=5), ttl * 0.1)
        token = BearerToken(raw_token)
        register_sensitive_values(raw_token)
        self._generation += 1
        return _TokenLease(
            token=token,
            expires_at=expires_at,
            usable_until=expires_at - refresh_skew,
            generation=self._generation,
        )
