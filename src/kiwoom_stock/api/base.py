"""Hardened Kiwoom API transport."""

import logging
import re
import time
from typing import Any, Callable, Mapping, Optional, Protocol
from urllib.parse import urlsplit

import requests

from kiwoom_stock.api.auth import AuthorizationSnapshot
from kiwoom_stock.api.exceptions import KiwoomAPIError, KiwoomAPIResponseError
from kiwoom_stock.settings import KiwoomEndpoint


logger = logging.getLogger(__name__)
_API_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_AUTHORIZATION = re.compile(r"^Bearer [A-Za-z0-9\-._~+/]+=*$")
_RETRYABLE_READ_STATUS = frozenset({429, 500, 502, 503, 504})


class TokenAuthenticator(Protocol):
    def authorization_snapshot(self) -> AuthorizationSnapshot:
        """Return a non-empty Bearer value and its token generation."""

    def refresh_after_401(
        self,
        rejected_generation: int,
    ) -> AuthorizationSnapshot:
        """Refresh only when the rejected generation remains current."""


def is_valid_bearer_authorization(value: object) -> bool:
    """Return whether a value satisfies the shared Kiwoom Bearer grammar."""

    return (
        isinstance(value, str)
        and _AUTHORIZATION.fullmatch(value) is not None
        and value.casefold() != "bearer none"
    )


class BaseClient:
    """POST transport restricted to a typed Kiwoom origin.

    Kiwoom models reads as POST requests. A caller must explicitly declare
    ``read_only=True`` before transport retry is possible; order or unknown
    semantics are sent at most once.
    """

    def __init__(
        self,
        authenticator: TokenAuthenticator,
        endpoint: KiwoomEndpoint,
        *,
        session: Optional[requests.Session] = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(endpoint, KiwoomEndpoint):
            raise TypeError("endpoint must be KiwoomEndpoint")
        self._auth = authenticator
        self._endpoint = endpoint
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._session.trust_env = False
        self._session.proxies = {}
        self._sleeper = sleeper
        self._closed = False

    def close(self) -> None:
        """Close only a standalone transport-owned Session."""

        if self._closed:
            return
        self._closed = True
        if self._owns_session:
            self._session.close()

    def request(
        self,
        endpoint: str,
        api_id: str,
        payload: Mapping[str, Any],
        max_retries: int = 3,
        *,
        read_only: bool = False,
    ) -> Mapping[str, Any]:
        """Send one typed-origin request and return a validated mapping."""

        if self._closed:
            raise KiwoomAPIError(
                "Kiwoom transport is closed",
                category="transport_closed",
            )
        path = self._validate_path(endpoint)
        if not isinstance(api_id, str) or _API_ID.fullmatch(api_id) is None:
            raise ValueError("api_id must use the supported identifier form")
        if (
            isinstance(max_retries, bool)
            or not isinstance(max_retries, int)
            or not 1 <= max_retries <= 3
        ):
            raise ValueError("max_retries must be an integer from 1 to 3")
        attempts = max_retries if read_only else 1
        authorization = self._auth.authorization_snapshot()
        headers = self._headers(api_id, authorization.header)
        url = f"{self._endpoint.value}{path}"

        attempt = 1
        auth_replayed = False
        while attempt <= attempts:
            try:
                response = self._session.post(
                    url,
                    headers=headers,
                    json=dict(payload),
                    timeout=(5, 30),
                    allow_redirects=False,
                    verify=True,
                )
            except (requests.ConnectTimeout, requests.ReadTimeout):
                if attempt < attempts:
                    self._retry_wait("timeout", attempt, attempts)
                    attempt += 1
                    continue
                raise KiwoomAPIError(
                    "Kiwoom request timed out",
                    category="timeout",
                ) from None
            except requests.ConnectionError:
                if attempt < attempts:
                    self._retry_wait("connection", attempt, attempts)
                    attempt += 1
                    continue
                raise KiwoomAPIError(
                    "Kiwoom connection failed",
                    category="connection",
                ) from None
            except requests.RequestException:
                raise KiwoomAPIError(
                    "Kiwoom transport failed",
                    category="transport",
                ) from None

            if response.status_code == 401 and read_only and not auth_replayed:
                authorization = self._auth.refresh_after_401(
                    authorization.generation
                )
                headers = self._headers(api_id, authorization.header)
                auth_replayed = True
                continue
            if (
                read_only
                and response.status_code in _RETRYABLE_READ_STATUS
                and attempt < attempts
            ):
                self._retry_wait("http_status", attempt, attempts)
                attempt += 1
                continue
            if response.status_code != 200:
                raise KiwoomAPIError(
                    "Kiwoom HTTP failure",
                    status_code=response.status_code,
                    category="http_status",
                )

            try:
                data = response.json()
            except (ValueError, requests.RequestException):
                raise KiwoomAPIError(
                    "Kiwoom response was not valid JSON",
                    category="invalid_json",
                ) from None
            if not isinstance(data, Mapping):
                raise KiwoomAPIError(
                    "Kiwoom response must be an object",
                    category="invalid_contract",
                )
            return_code = data.get("return_code")
            if (
                isinstance(return_code, bool)
                or not isinstance(return_code, int)
                or return_code != 0
            ):
                safe_code = (
                    return_code
                    if isinstance(return_code, int) and not isinstance(return_code, bool)
                    else None
                )
                raise KiwoomAPIResponseError(
                    "Kiwoom API rejected the request",
                    return_code=safe_code,
                )
            return data

        raise KiwoomAPIError(
            "Kiwoom request retry state was exhausted",
            category="internal_state",
        )

    def _headers(self, api_id: str, authorization: str) -> dict[str, str]:
        if not is_valid_bearer_authorization(authorization):
            raise KiwoomAPIError(
                "authenticator returned an invalid authorization value",
                category="invalid_authorization",
            )
        return {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": authorization,
        }

    def _retry_wait(self, category: str, attempt: int, attempts: int) -> None:
        delay = float(attempt * 2)
        logger.warning(
            "Retrying read-only Kiwoom request: category=%s attempt=%d/%d",
            category,
            attempt,
            attempts,
        )
        self._sleeper(delay)

    @staticmethod
    def _validate_path(value: str) -> str:
        if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
            raise ValueError("endpoint must be an origin-relative absolute path")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("endpoint must not override the typed origin")
        return value
