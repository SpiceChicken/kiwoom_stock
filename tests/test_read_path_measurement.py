import time

import pytest
import requests

from kiwoom_stock.infrastructure.kiwoom_market_only import (
    AllowlistedReadOnlySession,
    ReadOnlyBoundaryError,
)


def _response(status_code=200):
    response = requests.Response()
    response.status_code = status_code
    return response


def _market_request(session, path, api_id, payload):
    return session.request(
        "POST",
        f"https://api.kiwoom.com{path}",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": "Bearer measurement-token",
        },
        json=payload,
        data=None,
        timeout=(5, 30),
        allow_redirects=False,
        verify=True,
    )


def test_market_read_path_measurement_captures_counts_and_latency():
    calls = []

    def sender(method, url, **_kwargs):
        started = time.perf_counter()
        time.sleep(0.001)
        calls.append((method, url, time.perf_counter() - started))
        return _response()

    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        max_attempts=5,
        sender=sender,
    )
    try:
        _market_request(session, "/api/dostk/stkinfo", "ka10001", {"stk_cd": "005930"})
        _market_request(
            session,
            "/api/dostk/chart",
            "ka10080",
            {"stk_cd": "005930", "tic_scope": "5", "upd_stkpc_tp": "1"},
        )
        _market_request(
            session,
            "/api/dostk/chart",
            "ka10080",
            {"stk_cd": "069500", "tic_scope": "60", "upd_stkpc_tp": "1"},
        )
        _market_request(session, "/api/dostk/mrkcond", "ka10046", {"stk_cd": "005930"})
        _market_request(session, "/api/dostk/mrkcond", "ka10004", {"stk_cd": "005930"})

        assert session.attempt_count == 5
        assert session.safe_counts() == {
            "token": 0,
            "stock_basic": 1,
            "stock_chart_5m": 1,
            "proxy_chart_60m": 1,
            "stock_strength": 1,
            "stock_orderbook": 1,
        }
        assert len(calls) == 5
        assert all(latency > 0 for _, _, latency in calls)
    finally:
        session.close()


def test_market_read_path_measurement_terminates_on_429_without_retry():
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        max_attempts=23,
        sender=lambda *_args, **_kwargs: _response(429),
        terminate_on_rate_limit=True,
    )
    try:
        with pytest.raises(ReadOnlyBoundaryError, match="rate limit"):
            _market_request(session, "/api/dostk/stkinfo", "ka10001", {"stk_cd": "005930"})
        assert session.attempt_count == 1
        assert session.safe_counts()["stock_basic"] == 1
    finally:
        session.close()
