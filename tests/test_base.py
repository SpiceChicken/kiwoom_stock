from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import threading
from unittest.mock import Mock

import pytest
import requests

from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.auth import Authenticator, AuthorizationSnapshot
from kiwoom_stock.api.exceptions import KiwoomAPIError, KiwoomAPIResponseError
from kiwoom_stock.application.credentials import (
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.settings import KiwoomEndpoint


class Response:
    def __init__(self, status_code=200, payload=None, *, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def client(response, *, sleeper=None):
    auth = Mock()
    auth.authorization_snapshot.return_value = AuthorizationSnapshot(
        "Bearer synthetic-token",
        1,
    )
    auth.refresh_after_401.return_value = AuthorizationSnapshot(
        "Bearer refreshed-token",
        2,
    )
    session = Mock(spec=requests.Session)
    session.trust_env = True
    session.post.return_value = response
    sleep = sleeper or Mock()
    return (
        BaseClient(
            auth,
            KiwoomEndpoint.MOCK,
            session=session,
            sleeper=sleep,
        ),
        auth,
        session,
        sleep,
    )


def test_success_uses_typed_origin_hardened_session_and_no_redirects():
    transport, auth, session, _ = client(Response(payload={"return_code": 0, "value": 7}))

    assert transport.request(
        "/api/test",
        "ka99999",
        {"stk_cd": "005930"},
        read_only=True,
    ) == {"return_code": 0, "value": 7}

    assert session.trust_env is False
    auth.authorization_snapshot.assert_called_once_with()
    session.post.assert_called_once_with(
        "https://mockapi.kiwoom.com/api/test",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "ka99999",
            "authorization": "Bearer synthetic-token",
        },
        json={"stk_cd": "005930"},
        timeout=(5, 30),
        allow_redirects=False,
        verify=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        "https://attacker.invalid/api",
        "//attacker.invalid/api",
        "api/test",
        "/api/test#fragment",
        "/api/test?redirect=https://attacker.invalid",
    ],
)
def test_endpoint_cannot_override_typed_origin(path):
    transport, _, session, _ = client(Response(payload={"return_code": 0}))

    with pytest.raises(ValueError):
        transport.request(path, "ka99999", {})

    session.post.assert_not_called()


def test_response_error_has_safe_fields_and_no_raw_response_data():
    raw = {"return_code": -100, "return_message": "secret reflected", "detail": "raw"}
    transport, _, session, _ = client(Response(payload=raw))

    with pytest.raises(KiwoomAPIResponseError) as caught:
        transport.request("/test", "api_id", {})

    assert caught.value.return_code == -100
    assert caught.value.category == "api_rejected"
    assert not hasattr(caught.value, "response_data")
    assert "secret reflected" not in str(caught.value)
    assert session.post.call_count == 1


def test_invalid_json_and_http_failure_are_typed_without_raw_content():
    transport, _, session, _ = client(
        Response(json_error=ValueError("raw response secret"))
    )
    with pytest.raises(KiwoomAPIError) as invalid:
        transport.request("/test", "api_id", {})
    assert invalid.value.category == "invalid_json"
    assert "raw response secret" not in str(invalid.value)

    session.reset_mock()
    session.post.return_value = Response(status_code=503)
    with pytest.raises(KiwoomAPIError) as http:
        transport.request("/test", "api_id", {})
    assert http.value.status_code == 503
    assert http.value.category == "http_status"


def test_read_only_timeout_retries_but_unknown_or_order_semantics_never_replay():
    sleeper = Mock()
    transport, _, session, _ = client(Response(), sleeper=sleeper)
    session.post.side_effect = requests.ReadTimeout("raw request secret")

    with pytest.raises(KiwoomAPIError, match="timed out"):
        transport.request("/read", "ka99999", {}, read_only=True)
    assert session.post.call_count == 3
    assert [call.args[0] for call in sleeper.call_args_list] == [2.0, 4.0]

    session.post.reset_mock()
    sleeper.reset_mock()
    with pytest.raises(KiwoomAPIError, match="timed out"):
        transport.request("/order", "kt10000", {}, read_only=False)
    assert session.post.call_count == 1
    sleeper.assert_not_called()


def test_read_only_5xx_is_bounded_and_non_read_only_5xx_is_not_replayed():
    transport, _, session, sleeper = client(Response(status_code=503))

    with pytest.raises(KiwoomAPIError):
        transport.request("/read", "ka99999", {}, read_only=True)
    assert session.post.call_count == 3
    assert sleeper.call_count == 2

    session.post.reset_mock()
    sleeper.reset_mock()
    with pytest.raises(KiwoomAPIError):
        transport.request("/order", "kt10000", {}, read_only=False)
    assert session.post.call_count == 1
    sleeper.assert_not_called()


