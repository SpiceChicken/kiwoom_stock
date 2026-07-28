from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
from unittest.mock import Mock

import pytest
import requests

from kiwoom_stock.api.auth import (
    Authenticator,
    AuthenticatorClosedError,
    RateLimitExceededError,
    RevokeResult,
)
from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.api.exceptions import KiwoomAuthError
from kiwoom_stock.application.credentials import (
    BearerToken,
    CredentialProviderError,
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.settings import KiwoomEndpoint


class Response:
    def __init__(self, status_code=200, payload=None, *, json_error=None, text=""):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error
        self.text = text

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


def credentials():
    return KiwoomClientCredentials(
        SensitiveText("synthetic-appkey"),
        SensitiveText("synthetic-secret"),
    )


def token_payload(*, token="synthetic-token", expires_dt="20260717100000"):
    return {
        "return_code": 0,
        "token": token,
        "token_type": "bearer",
        "expires_dt": expires_dt,
    }


def authenticator(response, *, now=None, sleeper=None):
    session = Mock(spec=requests.Session)
    session.trust_env = True
    session.post.return_value = response
    clock = Mock(return_value=now or datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc))
    sleep = sleeper or Mock()
    auth = Authenticator(
        credentials(),
        KiwoomEndpoint.MOCK,
        session=session,
        clock=clock,
        sleeper=sleep,
    )
    return auth, session, clock, sleep


def test_token_issue_uses_exact_origin_hardened_session_and_aware_kst_expiry():
    auth, session, _, _ = authenticator(Response(payload=token_payload()))

    token = auth.get_token()

    assert isinstance(token, BearerToken)
    assert str(token) == "[REDACTED]"
    assert repr(token) == "BearerToken([REDACTED])"
    assert auth.authorization_header() == "Bearer synthetic-token"
    assert session.trust_env is False
    session.post.assert_called_once_with(
        "https://mockapi.kiwoom.com/oauth2/token",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        },
        json={
            "grant_type": "client_credentials",
            "appkey": "synthetic-appkey",
            "secretkey": "synthetic-secret",
        },
        timeout=10,
        allow_redirects=False,
        verify=True,
    )
    assert auth._lease is not None
    assert auth._lease.expires_at == datetime(
        2026, 7, 17, 1, 0, tzinfo=timezone.utc
    )
    assert auth._lease.usable_until == datetime(
        2026, 7, 17, 0, 55, tzinfo=timezone.utc
    )


def test_refresh_skew_is_ten_percent_for_short_ttl_and_never_uses_expires_in():
    now_values = [
        datetime(2026, 7, 17, 0, 50, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 0, 50, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 0, 59, tzinfo=timezone.utc),
        datetime(2026, 7, 17, 0, 59, tzinfo=timezone.utc),
    ]
    session = Mock(spec=requests.Session)
    session.post.side_effect = [
        Response(payload={**token_payload(token="first"), "expires_in": 999999}),
        Response(payload=token_payload(token="second", expires_dt="20260717110000")),
    ]
    auth = Authenticator(
        credentials(),
        KiwoomEndpoint.MOCK,
        session=session,
        clock=lambda: now_values.pop(0),
        sleeper=Mock(),
    )

    first = auth.get_token()
    second = auth.get_token()

    assert first.reveal_for_authorization() == "first"
    assert second.reveal_for_authorization() == "second"
    assert session.post.call_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"return_code": 1, "token": "x", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": " x", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x\n", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x,y", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x;y", "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": 'x"y', "token_type": "bearer", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x", "token_type": "basic", "expires_dt": "20260717100000"},
        {"return_code": 0, "token": "x", "token_type": "bearer"},
        {"return_code": 0, "token": "x", "token_type": "bearer", "expires_dt": "2026-07-17"},
        {"return_code": 0, "token": "x", "token_type": "bearer", "expires_dt": "2026717100000"},
        {"return_code": 0, "token": "x", "token_type": "bearer", "expires_dt": "20260717090000"},
    ],
)
def test_token_contract_failures_are_closed(payload):
    auth, session, _, _ = authenticator(Response(payload=payload))

    with pytest.raises(KiwoomAuthError):
        auth.get_token()

    assert auth._lease is None
    assert session.post.call_count == 1


def test_invalid_json_timeout_and_5xx_are_typed_and_not_retried(caplog):
    raw_secret = "raw-body-must-not-appear"
    for effect, category in (
        (Response(json_error=ValueError(raw_secret), text=raw_secret), "invalid_json"),
        (requests.ReadTimeout(raw_secret), "timeout"),
        (Response(status_code=503, text=raw_secret), "http_status"),
    ):
        auth, session, _, _ = authenticator(Response())
        if isinstance(effect, BaseException):
            session.post.side_effect = effect
        else:
            session.post.return_value = effect
        with caplog.at_level(logging.INFO), pytest.raises(KiwoomAuthError) as caught:
            auth.get_token()
        assert caught.value.category == category
        assert session.post.call_count == 1
    assert raw_secret not in caplog.text


def test_only_429_retries_three_total_with_injected_sleeper():
    auth, session, _, sleeper = authenticator(Response(status_code=429))

    with pytest.raises(RateLimitExceededError):
        auth.get_token()

    assert session.post.call_count == 3
    assert [call.args[0] for call in sleeper.call_args_list] == [2.0, 4.0]


def test_expiry_ttl_uses_clock_after_response_not_request_start():
    session = Mock(spec=requests.Session)
    session.post.return_value = Response(payload=token_payload())
    clock = Mock(
        side_effect=[
            datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 17, 0, 58, tzinfo=timezone.utc),
        ]
    )
    auth = Authenticator(
        credentials(),
        KiwoomEndpoint.MOCK,
        session=session,
        clock=clock,
        sleeper=Mock(),
    )

    auth.get_token()

    assert auth._lease is not None
    assert auth._lease.usable_until == datetime(
        2026, 7, 17, 0, 59, 48, tzinfo=timezone.utc
    )


