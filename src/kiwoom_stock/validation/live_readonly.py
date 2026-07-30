"""Strict production Kiwoom market-only validation.

The command constructs only Authenticator, BaseClient, and MarketService. It
does not expose account, revoke, order, database, notification, or report
capabilities.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, Iterator, Mapping, Protocol, Sequence
from urllib.parse import urlsplit

import requests

from kiwoom_stock.api.auth import Authenticator, Sleeper, UtcClock, _utc_now
from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.exceptions import KiwoomAPIError
from kiwoom_stock.api.services.market import MarketService
from kiwoom_stock.application.credentials import KiwoomClientCredentials
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.models import MarketRegime
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.settings import KiwoomEndpoint


MAX_HTTP_ATTEMPTS = 23
REGIME_PROXY_CODE = "069500"
EXPECTED_FORCE_KEYS = frozenset(
    {
        "thrust",
        "gravity",
        "drag",
        "magnetic",
        "jerk",
        "impulse",
        "net_force",
        "current_velocity",
        "volume_drop_ratio",
    }
)
EXPECTED_LOGICAL_SEQUENCE = (
    "stock_basic",
    "stock_chart_5m",
    "proxy_chart_60m",
    "stock_strength",
    "stock_orderbook",
)
_DEPENDENCY_LOGGERS = (
    "kiwoom_stock.monitoring.analyzer",
    "kiwoom_stock.monitoring.collector",
)
_STRICT_NUMERIC = re.compile(
    r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$"
)


class ValidationError(RuntimeError):
    """A safe operational validation failure."""


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
    ) -> None:
        super().__init__()
        self.trust_env = False
        self.proxies = {}
        self._stock_code = stock_code
        self._proxy_code = proxy_code
        self._max_attempts = max_attempts
        self._sender = sender
        self._attempts = 0
        self._counts = {
            "token": 0,
            "stock_basic": 0,
            "stock_chart_5m": 0,
            "proxy_chart_60m": 0,
            "stock_strength": 0,
            "stock_orderbook": 0,
        }

    @property
    def attempt_count(self) -> int:
        return self._attempts

    def safe_counts(self) -> dict[str, int]:
        return dict(self._counts)

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        *args: Any,
        **kwargs: Any,
    ) -> requests.Response:
        self._attempts += 1
        if self._attempts > self._max_attempts:
            raise ReadOnlyBoundaryError("HTTP attempt budget exceeded")
        label = self._classify(method, url, kwargs)
        self._counts[label] += 1
        if self._sender is not None:
            return self._sender(method, url, **kwargs)
        return super().request(method, url, *args, **kwargs)

    def _classify(
        self,
        method: str,
        url: str,
        kwargs: Mapping[str, Any],
    ) -> str:
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
        if (
            not isinstance(headers, Mapping)
            or not isinstance(payload, Mapping)
        ):
            raise ReadOnlyBoundaryError("HTTP request shape rejected")
        api_id = headers.get("api-id")
        pair = (parsed.path, api_id)

        if pair == ("/oauth2/token", "au10001"):
            if (
                set(payload) != {"grant_type", "appkey", "secretkey"}
                or payload.get("grant_type") != "client_credentials"
            ):
                raise ReadOnlyBoundaryError("token request shape rejected")
            return "token"
        if pair == ("/api/dostk/stkinfo", "ka10001"):
            self._require_stock_payload(payload)
            return "stock_basic"
        if pair == ("/api/dostk/chart", "ka10080"):
            return self._classify_chart(payload)
        if pair == ("/api/dostk/mrkcond", "ka10046"):
            self._require_stock_payload(payload)
            return "stock_strength"
        if pair == ("/api/dostk/mrkcond", "ka10004"):
            self._require_stock_payload(payload)
            return "stock_orderbook"
        raise ReadOnlyBoundaryError("API path/id pair rejected")

    def _require_stock_payload(self, payload: Mapping[str, Any]) -> None:
        if dict(payload) != {"stk_cd": self._stock_code}:
            raise ReadOnlyBoundaryError("stock payload rejected")

    def _classify_chart(self, payload: Mapping[str, Any]) -> str:
        expected_tail = {"upd_stkpc_tp": "1"}
        if (
            payload.get("stk_cd") == self._stock_code
            and payload.get("tic_scope") == "5"
            and payload.get("upd_stkpc_tp") == expected_tail["upd_stkpc_tp"]
            and set(payload) == {"stk_cd", "tic_scope", "upd_stkpc_tp"}
        ):
            return "stock_chart_5m"
        if (
            payload.get("stk_cd") == self._proxy_code
            and payload.get("tic_scope") == "60"
            and payload.get("upd_stkpc_tp") == expected_tail["upd_stkpc_tp"]
            and set(payload) == {"stk_cd", "tic_scope", "upd_stkpc_tp"}
        ):
            return "proxy_chart_60m"
        raise ReadOnlyBoundaryError(
            "only stock 5m and proxy 60m charts are allowed"
        )


class MarketOnlyClient:
    """Composition root intentionally lacking AccountService/KiwoomClient."""

    def __init__(
        self,
        credentials: KiwoomClientCredentials,
        *,
        session: AllowlistedReadOnlySession,
        clock: UtcClock = _utc_now,
        sleeper: Sleeper = time.sleep,
    ) -> None:
        self._session = session
        self.auth = Authenticator(
            credentials,
            KiwoomEndpoint.PROD,
            session=session,
            clock=clock,
            sleeper=sleeper,
        )
        self.base = BaseClient(
            self.auth,
            KiwoomEndpoint.PROD,
            session=session,
            sleeper=sleeper,
        )
        self.market = MarketService(self.base)
        self._closed = False

    def ensure_auth_ready(self) -> None:
        self.auth.ensure_ready()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.auth.close()
        finally:
            self.base.close()
            self._session.close()


@dataclass
class MemoryStateRepository:
    latest: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    submissions: list[str] = field(default_factory=list)

    def get_last_physical_state(
        self,
        stock_code: str,
    ) -> Mapping[str, Any] | None:
        return self.latest.get(stock_code)

    def submit_physical_state(
        self,
        stock_code: str,
        forces: Mapping[str, Any],
    ) -> None:
        self.latest[stock_code] = dict(forces)
        self.submissions.append(stock_code)

    def close(self) -> None:
        self.latest.clear()


@dataclass
class CachedMarketGateway:
    stock_code: str
    proxy_code: str
    basic: Mapping[str, Any]
    stock_chart: Sequence[Mapping[str, Any]]
    proxy_chart: Sequence[Mapping[str, Any]]
    strength: Sequence[Mapping[str, Any]]
    order_book: Mapping[str, Any]
    calls: list[tuple[str, str, str | None]] = field(default_factory=list)

    def get_top_trading_value(
        self,
        market_tp: str = "001",
    ) -> Sequence[Mapping[str, Any]]:
        raise ValidationError("top-trading-value access is outside scope")

    def get_stock_basic_info(self, stock_code: str) -> Mapping[str, Any]:
        self.calls.append(("basic", stock_code, None))
        return self.basic

    def get_minute_chart(
        self,
        stock_code: str,
        tic: str,
    ) -> list[Mapping[str, Any]]:
        self.calls.append(("chart", stock_code, tic))
        if (stock_code, tic) == (self.stock_code, "5"):
            return list(self.stock_chart)
        if (stock_code, tic) == (self.proxy_code, "60"):
            return list(self.proxy_chart)
        raise ValidationError("cached chart request rejected")

    def get_tick_strength(
        self,
        stock_code: str,
    ) -> list[Mapping[str, Any]]:
        self.calls.append(("strength", stock_code, None))
        return list(self.strength)

    def get_program_trade(self) -> Sequence[Mapping[str, Any]]:
        raise ValidationError("program-trade access is outside scope")

    def get_foreign_window_total(self) -> Sequence[Mapping[str, Any]]:
        raise ValidationError("foreign-window access is outside scope")

    def get_order_book(self, stock_code: str) -> Mapping[str, Any]:
        self.calls.append(("orderbook", stock_code, None))
        return self.order_book

    def get_recent_ticks(
        self,
        stock_code: str,
    ) -> Sequence[Mapping[str, Any]]:
        raise ValidationError("recent-tick access is outside scope")


class _RedactDependencyErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.ERROR:
            record.msg = (
                "live validation dependency failure (details redacted)"
            )
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


@contextmanager
def _safe_dependency_logging() -> Iterator[None]:
    redactor = _RedactDependencyErrors()
    loggers = [logging.getLogger(name) for name in _DEPENDENCY_LOGGERS]
    for logger in loggers:
        logger.addFilter(redactor)
    try:
        yield
    finally:
        for logger in loggers:
            logger.removeFilter(redactor)


def _safe_error(error: BaseException) -> str:
    if isinstance(error, KiwoomAPIError):
        return f"{type(error).__name__}:{error.category}"
    return type(error).__name__


def _fetch_snapshot(
    client: MarketOnlyClient,
    *,
    stock_code: str,
    proxy_code: str,
) -> tuple[
    Mapping[str, Any],
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
    Sequence[Mapping[str, Any]],
    Mapping[str, Any],
    tuple[str, ...],
]:
    logical_calls: list[str] = []
    basic = dict(client.market.get_stock_basic_info(stock_code))
    logical_calls.append("stock_basic")
    stock_chart = list(client.market.get_minute_chart(stock_code, "5"))
    logical_calls.append("stock_chart_5m")
    proxy_chart = list(client.market.get_minute_chart(proxy_code, "60"))
    logical_calls.append("proxy_chart_60m")
    strength = list(client.market.get_tick_strength(stock_code))
    logical_calls.append("stock_strength")
    order_book = dict(client.market.get_order_book(stock_code))
    logical_calls.append("stock_orderbook")
    return (
        basic,
        stock_chart,
        proxy_chart,
        strength,
        order_book,
        tuple(logical_calls),
    )


def _positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} is not numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValidationError(f"{name} must be positive and finite")
    return normalized


def _required_finite(
    record: Mapping[str, Any],
    field_name: str,
    location: str,
) -> float:
    value = record.get(field_name)
    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, str),
    ):
        raise ValidationError(f"snapshot contract failed: {location}")
    if isinstance(value, str):
        if _STRICT_NUMERIC.fullmatch(value) is None:
            raise ValidationError(
                f"snapshot contract failed: {location}"
            )
        value = value.replace(",", "")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValidationError(
            f"snapshot contract failed: {location}"
        ) from None
    if not math.isfinite(normalized):
        raise ValidationError(f"snapshot contract failed: {location}")
    return normalized


def _require_positive(
    record: Mapping[str, Any],
    field_name: str,
    location: str,
    *,
    magnitude: bool = False,
) -> float:
    value = _required_finite(record, field_name, location)
    comparable = abs(value) if magnitude else value
    if comparable <= 0.0:
        raise ValidationError(f"snapshot contract failed: {location}")
    return value


def _require_nonnegative(
    record: Mapping[str, Any],
    field_name: str,
    location: str,
    *,
    magnitude: bool = False,
) -> float:
    value = _required_finite(record, field_name, location)
    comparable = abs(value) if magnitude else value
    if comparable < 0.0:
        raise ValidationError(f"snapshot contract failed: {location}")
    return value


def _validate_chart_contract(
    chart: Sequence[Mapping[str, Any]],
    location: str,
) -> None:
    if len(chart) < 14:
        raise ValidationError(f"snapshot contract failed: {location}")
    for row in chart:
        if not isinstance(row, Mapping):
            raise ValidationError(f"snapshot contract failed: {location}")
        for field_name in (
            "cur_prc",
            "open_pric",
            "high_pric",
            "low_pric",
        ):
            _require_positive(
                row,
                field_name,
                f"{location}.{field_name}",
                magnitude=True,
            )
        _require_nonnegative(
            row,
            "trde_qty",
            f"{location}.trde_qty",
            magnitude=True,
        )


def _validate_snapshot_contract(
    *,
    basic: Mapping[str, Any],
    stock_chart: Sequence[Mapping[str, Any]],
    proxy_chart: Sequence[Mapping[str, Any]],
    strength: Sequence[Mapping[str, Any]],
    order_book: Mapping[str, Any],
) -> None:
    """Reject incomplete raw inputs before legacy analyzer fallbacks run.

    Basic directional fields must have finite positive magnitude, market cap
    and strength must be finite and strictly positive. Kiwoom chart prices may
    carry a direction sign, so their absolute values must be positive; chart
    volume must have finite nonnegative magnitude. Order-book totals must be
    finite and nonnegative, with at least one positive side.
    """

    for field_name in ("cur_prc", "trde_pre", "trde_qty"):
        _require_positive(
            basic,
            field_name,
            f"basic.{field_name}",
            magnitude=True,
        )
    _require_positive(basic, "mac", "basic.mac")
    _validate_chart_contract(stock_chart, "stock_chart")
    _validate_chart_contract(proxy_chart, "proxy_chart")

    if len(strength) < 5:
        raise ValidationError("snapshot contract failed: strength")
    for index in (0, 4):
        row = strength[index]
        if not isinstance(row, Mapping):
            raise ValidationError("snapshot contract failed: strength")
        _require_positive(
            row,
            "cntr_str",
            f"strength.row{index}.cntr_str",
        )

    sell_total = _require_nonnegative(
        order_book,
        "tot_sel_req",
        "orderbook.tot_sel_req",
    )
    buy_total = _require_nonnegative(
        order_book,
        "tot_buy_req",
        "orderbook.tot_buy_req",
    )
    if sell_total <= 0.0 and buy_total <= 0.0:
        raise ValidationError("snapshot contract failed: orderbook.totals")


def _finite_forces(value: Mapping[str, Any]) -> dict[str, float]:
    if set(value) != EXPECTED_FORCE_KEYS:
        raise ValidationError("force key contract mismatch")
    result: dict[str, float] = {}
    for key in sorted(EXPECTED_FORCE_KEYS):
        item = value[key]
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValidationError("force value is not numeric")
        normalized = float(item)
        if not math.isfinite(normalized):
            raise ValidationError("force value is not finite")
        result[key] = normalized
    return result


def _allowlisted_strategy_verdict(
    raw_verdict: Mapping[str, Any],
    regime: MarketRegime,
) -> dict[str, Any]:
    if regime is MarketRegime.UNKNOWN:
        raise ValidationError("strategy verdict regime is unknown")
    status = raw_verdict.get("status")
    is_buy_signal = raw_verdict.get("is_buy_signal")
    raw_regime = raw_verdict.get("regime")
    if not isinstance(status, str) or not status:
        raise ValidationError("strategy verdict status contract failed")
    if not isinstance(is_buy_signal, bool):
        raise ValidationError("strategy verdict signal contract failed")
    if (
        not isinstance(raw_regime, str)
        or raw_regime not in {regime.name, regime.value}
    ):
        raise ValidationError("strategy verdict regime contract failed")
    return {
        "status": status,
        "is_buy_signal": is_buy_signal,
        "regime": regime.name,
    }


def run_with_client(
    client: MarketOnlyClient,
    *,
    stock_code: str,
    proxy_code: str,
) -> dict[str, Any]:
    repository = MemoryStateRepository()
    try:
        client.ensure_auth_ready()
        snapshot = _fetch_snapshot(
            client,
            stock_code=stock_code,
            proxy_code=proxy_code,
        )
        (
            basic,
            stock_chart,
            proxy_chart,
            strength,
            order_book,
            sequence,
        ) = snapshot
        if sequence != EXPECTED_LOGICAL_SEQUENCE:
            raise ValidationError("logical API sequence mismatch")
        _validate_snapshot_contract(
            basic=basic,
            stock_chart=stock_chart,
            proxy_chart=proxy_chart,
            strength=strength,
            order_book=order_book,
        )
        gateway = CachedMarketGateway(
            stock_code=stock_code,
            proxy_code=proxy_code,
            basic=basic,
            stock_chart=stock_chart,
            proxy_chart=proxy_chart,
            strength=strength,
            order_book=order_book,
        )
        tracker = PhysicalStateTracker(
            repository,
            clock=lambda: datetime.now(timezone.utc),
        )
        analyzer = MarketAnalyzer(
            gateway,
            {"proxy_code": proxy_code},
            tracker,
        )
        with _safe_dependency_logging():
            analyzer.update_regime()
            analyzer.update_priority_supply([stock_code])
        if analyzer.market_regime is MarketRegime.UNKNOWN:
            raise ValidationError("market regime remained UNKNOWN")
        metrics = analyzer.supply_cache.get(stock_code)
        if metrics is None:
            raise ValidationError("required stock metrics were not produced")
        metric_dto = {
            "current_price": _positive_finite(
                metrics.cur_prc,
                "current_price",
            ),
            "vwap": _positive_finite(metrics.vwap, "vwap"),
            "strength": _positive_finite(metrics.strength, "strength"),
            "trend_rsi": _positive_finite(metrics.trend_rsi, "trend_rsi"),
            "atr_percent": _positive_finite(
                metrics.atr_percent,
                "atr_percent",
            ),
            "down_atr_percent": _positive_finite(
                metrics.down_atr_percent,
                "down_atr_percent",
            ),
            "volume_ratio": _positive_finite(
                metrics.vol_ratio,
                "volume_ratio",
            ),
        }
        forces = _finite_forces(metrics.forces)
        if repository.submissions != [stock_code]:
            raise ValidationError("physical state submission contract failed")
        strategy = TradingStrategy(
            {
                "debug_mode": False,
                "regimes": {"default": {}},
            },
            clock=lambda: datetime(
                2026,
                7,
                24,
                10,
                0,
                tzinfo=timezone.utc,
            ),
        )
        strategy.update_context(analyzer.market_regime)
        verdict = _allowlisted_strategy_verdict(
            strategy.evaluate(metrics),
            analyzer.market_regime,
        )
        counts = client._session.safe_counts()
        if set(counts) != {
            "token",
            "stock_basic",
            "stock_chart_5m",
            "proxy_chart_60m",
            "stock_strength",
            "stock_orderbook",
        }:
            raise ValidationError("API count allowlist mismatch")
        if any(counts[name] < 1 for name in counts):
            raise ValidationError("required API was not attempted")
        if client._session.attempt_count > MAX_HTTP_ATTEMPTS:
            raise ValidationError("HTTP attempt budget exceeded")
        return {
            "status": "PASS",
            "mode": "prod-read-only",
            "confirmation": "explicit",
            "stock_code": stock_code,
            "proxy_code": proxy_code,
            "stock_chart_minutes": 5,
            "proxy_chart_minutes": 60,
            "market_regime": analyzer.market_regime.name,
            "verdict": verdict,
            "metrics": metric_dto,
            "forces": forces,
            "api_counts": counts,
            "http_attempts": client._session.attempt_count,
            "logical_api_sequence": list(sequence),
            "state_submissions": 1,
            "side_effects": {
                "orders": False,
                "account": False,
                "revoke": False,
                "database": False,
                "reports": False,
                "notifications": False,
            },
        }
    finally:
        repository.close()
        client.close()


def _stock_code(value: str) -> str:
    if len(value) != 6 or not value.isascii() or not value.isdigit():
        raise argparse.ArgumentTypeError("stock code must be six ASCII digits")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Strict market-only Kiwoom production read validation."
    )
    parser.add_argument("--credentials-dir", required=True, type=Path)
    parser.add_argument("--stock-code", default="005930", type=_stock_code)
    parser.add_argument(
        "--confirm-prod-read-only",
        action="store_true",
        help="confirm production OAuth and five allowlisted market reads",
    )
    return parser


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if not args.confirm_prod_read_only:
        raise ValidationError(
            "explicit prod-read-only confirmation is required"
        )
    provider = StrictFileCredentialProvider(
        args.credentials_dir,
        repository_root=credential_repository_boundary(),
    )
    credentials = provider.load()
    session = AllowlistedReadOnlySession(
        stock_code=args.stock_code,
        proxy_code=REGIME_PROXY_CODE,
    )
    client = MarketOnlyClient(credentials, session=session)
    return run_with_client(
        client,
        stock_code=args.stock_code,
        proxy_code=REGIME_PROXY_CODE,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "BLOCKED",
                    "error": _safe_error(error),
                    "side_effects": "not_started_or_read_only",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0