def test_401_refresh_replays_exactly_once_only_for_explicit_read_only():
    transport, auth, session, _ = client(Response())
    auth.authorization_snapshot.return_value = AuthorizationSnapshot(
        "Bearer old-token",
        1,
    )
    auth.refresh_after_401.return_value = AuthorizationSnapshot(
        "Bearer refreshed-token",
        2,
    )
    session.post.side_effect = [
        Response(status_code=401),
        Response(payload={"return_code": 0}),
    ]

    assert transport.request(
        "/read", "ka99999", {}, read_only=True
    ) == {"return_code": 0}
    assert session.post.call_count == 2
    auth.refresh_after_401.assert_called_once_with(1)
    assert session.post.call_args_list[1].kwargs["headers"]["authorization"] == (
        "Bearer refreshed-token"
    )

    transport, auth, session, _ = client(Response(status_code=401))
    with pytest.raises(KiwoomAPIError) as caught:
        transport.request("/order", "kt10000", {}, read_only=False)
    assert caught.value.status_code == 401
    assert session.post.call_count == 1
    auth.refresh_after_401.assert_not_called()


def test_second_401_is_not_replayed_and_none_authorization_never_posts():
    transport, auth, session, _ = client(Response(status_code=401))
    auth.authorization_snapshot.return_value = AuthorizationSnapshot(
        "Bearer old-token",
        1,
    )
    auth.refresh_after_401.return_value = AuthorizationSnapshot(
        "Bearer refreshed-token",
        2,
    )

    with pytest.raises(KiwoomAPIError) as caught:
        transport.request("/read", "ka99999", {}, read_only=True)
    assert caught.value.status_code == 401
    assert session.post.call_count == 2
    auth.refresh_after_401.assert_called_once_with(1)

    transport, auth, session, _ = client(Response(payload={"return_code": 0}))
    auth.authorization_snapshot.return_value = AuthorizationSnapshot(
        "Bearer None",
        1,
    )
    with pytest.raises(KiwoomAPIError) as invalid:
        transport.request("/read", "ka99999", {}, read_only=True)
    assert invalid.value.category == "invalid_authorization"
    session.post.assert_not_called()


def test_concurrent_stale_401_responses_share_one_refresh_generation():
    callers = 20

    class ConcurrentSession:
        def __init__(self):
            self.trust_env = True
            self.proxies = {"ambient": "forbidden"}
            self.token_posts = 0
            self.api_posts = 0
            self.lock = threading.Lock()
            self.old_request_barrier = threading.Barrier(callers)

        def post(self, url, *, headers, **kwargs):
            if url.endswith("/oauth2/token"):
                with self.lock:
                    self.token_posts += 1
                    token = "old-token" if self.token_posts == 1 else "new-token"
                return Response(
                    payload={
                        "return_code": 0,
                        "token": token,
                        "token_type": "bearer",
                        "expires_dt": "20260717100000",
                    }
                )
            with self.lock:
                self.api_posts += 1
            if headers["authorization"] == "Bearer old-token":
                self.old_request_barrier.wait(timeout=5)
                return Response(status_code=401)
            return Response(payload={"return_code": 0})

        def close(self):
            return None

    session = ConcurrentSession()
    auth = Authenticator(
        KiwoomClientCredentials(
            SensitiveText("synthetic-app"),
            SensitiveText("synthetic-secret"),
        ),
        KiwoomEndpoint.MOCK,
        session=session,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        sleeper=Mock(),
    )
    auth.ensure_ready()
    transport = BaseClient(
        auth,
        KiwoomEndpoint.MOCK,
        session=session,  # type: ignore[arg-type]
        sleeper=Mock(),
    )

    with ThreadPoolExecutor(max_workers=callers) as executor:
        results = list(
            executor.map(
                lambda _: transport.request(
                    "/read",
                    "ka99999",
                    {},
                    read_only=True,
                ),
                range(callers),
            )
        )

    assert results == [{"return_code": 0}] * callers
    assert session.token_posts == 2
    assert session.api_posts == callers * 2


def test_standalone_base_client_closes_only_owned_session(monkeypatch):
    owned_session = Mock(spec=requests.Session)
    monkeypatch.setattr(requests, "Session", Mock(return_value=owned_session))
    auth = Mock()
    transport = BaseClient(auth, KiwoomEndpoint.MOCK)

    transport.close()
    transport.close()

    owned_session.close.assert_called_once_with()