def test_fifty_concurrent_callers_share_one_token_post():
    auth, session, _, _ = authenticator(Response(payload=token_payload()))

    with ThreadPoolExecutor(max_workers=50) as executor:
        tokens = list(executor.map(lambda _: auth.get_token(), range(50)))

    assert session.post.call_count == 1
    assert len({id(token) for token in tokens}) == 1


def test_concurrent_failure_callers_share_one_safe_failure_result():
    auth, session, _, _ = authenticator(Response())
    session.post.side_effect = requests.ReadTimeout("raw timeout detail")

    def call():
        try:
            auth.get_token()
        except KiwoomAuthError as error:
            return error.category, str(error)
        raise AssertionError("failure was expected")

    with ThreadPoolExecutor(max_workers=20) as executor:
        outcomes = list(executor.map(lambda _: call(), range(20)))

    assert session.post.call_count == 1
    assert outcomes == [("timeout", "token issuance timed out")] * 20
    assert all("raw timeout detail" not in message for _, message in outcomes)


def test_failure_cache_allows_one_later_bounded_retry_after_safe_window():
    current = [datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)]
    session = Mock(spec=requests.Session)
    session.post.side_effect = requests.ReadTimeout("raw timeout detail")
    auth = Authenticator(
        credentials(),
        KiwoomEndpoint.MOCK,
        session=session,
        clock=lambda: current[0],
        sleeper=Mock(),
    )

    with pytest.raises(KiwoomAuthError):
        auth.get_token()
    with pytest.raises(KiwoomAuthError):
        auth.get_token()
    assert session.post.call_count == 1

    current[0] = datetime(2026, 7, 17, 0, 0, 2, tzinfo=timezone.utc)
    session.post.side_effect = None
    session.post.return_value = Response(payload=token_payload())

    assert auth.get_token().reveal_for_authorization() == "synthetic-token"
    assert session.post.call_count == 2


def test_revoke_success_is_explicit_one_shot_and_retires_owner():
    auth, session, _, _ = authenticator(Response(payload=token_payload()))
    auth.get_token()
    session.post.reset_mock()
    session.post.return_value = Response(payload={"return_code": 0, "return_msg": "ok"})

    assert auth.revoke() is RevokeResult.REVOKED
    assert auth._lease is None
    session.post.assert_called_once()
    _, kwargs = session.post.call_args
    assert kwargs["allow_redirects"] is False
    assert kwargs["verify"] is True
    assert kwargs["headers"]["api-id"] == "au10002"
    assert auth.revoke() is RevokeResult.UNKNOWN
    with pytest.raises(AuthenticatorClosedError):
        auth.get_token()


@pytest.mark.parametrize(
    ("effect", "expected"),
    [
        (Response(payload={"return_code": -1}), RevokeResult.REJECTED),
        (Response(status_code=503), RevokeResult.UNKNOWN),
        (Response(json_error=ValueError("bad")), RevokeResult.UNKNOWN),
        (requests.ReadTimeout("secret exception"), RevokeResult.UNKNOWN),
    ],
)
def test_revoke_rejected_or_unknown_never_retries_and_always_clears(effect, expected):
    auth, session, _, _ = authenticator(Response(payload=token_payload()))
    auth.get_token()
    session.post.reset_mock()
    if isinstance(effect, BaseException):
        session.post.side_effect = effect
    else:
        session.post.return_value = effect

    assert auth.revoke() is expected
    assert session.post.call_count == 1
    assert auth._lease is None
    with pytest.raises(AuthenticatorClosedError):
        auth.get_token()


def test_close_is_local_only_and_never_auto_revokes():
    auth, session, _, _ = authenticator(Response(payload=token_payload()))
    auth.get_token()
    session.post.reset_mock()

    auth.close()

    session.post.assert_not_called()
    with pytest.raises(AuthenticatorClosedError):
        auth.get_token()


def test_client_construction_is_network_lazy_and_explicit_readiness_uses_shared_session():
    session = Mock(spec=requests.Session)
    session.post.return_value = Response(payload=token_payload())
    client = KiwoomClient(
        credentials=credentials(),
        endpoint=KiwoomEndpoint.MOCK,
        session=session,
        clock=lambda: datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc),
        sleeper=Mock(),
    )

    session.post.assert_not_called()
    assert client.base._session is session
    assert client.auth._session is session

    client.ensure_auth_ready()

    assert session.post.call_count == 1
    session.post.reset_mock()
    client.close()
    session.post.assert_not_called()
    session.close.assert_not_called()


def test_standalone_authenticator_closes_owned_session_without_revoke(monkeypatch):
    owned_session = Mock(spec=requests.Session)
    monkeypatch.setattr(requests, "Session", Mock(return_value=owned_session))
    auth = Authenticator(credentials(), KiwoomEndpoint.MOCK)

    auth.close()
    auth.close()

    owned_session.post.assert_not_called()
    owned_session.close.assert_called_once_with()


def test_sensitive_values_and_bundle_enforce_runtime_types_and_controls():
    with pytest.raises(TypeError):
        KiwoomClientCredentials("raw", SensitiveText("secret"))  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        KiwoomClientCredentials(SensitiveText("app"), "raw")  # type: ignore[arg-type]
    with pytest.raises(CredentialProviderError):
        SensitiveText(123)  # type: ignore[arg-type]
    with pytest.raises(CredentialProviderError):
        SensitiveText("line\nbreak")
