"""Characterize Kiwoom HTTP mappings and retry behavior with no real network."""

from datetime import date, datetime, timezone
import socket
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from kiwoom_stock.api.auth import (
    Authenticator,
    AuthorizationSnapshot,
    RateLimitExceededError,
)
from kiwoom_stock.application.credentials import (
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.exceptions import KiwoomAPIError, KiwoomAPIResponseError
from kiwoom_stock.api.parser import clean_numeric
from kiwoom_stock.api.services import market as market_service_module
from kiwoom_stock.api.services.account import AccountService
from kiwoom_stock.api.services.market import MarketService
from kiwoom_stock.settings import KiwoomEndpoint


class _Response:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_suite_network_guard_blocks_dns_and_socket_helpers():
    with pytest.raises(AssertionError, match="fake transport"):
        socket.getaddrinfo("example.invalid", 443)
    with pytest.raises(AssertionError, match="fake transport"):
        socket.create_connection(("example.invalid", 443))


def _authenticator(session=None, sleeper=lambda _delay: None):
    return Authenticator(
        KiwoomClientCredentials(
            SensitiveText("app-key"),
            SensitiveText("secret-key"),
        ),
        KiwoomEndpoint.MOCK,
        session=session,
        clock=lambda: datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        sleeper=sleeper,
    )


def test_oauth_endpoint_headers_payload_expiry_and_timeout():
    session = Mock(spec=requests.Session)
    session.post.return_value = _Response(
        200,
        {
            "return_code": 0,
            "token": "fresh-token",
            "token_type": "bearer",
            "expires_dt": "20260717100000",
        },
    )
    authenticator = _authenticator(session)

    assert authenticator.authorization_header() == "Bearer fresh-token"
    assert authenticator._lease is not None
    assert authenticator._lease.expires_at == datetime(
        2026, 7, 17, 1, 0, tzinfo=timezone.utc
    )
    session.post.assert_called_once_with(
        "https://mockapi.kiwoom.com/oauth2/token",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        },
        json={
            "grant_type": "client_credentials",
            "appkey": "app-key",
            "secretkey": "secret-key",
        },
        timeout=10,
        allow_redirects=False,
        verify=True,
    )


def test_oauth_retries_only_429_three_times_without_real_sleep():
    session = Mock(spec=requests.Session)
    session.post.return_value = _Response(429, text="rate limited")
    sleeper = Mock()
    authenticator = _authenticator(session, sleeper=sleeper)

    with pytest.raises(RateLimitExceededError, match="rate limit exceeded"):
        authenticator.get_token()

    assert session.post.call_count == 3
    assert sleeper.call_count == 2


def test_oauth_non_429_http_failure_is_typed_without_retry():
    session = Mock(spec=requests.Session)
    session.post.return_value = _Response(503, text="unavailable")
    authenticator = _authenticator(session)

    with pytest.raises(KiwoomAPIError) as caught:
        authenticator.get_token()
    assert caught.value.status_code == 503
    assert session.post.call_count == 1


def test_base_client_endpoint_headers_payload_timeout_and_success():
    session = Mock(spec=requests.Session)
    session.post.return_value = _Response(200, {"return_code": 0, "value": 7})
    auth = SimpleNamespace(
        authorization_snapshot=Mock(
            return_value=AuthorizationSnapshot("Bearer token-value", 1)
        ),
        refresh_after_401=Mock(),
    )
    client = BaseClient(auth, KiwoomEndpoint.MOCK, session=session)

    assert client.request(
        "/api/test", "ka99999", {"stk_cd": "005930"}, read_only=True
    ) == {
        "return_code": 0,
        "value": 7,
    }
    session.post.assert_called_once_with(
        "https://mockapi.kiwoom.com/api/test",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "ka99999",
            "authorization": "Bearer token-value",
        },
        json={"stk_cd": "005930"},
        timeout=(5, 30),
        allow_redirects=False,
        verify=True,
    )


