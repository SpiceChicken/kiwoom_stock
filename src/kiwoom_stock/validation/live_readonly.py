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
from typing import Any, Iterator, Mapping, Sequence
from kiwoom_stock.api.exceptions import KiwoomAPIError
from kiwoom_stock.application.credentials import CredentialProviderError
from kiwoom_stock.application.ports import MarketDataCollectionError
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.models import MarketRegime
from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
    PhysicalStateWrite,
    PhysicalTrackerState,
)
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    APP_KEY_FILE,
    MATERIALIZED_APP_KEY_FILE,
    MATERIALIZED_SECRET_KEY_FILE,
    SECRET_KEY_FILE,
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    MAX_HTTP_ATTEMPTS,
    AllowlistedReadOnlySession,
    CachedMarketGateway,
    MarketSnapshot,
    MarketOnlyClient,
    ReadOnlyBoundaryError as _ReadOnlyBoundaryError,
    ValidationError,
    fetch_market_snapshot,
    validate_market_snapshot,
)
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.monitoring.strategy import TradingStrategy


ReadOnlyBoundaryError = _ReadOnlyBoundaryError


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


@dataclass
class MemoryStateRepository:
    latest: dict[str, PhysicalTrackerState] = field(default_factory=dict)
    submissions: list[str] = field(default_factory=list)

    def load_physical_state(self, stock_code: str) -> PhysicalStateLoadResult:
        state = self.latest.get(stock_code)
        return PhysicalStateLoadResult(
            (
                PhysicalStateHydrationSource.PERSISTED
                if state is not None
                else PhysicalStateHydrationSource.INITIAL
            ),
            state,
        )

    def persist_physical_state(
        self,
        state: PhysicalTrackerState,
        forces: Mapping[str, Any],
    ) -> PhysicalStateCommitReceipt:
        write = PhysicalStateWrite(state, tuple(dict(forces).items()))
        return self.persist_physical_state_batch((write,)).items[0]

    def persist_physical_state_batch(
        self,
        writes: Sequence[PhysicalStateWrite],
    ) -> PhysicalStateBatchCommitReceipt:
        immutable_writes = tuple(writes)
        if not immutable_writes:
            raise ValueError("physical-state batch is empty")
        generation = immutable_writes[0].state.last_observed_at
        assert generation is not None
        committed_at = immutable_writes[0].state.updated_at
        receipts = tuple(
            PhysicalStateCommitReceipt(
                write.state.stock_code,
                generation.isoformat(),
                committed_at,
            )
            for write in immutable_writes
        )
        next_latest = dict(self.latest)
        for write in immutable_writes:
            next_latest[write.state.stock_code] = write.state
        self.latest = next_latest
        self.submissions.extend(write.state.stock_code for write in immutable_writes)
        return PhysicalStateBatchCommitReceipt(
            generation.isoformat(),
            receipts,
            committed_at,
        )

    def close(self) -> None:
        self.latest.clear()


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
    try:
        snapshot = fetch_market_snapshot(
            client, stock_code=stock_code, proxy_code=proxy_code
        )
    except MarketDataCollectionError as error:
        if isinstance(error.__cause__, ReadOnlyBoundaryError):
            raise ValidationError(str(error.__cause__)) from error
        raise ValidationError(
            f"market snapshot collection failed ({error.kind.value})"
        ) from error
    return (
        snapshot.basic,
        snapshot.stock_chart,
        snapshot.proxy_chart,
        snapshot.strength,
        snapshot.order_book,
        EXPECTED_LOGICAL_SEQUENCE,
    )


def _positive_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} is not numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValidationError(f"{name} must be positive and finite")
    return normalized


def _nonnegative_finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} is not numeric")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0:
        raise ValidationError(f"{name} must be nonnegative and finite")
    return normalized


def _validate_snapshot_contract(
    *,
    basic: Mapping[str, Any],
    stock_chart: Sequence[Mapping[str, Any]],
    proxy_chart: Sequence[Mapping[str, Any]],
    strength: Sequence[Mapping[str, Any]],
    order_book: Mapping[str, Any],
) -> None:
    validate_market_snapshot(
        MarketSnapshot(basic, stock_chart, proxy_chart, strength, order_book)
    )


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
            stock_code,
            proxy_code,
            MarketSnapshot(basic, stock_chart, proxy_chart, strength, order_book),
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
            "trend_rsi": _nonnegative_finite(metrics.trend_rsi, "trend_rsi"),
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
    file_names = _credential_file_names(args.credentials_dir)
    provider = StrictFileCredentialProvider(
        args.credentials_dir,
        repository_root=credential_repository_boundary(),
        file_names=file_names,
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


def _credential_file_names(credentials_dir: Path) -> tuple[str, str]:
    """Accept one complete approved layout, never a mixed pair."""

    canonical = (
        credentials_dir / APP_KEY_FILE,
        credentials_dir / SECRET_KEY_FILE,
    )
    materialized = (
        credentials_dir / MATERIALIZED_APP_KEY_FILE,
        credentials_dir / MATERIALIZED_SECRET_KEY_FILE,
    )
    canonical_present = tuple(path.is_file() for path in canonical)
    materialized_present = tuple(path.is_file() for path in materialized)
    if canonical_present == (True, True) and materialized_present == (
        False,
        False,
    ):
        return (APP_KEY_FILE, SECRET_KEY_FILE)
    if materialized_present == (True, True) and canonical_present == (
        False,
        False,
    ):
        return (MATERIALIZED_APP_KEY_FILE, MATERIALIZED_SECRET_KEY_FILE)
    raise CredentialProviderError(
        "credential directory has no single approved file pair"
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
