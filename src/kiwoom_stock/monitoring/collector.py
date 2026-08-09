"""Typed market-data collection boundary."""

from datetime import datetime
import math
import re
from typing import Any, Callable, Dict, List, Mapping, NoReturn, Sequence, TypeVar
from zoneinfo import ZoneInfo

import requests

from kiwoom_stock.application.ports import (
    MarketDataCollectionError,
    MarketDataFailureKind,
    MarketDataGateway,
)
from kiwoom_stock.domain.indicators import (
    INDICATOR_PERIOD,
    MIN_INDICATOR_ROWS,
)


_STRICT_NUMERIC = re.compile(
    r"^[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$"
)
_TEMPORAL_FIELDS = frozenset({"cntr_tm", "dt", "date", "time", "체결시간"})
_CHART_TIMEZONE = ZoneInfo("Asia/Seoul")
_T = TypeVar("_T")


def _raise_collection_error(
    kind: MarketDataFailureKind,
    operation: str,
    cause: Exception | None = None,
) -> NoReturn:
    error = MarketDataCollectionError(kind, operation)
    if cause is None:
        raise error
    raise error from cause


def _fetch(operation: str, call: Callable[[], _T]) -> _T:
    try:
        return call()
    except MarketDataCollectionError:
        raise
    except (TimeoutError, requests.Timeout) as error:
        _raise_collection_error(MarketDataFailureKind.TIMEOUT, operation, error)
    except Exception as error:
        _raise_collection_error(MarketDataFailureKind.FETCH, operation, error)