def test_base_client_timeout_retries_three_times_with_two_and_four_second_waits():
    session = Mock(spec=requests.Session)
    session.post.side_effect = requests.exceptions.ReadTimeout("slow")
    sleep = Mock()
    client = BaseClient(
        SimpleNamespace(
            authorization_snapshot=lambda: AuthorizationSnapshot(
                "Bearer token",
                1,
            ),
            refresh_after_401=Mock(),
        ),
        KiwoomEndpoint.MOCK,
        session=session,
        sleeper=sleep,
    )

    with pytest.raises(KiwoomAPIError, match="timed out"):
        client.request("/api/test", "ka99999", {}, max_retries=3, read_only=True)

    assert session.post.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [2, 4]


def test_base_client_http_and_return_code_errors_are_sanitized():
    session = Mock(spec=requests.Session)
    session.post.return_value = _Response(503, {"return_code": 0})
    client = BaseClient(
        SimpleNamespace(
            authorization_snapshot=lambda: AuthorizationSnapshot(
                "Bearer token",
                1,
            ),
            refresh_after_401=Mock(),
        ),
        KiwoomEndpoint.MOCK,
        session=session,
    )

    with pytest.raises(KiwoomAPIError) as http_error:
        client.request("/api/test", "ka99999", {})
    assert http_error.value.status_code == 503
    assert session.post.call_count == 1

    response_payload = {"return_code": -100, "return_message": "invalid", "detail": "raw"}
    session.post.reset_mock()
    session.post.return_value = _Response(200, response_payload)
    with pytest.raises(KiwoomAPIResponseError) as api_error:
        client.request("/api/test", "ka99999", {})
    assert api_error.value.return_code == -100
    assert not hasattr(api_error.value, "response_data")
    assert "raw" not in str(api_error.value)
    assert session.post.call_count == 1


class _RecordingBase:
    def __init__(self):
        self.calls = []

    def request(self, endpoint, api_id, payload, *, read_only=False):
        self.calls.append((endpoint, api_id, payload, read_only))
        return {
            "return_code": 0,
            "trde_prica_upper": [{"stk_cd": "A"}],
            "stk_min_pole_chart_qry": [{"cur_prc": "1"}],
            "cntr_str_tm": [{"cntr_str": "100"}],
            "stk_prm_trde_prst": [{"stk_cd": "A"}],
            "frgn_wicket_trde_upper": [{"stk_cd": "A"}],
            "cntr_infr": {"cur_prc": "1"},
        }


def test_market_and_account_service_endpoint_api_id_and_payload_mappings(monkeypatch):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 7, 17)

    monkeypatch.setattr(market_service_module.datetime, "date", FrozenDate)
    base = _RecordingBase()
    account = AccountService(base)
    market = MarketService(base)

    account.get_portfolio()
    market.get_top_trading_value()
    market.get_stock_basic_info("005930")
    market.get_minute_chart("005930", "5")
    market.get_tick_strength("005930")
    market.get_program_trade()
    market.get_foreign_window_total()
    market.get_recent_ticks("005930")
    market.get_order_book("005930")

    assert base.calls == [
        ("/api/dostk/acnt", "kt00004", {"qry_tp": "0", "dmst_stex_tp": "KRX"}, True),
        (
            "/api/dostk/rkinfo",
            "ka10032",
            {"mrkt_tp": "001", "mang_stk_incls": "0", "stex_tp": "1"},
            True,
        ),
        ("/api/dostk/stkinfo", "ka10001", {"stk_cd": "005930"}, True),
        (
            "/api/dostk/chart",
            "ka10080",
            {"stk_cd": "005930", "tic_scope": "5", "upd_stkpc_tp": "1"},
            True,
        ),
        ("/api/dostk/mrkcond", "ka10046", {"stk_cd": "005930"}, True),
        (
            "/api/dostk/stkinfo",
            "ka90004",
            {"dt": "20260717", "mrkt_tp": "P00101", "stex_tp": "1"},
            True,
        ),
        (
            "/api/dostk/rkinfo",
            "ka10037",
            {"mrkt_tp": "001", "dt": "0", "trde_tp": "1", "sort_tp": "1", "stex_tp": "1"},
            True,
        ),
        ("/api/dostk/stkinfo", "ka10003", {"stk_cd": "005930"}, True),
        ("/api/dostk/mrkcond", "ka10004", {"stk_cd": "005930"}, True),
    ]


def test_numeric_parser_preserves_current_absolute_sign_convention():
    assert clean_numeric("-1,234.5") == 1234.5
    assert clean_numeric("+1,234.5") == 1234.5
