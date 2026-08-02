"""Bounded Kiwoom market-only transport used by validation and shadow runs."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
import threading
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import requests

from kiwoom_stock.api.auth import Authenticator, Sleeper, UtcClock, _utc_now
from kiwoom_stock.api.base import BaseClient, is_valid_bearer_authorization
from kiwoom_stock.api.services.market import MarketService
from kiwoom_stock.application.credentials import KiwoomClientCredentials
from kiwoom_stock.settings import KiwoomEndpoint


MAX_HTTP_ATTEMPTS = 23
_STRICT_NUMERIC = re.compile(
    r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$"
)


def _clamp_timeout(timeout: Any, remaining: float) -> Any:
    """Keep transport timeout inside the remaining cooperative budget."""

    if remaining <= 0:
        raise ReadOnlyBoundaryError("shadow shutdown deadline exceeded")
    if isinstance(timeout, tuple) and len(timeout) == 2:
        return (min(float(timeout[0]), remaining), min(float(timeout[1]), remaining))
    return min(float(timeout), remaining)


class ValidationError(RuntimeError):
    """Safe market-only contract failure shared with the live validator."""


class ReadOnlyBoundaryError(ValidationError):
    """The shared HTTP session rejected an unapproved attempt."""


class ResponseSender(Protocol):
    def __call__(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> requests.Response: ...


class AllowlistedReadOnlySession(requests.Session):
    """Shared Auth/Base session with an exact URL/API/payload allowlist."""

    def __init__(
        self,
        *,
        stock_code: str,
        proxy_code: str,
        max_attempts: int = MAX_HTTP_ATTEMPTS,
        sender: ResponseSender | None = None,
        terminate_on_rate_limit: bool = False,
        stop_event: threading.Event | None = None,
        deadline_remaining: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or not 1 <= max_attempts <= MAX_HTTP_ATTEMPTS
        ):
            raise ValueError("max_attempts must be an integer from 1 to 23")
        self._transport = requests.Session()
        self._transport.trust_env = False
        self._transport.proxies = {}
        self._stock_code = stock_code
        self._proxy_code = proxy_code
        self._max_attempts = max_attempts
        self._sender = sender
        self._terminate_on_rate_limit = terminate_on_rate_limit
        self._stop_event = stop_event
        self._deadline_remaining = deadline_remaining
        self._attempts = 0
        self._counts = {
            "token": 0,
            "stock_basic": 0,
            "stock_chart_5m": 0,
            "proxy_chart_60m": 0,
            "stock_strength": 0,
            "stock_orderbook": 0,
        }
        self._closed = False

    @property
    def attempt_count(self) -> int:
        return self._attempts

    def safe_counts(self) -> dict[str, int]:
        return dict(self._counts)

    def post(  # type: ignore[override]
        self,
        url: str,
        *args: Any,
        data: Any = None,
        json: Any = None,
        **kwargs: Any,
    ) -> requests.Response:
        if args:
            raise ReadOnlyBoundaryError("positional HTTP arguments rejected")
        return self.request(
            "POST",
            url,
            data=data,
            json=json,
            **kwargs,
        )

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        if args:
            raise ReadOnlyBoundaryError("positional HTTP arguments rejected")
        remaining = self._check_lifecycle()
        if self._closed:
            raise ReadOnlyBoundaryError("market-only session is closed")
        self._attempts += 1
        if self._attempts > self._max_attempts:
            raise ReadOnlyBoundaryError("HTTP attempt budget exceeded")
        label = self._classify(method, url, kwargs)
        self._counts[label] += 1
        if remaining is not None:
            kwargs = dict(kwargs)
            kwargs["timeout"] = _clamp_timeout(kwargs["timeout"], remaining)
        response = (
            self._sender(method, url, **kwargs)
            if self._sender is not None
            else self._transport.request(method, url, **kwargs)
        )
        self._check_lifecycle()
        if self._terminate_on_rate_limit and response.status_code == 429:
            raise ReadOnlyBoundaryError("rate limit response terminated the shadow run")
        return response

    def _check_lifecycle(self) -> float | None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise ReadOnlyBoundaryError("shadow stop requested")
        if self._deadline_remaining is not None:
            try:
                return self._deadline_remaining()
            except Exception as error:
                raise ReadOnlyBoundaryError("shadow shutdown deadline exceeded") from error
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._transport.close()
        super().close()

    def send(self, request: Any, **kwargs: Any) -> requests.Response:
        raise ReadOnlyBoundaryError("direct prepared-request send is forbidden")

    def _classify(self, method: str, url: str, kwargs: Mapping[str, Any]) -> str:
        if method.upper() != "POST":
            raise ReadOnlyBoundaryError("non-POST HTTP attempt rejected")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.kiwoom.com"
            or parsed.query
            or parsed.fragment
        ):
            raise ReadOnlyBoundaryError("HTTP origin/path rejected")
        headers = kwargs.get("headers")
        payload = kwargs.get("json")
        required_kwargs = {
            "headers", "json", "data", "timeout", "allow_redirects", "verify"
        }
        if set(kwargs) != required_kwargs:
            raise ReadOnlyBoundaryError("HTTP request kwargs rejected")
        if kwargs.get("data") is not None:
            raise ReadOnlyBoundaryError("duplicate HTTP body rejected")
        if kwargs.get("allow_redirects") is not False:
            raise ReadOnlyBoundaryError("HTTP redirects must be disabled")
        if kwargs.get("verify") is not True:
            raise ReadOnlyBoundaryError("TLS verification must remain enabled")
        if not isinstance(headers, Mapping) or not isinstance(payload, Mapping):
            raise ReadOnlyBoundaryError("HTTP request shape rejected")
        pair = (parsed.path, headers.get("api-id"))
        if pair == ("/oauth2/token", "au10001"):
            if kwargs.get("timeout") != 10:
                raise ReadOnlyBoundaryError("token timeout rejected")
            if set(headers) != {"Content-Type", "api-id"}:
                raise ReadOnlyBoundaryError("token headers rejected")
            if headers.get("Content-Type") != "application/json;charset=UTF-8":
                raise ReadOnlyBoundaryError("token content type rejected")
            if (
                set(payload) != {"grant_type", "appkey", "secretkey"}
                or payload.get("grant_type") != "client_credentials"
            ):
                raise ReadOnlyBoundaryError("token request shape rejected")
            return "token"
        if kwargs.get("timeout") != (5, 30):
            raise ReadOnlyBoundaryError("market timeout rejected")
        if pair == ("/api/dostk/stkinfo", "ka10001"):
            self._require_market_headers(headers)
            self._require_stock_payload(payload)
            return "stock_basic"
        if pair == ("/api/dostk/chart", "ka10080"):
            self._require_market_headers(headers)
            return self._classify_chart(payload)
        if pair == ("/api/dostk/mrkcond", "ka10046"):
            self._require_market_headers(headers)
            self._require_stock_payload(payload)
            return "stock_strength"
        if pair == ("/api/dostk/mrkcond", "ka10004"):
            self._require_market_headers(headers)
            self._require_stock_payload(payload)
            return "stock_orderbook"
        raise ReadOnlyBoundaryError("API path/id pair rejected")

    @staticmethod
    def _require_market_headers(headers: Mapping[str, Any]) -> None:
        if set(headers) != {"Content-Type", "api-id", "authorization"}:
            raise ReadOnlyBoundaryError("market headers rejected")
        if headers.get("Content-Type") != "application/json;charset=UTF-8":
            raise ReadOnlyBoundaryError("market content type rejected")
        authorization = headers.get("authorization")
        if not is_valid_bearer_authorization(authorization):
            raise ReadOnlyBoundaryError("market authorization rejected")

    def _require_stock_payload(self, payload: Mapping[str, Any]) -> None:
        if dict(payload) != {"stk_cd": self._stock_code}:
            raise ReadOnlyBoundaryError("stock payload rejected")

    def _classify_chart(self, payload: Mapping[str, Any]) -> str:
        expected_keys = {"stk_cd", "tic_scope", "upd_stkpc_tp"}
        if set(payload) != expected_keys or payload.get("upd_stkpc_tp") != "1":
            raise ReadOnlyBoundaryError("chart payload rejected")
        if payload.get("stk_cd") == self._stock_code and payload.get("tic_scope") == "5":
            return "stock_chart_5m"
        if payload.get("stk_cd") == self._proxy_code and payload.get("tic_scope") == "60":
            return "proxy_chart_60m"
        raise ReadOnlyBoundaryError("only stock 5m and proxy 60m charts are allowed")


class MarketOnlyClient:
    """Composition root exposing market reads but no account/order/revoke surface."""

    def __init__(
        self,
        credentials: KiwoomClientCredentials,
        *,
        session: AllowlistedReadOnlySession,
        clock: UtcClock = _utc_now,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._session = session
        self._authenticator = Authenticator(
            credentials,
            KiwoomEndpoint.PROD,
            session=session,
            clock=clock,
            sleeper=sleeper,
        )
        self._base_client = BaseClient(
            self._authenticator,
            KiwoomEndpoint.PROD,
            session=session,
            sleeper=sleeper,
        )
        self.market = MarketService(self._base_client)
        self._closed = False

    @property
    def attempt_count(self) -> int:
        return self._session.attempt_count

    def safe_counts(self) -> dict[str, int]:
        return self._session.safe_counts()

    def ensure_auth_ready(self) -> None:
        self._authenticator.ensure_ready()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._authenticator.close()
        finally:
            self._base_client.close()
            self._session.close()


@dataclass(frozen=True)
class MarketSnapshot:
    """Validated in-memory input for one calculation cycle."""

    basic: Mapping[str, Any] = field(repr=False)
    stock_chart: Sequence[Mapping[str, Any]] = field(repr=False)
    proxy_chart: Sequence[Mapping[str, Any]] = field(repr=False)
    strength: Sequence[Mapping[str, Any]] = field(repr=False)
    order_book: Mapping[str, Any] = field(repr=False)


def _finite(record: Mapping[str, Any], key: str, location: str) -> float:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")
    if isinstance(value, str):
        if _STRICT_NUMERIC.fullmatch(value) is None:
            raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")
        value = value.replace(",", "")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}") from None
    if not math.isfinite(result):
        raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")
    return result


def _positive(record: Mapping[str, Any], key: str, location: str, *, magnitude: bool = False) -> None:
    value = _finite(record, key, location)
    if (abs(value) if magnitude else value) <= 0.0:
        raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")


def _validate_chart(chart: Sequence[Mapping[str, Any]], location: str) -> None:
    if len(chart) < 14:
        raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")
    for row in chart:
        if not isinstance(row, Mapping):
            raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}")
        for key in ("cur_prc", "open_pric", "high_pric", "low_pric"):
            _positive(row, key, f"{location}.{key}", magnitude=True)
        if abs(_finite(row, "trde_qty", f"{location}.trde_qty")) < 0.0:
            raise ReadOnlyBoundaryError(f"market snapshot contract failed: {location}.trde_qty")


def validate_market_snapshot(snapshot: MarketSnapshot) -> None:
    for key in ("cur_prc", "trde_pre", "trde_qty"):
        _positive(snapshot.basic, key, f"basic.{key}", magnitude=True)
    _positive(snapshot.basic, "mac", "basic.mac")
    _validate_chart(snapshot.stock_chart, "stock_chart")
    _validate_chart(snapshot.proxy_chart, "proxy_chart")
    if len(snapshot.strength) < 5:
        raise ReadOnlyBoundaryError("market snapshot contract failed: strength")
    for index in (0, 4):
        row = snapshot.strength[index]
        if not isinstance(row, Mapping):
            raise ReadOnlyBoundaryError(
                f"market snapshot contract failed: strength.row{index}"
            )
        _positive(row, "cntr_str", f"strength.row{index}.cntr_str")
    sell = _finite(snapshot.order_book, "tot_sel_req", "orderbook.tot_sel_req")
    buy = _finite(snapshot.order_book, "tot_buy_req", "orderbook.tot_buy_req")
    if sell < 0.0 or buy < 0.0 or (sell == 0.0 and buy == 0.0):
        raise ReadOnlyBoundaryError("market snapshot contract failed: orderbook.totals")


def fetch_market_snapshot(
    client: MarketOnlyClient,
    *,
    stock_code: str,
    proxy_code: str,
) -> MarketSnapshot:
    """Fetch the exact five logical market reads, then validate raw inputs."""

    client.ensure_auth_ready()
    snapshot = MarketSnapshot(
        basic=dict(client.market.get_stock_basic_info(stock_code)),
        stock_chart=list(client.market.get_minute_chart(stock_code, "5")),
        proxy_chart=list(client.market.get_minute_chart(proxy_code, "60")),
        strength=list(client.market.get_tick_strength(stock_code)),
        order_book=dict(client.market.get_order_book(stock_code)),
    )
    validate_market_snapshot(snapshot)
    return snapshot


@dataclass
class CachedMarketGateway:
    """Serve one validated snapshot without performing further I/O."""

    stock_code: str
    proxy_code: str
    snapshot: MarketSnapshot = field(repr=False)
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get_stock_basic_info(self, stock_code: str) -> Mapping[str, Any]:
        self.calls.append(("basic", stock_code, None))
        if stock_code != self.stock_code:
            raise ReadOnlyBoundaryError("cached basic request rejected")
        return self.snapshot.basic

    def get_minute_chart(self, stock_code: str, tic: str) -> list[Mapping[str, Any]]:
        self.calls.append(("chart", stock_code, tic))
        if (stock_code, tic) == (self.stock_code, "5"):
            return list(self.snapshot.stock_chart)
        if (stock_code, tic) == (self.proxy_code, "60"):
            return list(self.snapshot.proxy_chart)
        raise ReadOnlyBoundaryError("cached chart request rejected")

    def get_tick_strength(self, stock_code: str) -> list[Mapping[str, Any]]:
        self.calls.append(("strength", stock_code, None))
        if stock_code != self.stock_code:
            raise ReadOnlyBoundaryError("cached strength request rejected")
        return list(self.snapshot.strength)

    def get_order_book(self, stock_code: str) -> Mapping[str, Any]:
        self.calls.append(("orderbook", stock_code, None))
        if stock_code != self.stock_code:
            raise ReadOnlyBoundaryError("cached order-book request rejected")
        return self.snapshot.order_book

    def _forbidden(self) -> Sequence[Mapping[str, Any]]:
        raise ReadOnlyBoundaryError("market discovery method is outside shadow scope")

    def get_top_trading_value(self, market_tp: str = "001") -> Sequence[Mapping[str, Any]]:
        return self._forbidden()

    def get_program_trade(self) -> Sequence[Mapping[str, Any]]:
        return self._forbidden()

    def get_foreign_window_total(self) -> Sequence[Mapping[str, Any]]:
        return self._forbidden()

    def get_recent_ticks(self, stock_code: str) -> Sequence[Mapping[str, Any]]:
        return self._forbidden()