def _parse_numeric(value: object, operation: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _raise_collection_error(MarketDataFailureKind.PARSE, operation)
    if isinstance(value, str):
        if _STRICT_NUMERIC.fullmatch(value) is None:
            _raise_collection_error(MarketDataFailureKind.PARSE, operation)
        value = value.replace(",", "")
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        _raise_collection_error(MarketDataFailureKind.PARSE, operation, error)
    if not math.isfinite(numeric):
        _raise_collection_error(MarketDataFailureKind.PARSE, operation)
    return numeric


def _require_mapping(value: object, operation: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    return value


def _normalize_record(
    record: Mapping[str, Any],
    operation: str,
    required_numeric: Sequence[str],
) -> Dict[str, Any]:
    missing = [key for key in required_numeric if key not in record]
    if missing:
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    normalized: Dict[str, Any] = {}
    for key, value in record.items():
        if key in _TEMPORAL_FIELDS:
            if not isinstance(value, str):
                _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
            normalized[key] = value
        elif key in required_numeric:
            normalized[key] = _parse_numeric(value, operation)
        elif isinstance(value, (int, float)) or (
            isinstance(value, str) and _STRICT_NUMERIC.fullmatch(value) is not None
        ):
            normalized[key] = _parse_numeric(value, operation)
    return normalized


def _parse_chart_timestamp(value: str, operation: str) -> datetime:
    if (
        len(value) != 14
        or not value.isascii()
        or not value.isdigit()
    ):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    try:
        parsed = datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError as error:
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation, error)
    if parsed.strftime("%Y%m%d%H%M%S") != value:
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    return parsed.replace(tzinfo=_CHART_TIMEZONE)


def _normalize_chart_oldest_first(
    rows: List[Dict[str, Any]],
    operation: str,
) -> List[Dict[str, Any]]:
    temporal_keys = [
        tuple(key for key in row if key in _TEMPORAL_FIELDS)
        for row in rows
    ]
    if all(not keys for keys in temporal_keys):
        rows.reverse()
        return rows
    if any(len(keys) != 1 for keys in temporal_keys):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    time_field = temporal_keys[0][0]
    if any(keys[0] != time_field for keys in temporal_keys):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    parsed_rows = [
        (_parse_chart_timestamp(row[time_field], operation), row)
        for row in rows
    ]
    timestamps = [parsed for parsed, _ in parsed_rows]
    if len(timestamps) != len(set(timestamps)):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    return [row for _, row in sorted(parsed_rows, key=lambda item: item[0])]


def _normalize_rows(
    value: object,
    operation: str,
    required_numeric: Sequence[str],
) -> List[Dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
    normalized = []
    for item in value:
        record = _require_mapping(item, operation)
        if not record:
            _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
        normalized.append(_normalize_record(record, operation, required_numeric))
    return normalized


class MarketDataCollector:
    """Collect market data while preserving empty and typed failure semantics."""

    def __init__(self, market_gateway: MarketDataGateway):
        self.market_gateway = market_gateway

    def fetch_stock_basic(self, code: str) -> Dict[str, Any]:
        operation = "stock_basic"
        item = _fetch(
            operation,
            lambda: self.market_gateway.get_stock_basic_info(code),
        )
        record = _require_mapping(item, operation)
        if not record:
            return {}
        return _normalize_record(
            record,
            operation,
            ("trde_pre", "trde_qty", "cur_prc", "mac"),
        )

    def fetch_tick_strength(self, code: str) -> List[Dict[str, Any]]:
        operation = "tick_strength"
        items = _fetch(
            operation,
            lambda: self.market_gateway.get_tick_strength(code),
        )
        return _normalize_rows(items, operation, ("cntr_str",))

    def fetch_minute_chart(
        self,
        code: str,
        tic: str = "1",
    ) -> List[Dict[str, Any]]:
        operation = f"minute_chart_{tic}m"
        items = _fetch(
            operation,
            lambda: self.market_gateway.get_minute_chart(code, tic=tic),
        )
        rows = _normalize_rows(
            items,
            operation,
            ("cur_prc", "open_pric", "high_pric", "low_pric", "trde_qty"),
        )
        if any(row["trde_qty"] < 0.0 for row in rows):
            _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
        return _normalize_chart_oldest_first(rows, operation)

    def fetch_indicator_chart(
        self,
        code: str,
        tic: str,
    ) -> List[Dict[str, Any]]:
        """Return the normalized minimum series required by shared-period indicators."""

        operation = f"minute_chart_{tic}m"
        rows = self.fetch_minute_chart(code, tic)
        if not rows:
            _raise_collection_error(MarketDataFailureKind.EMPTY, operation)
        if len(rows) < MIN_INDICATOR_ROWS:
            _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
        return rows

    def fetch_program_trade(self) -> Dict[str, Dict[str, Any]]:
        operation = "program_trade"
        items = _fetch(operation, self.market_gateway.get_program_trade)
        rows = _normalize_rows(items, operation, ())
        result: Dict[str, Dict[str, Any]] = {}
        for raw, normalized in zip(items, rows):
            record = _require_mapping(raw, operation)
            stock_code = record.get("stk_cd")
            if not isinstance(stock_code, str) or not stock_code:
                _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
            result[stock_code] = normalized
        return result

    def fetch_foreign_window_trade(self) -> Dict[str, Dict[str, Any]]:
        operation = "foreign_window_trade"
        items = _fetch(operation, self.market_gateway.get_foreign_window_total)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
            _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
        result: Dict[str, Dict[str, Any]] = {}
        for item in items:
            record = _require_mapping(item, operation)
            stock_code = record.get("stk_cd")
            if not isinstance(stock_code, str) or not stock_code:
                _raise_collection_error(MarketDataFailureKind.MALFORMED, operation)
            result[stock_code] = _normalize_record(
                record,
                operation,
                ("netprps_prica", "trde_prica"),
            )
        return result

    def fetch_order_book(self, code: str) -> Dict[str, Any]:
        operation = "order_book"
        item = _fetch(operation, lambda: self.market_gateway.get_order_book(code))
        record = _require_mapping(item, operation)
        if not record:
            return {}
        return _normalize_record(
            record,
            operation,
            ("tot_sel_req", "tot_buy_req"),
        )

    def fetch_recent_ticks(self, code: str) -> List[Dict[str, Any]]:
        operation = "recent_ticks"
        items = _fetch(operation, lambda: self.market_gateway.get_recent_ticks(code))
        return _normalize_rows(items, operation, ())
