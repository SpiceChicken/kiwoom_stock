import requests

from kiwoom_stock.api.http_headers import KIWOOM_USER_AGENT, configure_session
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    AllowlistedReadOnlySession,
)


def test_configure_session_applies_fixed_kiwoom_user_agent_and_network_defaults():
    session = configure_session(requests.Session())

    assert session.headers["User-Agent"] == KIWOOM_USER_AGENT
    assert session.trust_env is False
    assert session.proxies == {}


def test_market_only_transport_uses_fixed_kiwoom_user_agent():
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
    )

    try:
        assert session._transport.headers["User-Agent"] == KIWOOM_USER_AGENT
    finally:
        session.close()
