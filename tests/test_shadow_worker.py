from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from kiwoom_stock.application.credentials import (
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.application.execution import (
    ActivationTuple,
    ExecutionMode,
    ExecutionPolicy,
    ExecutionPolicyError,
    SHADOW_DATABASE_PATH,
)
from kiwoom_stock.application.runtime import (
    ShadowExecutionFailure,
    ShadowRuntime,
    create_shadow_runtime,
)
from kiwoom_stock.application.shadow_lifecycle import (
    ShadowRunDeadlineExceeded,
    ShadowStopRequested,
)
from kiwoom_stock.application.shadow_worker import (
    CalendarDecision,
    CalendarUnavailableError,
    ShadowAdmission,
    ShadowExecutionReceipt,
    ShadowCycleTerminated,
    ShadowTerminalReason,
    RuntimeStopEvent,
    ShadowWorkerError,
    run_shadow_once,
    run_shadow_continuous,
    run_shadow_once_managed,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    AllowlistedReadOnlySession,
    ReadOnlyBoundaryError,
)
from kiwoom_stock.infrastructure.shadow_process_lock import ShadowProcessLock
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.settings import Settings


def _policy() -> ExecutionPolicy:
    return ExecutionPolicy.for_request(
        ExecutionMode.SHADOW_ONCE,
        ActivationTuple(
            source_sha="a" * 40,
            image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            activation_id="shadow-test-1",
        ),
    )


def _continuous_policy() -> ExecutionPolicy:
    return ExecutionPolicy.for_request(
        ExecutionMode.SHADOW_CONTINUOUS,
        ActivationTuple(
            source_sha="a" * 40,
            image_digest="ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
            activation_id="shadow-test-1",
        ),
    )


class FakeRuntime:
    def __init__(self, *, cycle_error=None, attempts=6):
        self.events = []
        self.cycle_error = cycle_error
        self.db_path = Path("/isolated/shadow/trades.db")
        self.attempt_count = attempts

    def execute_once(self):
        self.events.append("execute_once")
        if self.cycle_error is not None:
            raise self.cycle_error
        return ShadowExecutionReceipt(
            cycles=1,
            http_attempts=self.attempt_count,
            api_counts={"token": 1, "stock_basic": 1},
            db_identity=str(self.db_path),
            resources_closed=True,
            local_counts={},
        )


class AdvancingStopEvent:
    def __init__(self, now, *, stop_after_waits=None):
        self.now = now
        self.stop_after_waits = stop_after_waits
        self.waits = []
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.now[0] += timeout or 0
        if self.stop_after_waits == len(self.waits):
            self._set = True
        return self._set


def test_runtime_stop_event_raises_typed_transition_once_then_allows_cleanup_checks():
    raw = threading.Event()
    observations = []
    event = RuntimeStopEvent(raw, lambda: observations.append("stop") or True)
    assert event.is_set() is False
    raw.set()
    with pytest.raises(ShadowStopRequested):
        event.is_set()
    assert event.is_set() is True
    assert observations == ["stop", "stop"]

def test_continuous_uses_fresh_one_shot_runtime_and_interruptible_sixty_second_gate(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now, stop_after_waits=2)
    runtimes = []
    evidence = []

    def factory(*_args):
        runtime = FakeRuntime()
        runtimes.append(runtime)
        return runtime

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=evidence.append,
        lock_path=(tmp_path / "continuous.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "STOPPED"
    assert result.exit_code == 0
    assert len(runtimes) == 2
    assert all(runtime.events == ["execute_once"] for runtime in runtimes)
    assert event.waits == [60.0, 60.0]
    assert [item["cycle_index"] for item in evidence] == [1, 2]
    assert all(item["resources_closed"] is True for item in evidence)
    assert all(not any(item["side_effects"].values()) for item in evidence)
    assert evidence[0]["cycle_start_elapsed_seconds"] == 0.0
    assert evidence[0]["observed_interval_seconds"] is None
    assert evidence[0]["db_reopened"] is False
    assert evidence[1]["cycle_start_elapsed_seconds"] == 60.0
    assert evidence[1]["observed_interval_seconds"] == 60.0
    assert evidence[1]["db_reopened"] is True
    assert result.first_cycle_start_elapsed_seconds == 0.0
    assert result.second_cycle_start_elapsed_seconds == 60.0
    assert result.second_cycle_interval_seconds == 60.0
    assert result.minimum_cycle_interval_seconds == 60.0
    assert result.db_reopens == 1


def test_continuous_hard_deadline_stops_before_constructing_another_cycle(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now)
    constructions = []
    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=lambda *_args: constructions.append(now[0]) or FakeRuntime(),
        emit=lambda _evidence: None,
        lock_path=(tmp_path / "deadline.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "DEADLINE"
    assert result.cycles == 15
    assert len(constructions) == 15
    assert now[0] == 900.0
    assert result.first_cycle_start_elapsed_seconds == 0.0
    assert result.second_cycle_start_elapsed_seconds == 60.0
    assert result.second_cycle_interval_seconds == 60.0
    assert result.minimum_cycle_interval_seconds == 60.0
    assert result.db_reopens == 14


def test_continuous_failure_is_redacted_and_never_starts_next_cycle(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now)
    constructions = []
    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=lambda *_args: constructions.append("runtime") or FakeRuntime(
            cycle_error=RuntimeError("secret provider body")
        ),
        emit=lambda _evidence: pytest.fail("failed cycle emitted as safe"),
        lock_path=(tmp_path / "failure.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "FAILED"
    assert result.exit_code == 1
    assert result.error_type == "RuntimeError"
    assert constructions == ["runtime"]
    assert "secret" not in json.dumps(result.to_safe_dict())


def test_continuous_fails_closed_when_the_database_identity_changes(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now, stop_after_waits=2)
    runtimes = []

    def factory(*_args):
        runtime = FakeRuntime()
        if runtimes:
            runtime.db_path = Path("/isolated/shadow/other-trades.db")
        runtimes.append(runtime)
        return runtime

    evidence = []
    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=evidence.append,
        lock_path=(tmp_path / "identity.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "FAILED"
    assert result.error_type == "ShadowDatabaseIdentityMismatch"
    assert result.cycles == 1
    assert len(evidence) == 1
    assert result.db_reopens == 0


def test_continuous_signal_cleanup_failure_is_failed_nonzero(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now)

    class CleanupFailureRuntime:
        def __init__(self, admission):
            self.admission = admission

        def execute_once(self):
            self.admission.stop_event.set()
            raise ShadowCycleTerminated(
                ShadowTerminalReason.STOP_REQUESTED,
                resources_closed=False,
                error_type="CleanupError",
            )

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=lambda _policy_value, admission: CleanupFailureRuntime(admission),
        emit=lambda _evidence: pytest.fail("failed cycle emitted"),
        lock_path=(tmp_path / "cleanup-failure.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "FAILED"
    assert result.exit_code == 1
    assert result.resources_closed is False
    assert result.reason == "stop-requested"


def test_active_cycle_signal_gives_runtime_dynamic_thirty_second_close_budget(tmp_path):
    now = [10.0]
    event = AdvancingStopEvent(now)
    remaining_seen = []

    class SignalDuringRuntime:
        def __init__(self, admission):
            self.admission = admission

        def execute_once(self):
            self.admission.stop_event.set()
            remaining_seen.append(self.admission.deadline_remaining())
            now[0] += 29.0
            remaining_seen.append(self.admission.deadline_remaining())
            return ShadowExecutionReceipt(
                cycles=1,
                http_attempts=0,
                api_counts={},
                db_identity=str(SHADOW_DATABASE_PATH),
                resources_closed=True,
                local_counts={},
            )

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=lambda _policy_value, admission: SignalDuringRuntime(admission),
        emit=lambda _evidence: pytest.fail("stopped cycle emitted"),
        lock_path=(tmp_path / "signal-budget.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert remaining_seen == [30.0, 1.0]
    assert result.status == "STOPPED"
    assert result.exit_code == 0
    assert result.resources_closed is True
    assert result.reason == "stop-requested"


def test_active_cycle_shutdown_budget_expiry_preserves_typed_failure_reason(tmp_path):
    now = [10.0]
    event = AdvancingStopEvent(now)

    class Monitor:
        def __init__(self, stop_event):
            self.stop_event = stop_event

        def run_shadow_cycle(self, _stock_code):
            self.stop_event.set()
            self.stop_event.is_set()
            pytest.fail("typed stop was not raised")

        def close(self):
            now[0] += 30.0

    class Closable:
        attempt_count = 0

        def safe_counts(self):
            return {}

        def close(self):
            return None

    def factory(policy, admission):
        client = Closable()
        return ShadowRuntime(
            policy=policy,
            client=client,
            monitor=Monitor(admission.stop_event),
            notifier=SimpleNamespace(safe_counts=lambda: {}),
            db_path=SHADOW_DATABASE_PATH,
            session=Closable(),
            stop_event=admission.stop_event,
            deadline_remaining=admission.deadline_remaining,
        )

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=lambda _evidence: pytest.fail("expired cycle emitted"),
        lock_path=(tmp_path / "shutdown-expired.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "FAILED"
    assert result.exit_code == 1
    assert result.reason == "shutdown-deadline"


def test_unrelated_primary_failure_crossing_run_deadline_remains_failed(tmp_path):
    now = [0.0]
    event = AdvancingStopEvent(now)

    class Monitor:
        def run_shadow_cycle(self, _stock_code):
            raise ValueError("provider failure")

        def close(self):
            now[0] = 900.0

    class Closable:
        attempt_count = 0

        def safe_counts(self):
            return {}

        def close(self):
            return None

    def factory(policy, admission):
        return ShadowRuntime(
            policy=policy,
            client=Closable(),
            monitor=Monitor(),
            notifier=SimpleNamespace(safe_counts=lambda: {}),
            db_path=SHADOW_DATABASE_PATH,
            session=Closable(),
            stop_event=admission.stop_event,
            deadline_remaining=admission.deadline_remaining,
        )

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=lambda _evidence: pytest.fail("failed cycle emitted"),
        lock_path=(tmp_path / "failure-precedence.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "FAILED"
    assert result.exit_code == 1
    assert result.reason == "failure"
    assert result.error_type == "ValueError"


def test_closed_calendar_constructs_no_credentials_client_or_database():
    calls = []

    result = run_shadow_once(
        _policy(),
        clock=lambda: datetime(2026, 8, 2, 12, tzinfo=timezone.utc),
        calendar=lambda target: calls.append(("calendar", target))
        or CalendarDecision.CLOSED,
        runtime_factory=lambda *_args: pytest.fail("runtime constructed"),
    )

    assert result.status == "CLOSED"
    assert result.http_attempts == 0
    assert result.cycles == 0
    assert result.db_identity is None
    assert calls == [("calendar", date(2026, 8, 2))]


def test_invalid_clock_and_calendar_fail_before_runtime_construction():
    def factory(*_args):
        pytest.fail("runtime constructed")

    with pytest.raises(ShadowWorkerError, match="aware"):
        run_shadow_once(
            _policy(),
            clock=lambda: datetime(2026, 8, 2),
            calendar=lambda _target: CalendarDecision.OPEN,
            runtime_factory=factory,
        )
    with pytest.raises(CalendarUnavailableError):
        run_shadow_once(
            _policy(),
            clock=lambda: datetime(2026, 8, 2, tzinfo=timezone.utc),
            calendar=lambda _target: (_ for _ in ()).throw(
                CalendarUnavailableError("unavailable")
            ),
            runtime_factory=factory,
        )


def test_open_calendar_runs_exactly_one_cycle_closes_and_returns_redacted_evidence():
    runtime = FakeRuntime()
    result = run_shadow_once(
        _policy(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        runtime_factory=lambda *_args: runtime,
    )

    assert runtime.events == ["execute_once"]
    assert result.status == "PASS"
    assert result.cycles == 1
    assert result.http_attempts == 6
    assert result.resources_closed is True
    assert all(value is False for value in result.side_effects.values())
    rendered = json.dumps(result.to_safe_dict(), sort_keys=True)
    assert "credential" not in rendered
    assert "token_value" not in rendered
    assert "raw_response" not in rendered


def test_worker_converts_utc_boundary_to_one_aware_kst_admission():
    captured = []

    result = run_shadow_once(
        _policy(),
        clock=lambda: datetime(2026, 8, 3, 0, 30, tzinfo=timezone.utc),
        calendar=lambda target: captured.append(target) or CalendarDecision.OPEN,
        runtime_factory=lambda _policy_value, admission: captured.append(admission)
        or FakeRuntime(),
    )

    admission = captured[1]
    assert result.status == "PASS"
    assert captured[0] == date(2026, 8, 3)
    assert admission.now == datetime(2026, 8, 3, 9, 30, tzinfo=ZoneInfo("Asia/Seoul"))
    assert admission.now.tzinfo.key == "Asia/Seoul"


def test_provider_or_cycle_failure_is_terminal_and_still_closes():
    runtime = FakeRuntime(cycle_error=RuntimeError("provider secret response"))
    with pytest.raises(RuntimeError, match="provider secret response"):
        run_shadow_once(
            _policy(),
            clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
            calendar=lambda _target: CalendarDecision.OPEN,
            runtime_factory=lambda *_args: runtime,
        )
    assert runtime.events == ["execute_once"]


def test_managed_worker_propagates_stop_event_into_runtime_and_releases_lock(tmp_path):
    stop_event = threading.Event()
    lock_path = (tmp_path / "shadow.lock").resolve()

    class StopOnExecute(FakeRuntime):
        def execute_once(self):
            result = super().execute_once()
            stop_event.set()
            return result

    runtime = StopOnExecute()
    with pytest.raises(ShadowWorkerError, match="lifecycle budget"):
        run_shadow_once_managed(
            _policy(),
            lock_path=lock_path,
            lock_factory=ShadowProcessLock,
            clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
            calendar=lambda _target: CalendarDecision.OPEN,
            runtime_factory=lambda _policy_value, admission: (
                runtime
                if admission.stop_event is stop_event
                else pytest.fail("managed stop event was not injected")
            ),
            stop_event=stop_event,
            monotonic=lambda: 0.0,
        )
    released = ShadowProcessLock(lock_path)
    released.acquire()
    released.release()


def test_local_shadow_notifier_imports_no_external_capability_modules():
    script = """
import sys
import kiwoom_stock.monitoring.local_shadow_notifier
forbidden = [
    name for name in sys.modules
    if name == 'pandas'
    or name == 'boto3'
    or name.startswith('slack_sdk')
    or name.startswith('google.generativeai')
]
assert forbidden == [], forbidden
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_shadow_session_treats_first_rate_limit_as_terminal():
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        terminate_on_rate_limit=True,
        sender=lambda method, url, **kwargs: calls.append((method, url))
        or SimpleNamespace(status_code=429),
    )
    with pytest.raises(ReadOnlyBoundaryError, match="rate limit"):
        session.post(
            "https://api.kiwoom.com/oauth2/token",
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "au10001",
            },
            json={
                "grant_type": "client_credentials",
                "appkey": "synthetic-app-key",
                "secretkey": "synthetic-secret-key",
            },
            timeout=10,
            allow_redirects=False,
            verify=True,
        )
    assert len(calls) == 1
    assert session.attempt_count == 1


def test_shadow_session_clamps_transport_timeout_to_remaining_deadline():
    captured = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        deadline_remaining=lambda: 2.0,
        sender=lambda method, url, **kwargs: captured.append(kwargs)
        or SimpleNamespace(status_code=200),
    )
    session.post(
        "https://api.kiwoom.com/oauth2/token",
        headers={
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        },
        json={
            "grant_type": "client_credentials",
            "appkey": "synthetic-app-key",
            "secretkey": "synthetic-secret-key",
        },
        timeout=10,
        allow_redirects=False,
        verify=True,
    )
    assert captured[0]["timeout"] == 2.0


@pytest.mark.parametrize(
    "unsafe",
    [
        {"params": {"leak": "1"}},
        {"files": {"payload": b"x"}},
        {"verify": False},
        {"allow_redirects": True},
        {"data": "duplicate"},
    ],
)
def test_market_session_rejects_unsafe_request_kwargs_before_sender(unsafe):
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=lambda *_args, **_kwargs: calls.append("sent"),
    )
    kwargs = {
        "headers": {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        },
        "json": {
            "grant_type": "client_credentials",
            "appkey": "synthetic-app-key",
            "secretkey": "synthetic-secret-key",
        },
        "timeout": 10,
        "allow_redirects": False,
        "verify": True,
        **unsafe,
    }
    with pytest.raises(ReadOnlyBoundaryError):
        session.post("https://api.kiwoom.com/oauth2/token", **kwargs)
    assert calls == []


def _safe_token_request():
    return {
        "headers": {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001",
        },
        "json": {
            "grant_type": "client_credentials",
            "appkey": "synthetic-app-key",
            "secretkey": "synthetic-secret-key",
        },
        "timeout": 10,
        "allow_redirects": False,
        "verify": True,
    }


@pytest.mark.parametrize("missing", ["timeout", "allow_redirects", "verify"])
def test_market_session_rejects_omitted_safe_options(missing):
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=lambda *_args, **_kwargs: calls.append("sent"),
    )
    request = _safe_token_request()
    request.pop(missing)
    with pytest.raises(ReadOnlyBoundaryError):
        session.post("https://api.kiwoom.com/oauth2/token", **request)
    assert calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeout", (5, 30)),
        ("allow_redirects", True),
        ("verify", False),
        ("Content-Type", "application/json"),
    ],
)
def test_market_session_rejects_mutated_safe_values(field, value):
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=lambda *_args, **_kwargs: calls.append("sent"),
    )
    request = _safe_token_request()
    if field == "Content-Type":
        request["headers"][field] = value
    else:
        request[field] = value
    with pytest.raises(ReadOnlyBoundaryError):
        session.post("https://api.kiwoom.com/oauth2/token", **request)
    assert calls == []


def test_market_session_rejects_positional_and_has_no_direct_send_surface():
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=lambda *_args, **_kwargs: calls.append("sent"),
    )
    with pytest.raises(ReadOnlyBoundaryError, match="positional"):
        session.post(
            "https://api.kiwoom.com/oauth2/token",
            object(),
            **_safe_token_request(),
        )
    with pytest.raises(ReadOnlyBoundaryError, match="direct prepared-request"):
        session.send(object())
    assert calls == []


@pytest.mark.parametrize("authorization", ["token", "Bearer", "Bearer two words"])
def test_market_session_rejects_invalid_bearer_before_sender(authorization):
    calls = []
    session = AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=lambda *_args, **_kwargs: calls.append("sent"),
    )
    with pytest.raises(ReadOnlyBoundaryError, match="authorization"):
        session.post(
            "https://api.kiwoom.com/api/dostk/stkinfo",
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "ka10001",
                "authorization": authorization,
            },
            json={"stk_cd": "005930"},
            timeout=(5, 30),
            allow_redirects=False,
            verify=True,
        )
    assert calls == []


class FakeMarketClient:
    def __init__(self, _credentials, *, session):
        self.market = self
        self._session = session
        self.attempt_count = 6
        self.closed = False

    def ensure_auth_ready(self):
        return None

    def get_stock_basic_info(self, _stock_code):
        return {
            "cur_prc": "71000",
            "trde_pre": "110",
            "trde_qty": "1000000",
            "mac": "4200000",
        }

    def get_minute_chart(self, _stock_code, _tic):
        return [
            {
                "cur_prc": str(70000 + index * 100),
                "open_pric": str(69900 + index * 100),
                "high_pric": str(70200 + index * 100),
                "low_pric": str(69800 + index * 100),
                "trde_qty": str(1000 + index),
            }
            for index in range(20)
        ]

    def get_tick_strength(self, _stock_code):
        return [{"cntr_str": str(95 - index)} for index in range(5)]

    def get_order_book(self, _stock_code):
        return {"tot_sel_req": "1000", "tot_buy_req": "1200"}

    def safe_counts(self):
        return {
            "token": 1,
            "stock_basic": 1,
            "stock_chart_5m": 1,
            "proxy_chart_60m": 1,
            "stock_strength": 1,
            "stock_orderbook": 1,
        }

    def close(self):
        self.closed = True


def test_shadow_composition_uses_only_cached_market_and_isolated_paper_db(tmp_path):
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    settings = Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-once",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-once",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir.resolve()),
            "KIWOOM_DB_PATH": str(SHADOW_DATABASE_PATH),
        }
    )
    credentials = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    runtime = create_shadow_runtime(
        policy=_policy(),
        settings=settings,
        admission=ShadowAdmission(
            now=datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
            kst_date=date(2026, 8, 3),
            decision=CalendarDecision.OPEN,
        ),
        credential_provider_factory=lambda _path: SimpleNamespace(
            load=lambda: credentials
        ),
        session_factory=lambda **_kwargs: object(),
        market_client_factory=FakeMarketClient,
        ledger_factory=lambda logical_path, clock: (
            pytest.fail("unexpected logical shadow path")
            if logical_path != SHADOW_DATABASE_PATH
            else TradeLogger(db_path, clock=clock)
        ),
    )

    receipt = runtime.execute_once()

    assert receipt.cycles == 1
    with sqlite3.connect(db_path) as connection:
        physics_rows = connection.execute(
            "SELECT COUNT(*) FROM physics_state WHERE stock_code = '005930'"
        ).fetchone()[0]
        updated_at = connection.execute(
            "SELECT last_updated FROM physics_state WHERE stock_code = '005930'"
        ).fetchone()[0]
    assert physics_rows == 1
    assert updated_at.startswith("2026-08-03 10:00:00")


def _shadow_settings(tmp_path, _db_path):
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir(exist_ok=True)
    return Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-once",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-once",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir.resolve()),
            "KIWOOM_DB_PATH": str(SHADOW_DATABASE_PATH),
        }
    )


def test_continuous_shadow_composition_requires_exact_process_name_before_credentials(tmp_path):
    credentials_dir = tmp_path / "continuous-credentials"
    credentials_dir.mkdir()
    settings = Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-continuous",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-once",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir.resolve()),
            "KIWOOM_DB_PATH": str(SHADOW_DATABASE_PATH),
        }
    )
    calls = []

    with pytest.raises(RuntimeError, match="kiwoom-shadow-worker"):
        create_shadow_runtime(
            policy=_continuous_policy(),
            settings=settings,
            admission=_open_admission(),
            credential_provider_factory=lambda _path: calls.append("credentials"),
        )

    assert calls == []


def _shadow_runtime(tmp_path, db_path, now, configure_engine=None):
    credentials = KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )
    def engine_factory(*args, **kwargs):
        engine = TradingEngine(*args, **kwargs)
        if configure_engine is not None:
            configure_engine(engine)
        return engine

    return create_shadow_runtime(
        policy=_policy(),
        settings=_shadow_settings(tmp_path, db_path),
        admission=ShadowAdmission(
            now=now,
            kst_date=now.date(),
            decision=CalendarDecision.OPEN,
        ),
        credential_provider_factory=lambda _path: SimpleNamespace(
            load=lambda: credentials
        ),
        session_factory=lambda **_kwargs: object(),
        market_client_factory=FakeMarketClient,
        ledger_factory=lambda logical_path, clock: (
            pytest.fail("unexpected logical shadow path")
            if logical_path != SHADOW_DATABASE_PATH
            else TradeLogger(db_path, clock=clock)
        ),
        engine_factory=engine_factory,
    )


def _paper_verdict(*, buy_signal, price):
    return {
        "stock_code": "005930",
        "status": "paper-test",
        "is_buy_signal": buy_signal,
        "price": price,
        "regime": "NEUTRAL",
        "atr_percent": 0.5,
        "down_atr_percent": 0.5,
        "forces": {
            "thrust": 1.0,
            "gravity": 0.0,
            "drag": 0.0,
            "magnetic": 0.0,
            "jerk": 0.0,
            "impulse": 0.0,
            "net_force": 1.0,
            "current_velocity": 1.0,
            "volume_drop_ratio": 1.0,
        },
    }


def test_shadow_policy_runtime_engine_persists_kst_paper_buy_and_sell(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    buy_now = datetime(2026, 8, 3, 10, 5, tzinfo=ZoneInfo("Asia/Seoul"))
    buy_runtime = _shadow_runtime(
        tmp_path,
        db_path,
        buy_now,
        configure_engine=lambda engine: setattr(
            engine.strategy,
            "evaluate",
            lambda _metrics: _paper_verdict(buy_signal=True, price=71000.0),
        ),
    )

    buy_receipt = buy_runtime.execute_once()

    assert buy_receipt.local_counts["paper_buy"] == 1
    with sqlite3.connect(db_path) as connection:
        buy_row = connection.execute(
            "SELECT status, buy_time FROM trades WHERE stock_code = '005930'"
        ).fetchone()
    assert buy_row == ("OPEN", "2026-08-03 10:05:00")

    sell_now = datetime(2026, 8, 3, 15, 28, tzinfo=ZoneInfo("Asia/Seoul"))

    def configure_sell(engine):
        engine.strategy.evaluate = lambda _metrics: _paper_verdict(
            buy_signal=False,
            price=72000.0,
        )
        engine.strategy.get_exit_reason = (
            lambda _pos, _price, _forces: "Day Trade Close"
        )

    sell_runtime = _shadow_runtime(
        tmp_path,
        db_path,
        sell_now,
        configure_engine=configure_sell,
    )

    sell_receipt = sell_runtime.execute_once()

    assert sell_receipt.local_counts["paper_sell"] == 1
    with sqlite3.connect(db_path) as connection:
        sell_row = connection.execute(
            "SELECT status, sell_time, sell_reason FROM trades WHERE stock_code = '005930'"
        ).fetchone()
    assert sell_row == ("CLOSED", "2026-08-03 15:28:00", "Day Trade Close")


def test_engine_work_owner_rejects_repeat_before_extra_physics_write(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    captured = []
    runtime = _shadow_runtime(
        tmp_path,
        db_path,
        datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
        configure_engine=captured.append,
    )

    runtime.execute_once()
    with pytest.raises(RuntimeError, match="consumed"):
        captured[0].run_shadow_cycle("005930")

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM physics_state").fetchone()[0] == 1


def test_paper_only_engine_rejects_continuous_run_surface(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    captured = []
    runtime = _shadow_runtime(
        tmp_path,
        db_path,
        datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
        configure_engine=captured.append,
    )

    with pytest.raises(RuntimeError, match="run_shadow_cycle only"):
        captured[0].run()

    runtime.execute_once()


def test_engine_work_owner_latches_failure_before_retry_work(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    captured = []
    work_calls = []

    def configure(engine):
        captured.append(engine)

        def fail_once():
            work_calls.append("analyzer")
            raise ValueError("safe failure")

        engine.analyzer.update_regime = fail_once

    runtime = _shadow_runtime(
        tmp_path,
        db_path,
        datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
        configure_engine=configure,
    )
    with pytest.raises(ShadowExecutionFailure) as failure:
        runtime.execute_once()
    assert failure.value.primary_type == "ValueError"
    with pytest.raises(RuntimeError, match="consumed"):
        captured[0].run_shadow_cycle("005930")
    assert work_calls == ["analyzer"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM physics_state").fetchone()[0] == 0


def test_engine_work_owner_rejects_concurrent_second_call(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    captured = []
    entered = threading.Event()
    release = threading.Event()
    worker_errors = []

    def configure(engine):
        captured.append(engine)
        original = engine.analyzer.update_regime

        def blocking_update():
            entered.set()
            assert release.wait(timeout=2)
            original()

        engine.analyzer.update_regime = blocking_update

    runtime = _shadow_runtime(
        tmp_path,
        db_path,
        datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
        configure_engine=configure,
    )

    def run_first():
        try:
            captured[0].run_shadow_cycle("005930")
        except BaseException as error:
            worker_errors.append(error)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(RuntimeError, match="consumed"):
        captured[0].run_shadow_cycle("005930")
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert worker_errors == []
    with pytest.raises(ShadowExecutionFailure) as consumed:
        runtime.execute_once()
    assert consumed.value.primary_type == "RuntimeError"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM physics_state").fetchone()[0] == 1


class _LatchMonitor:
    def __init__(self, entered=None, release=None, *, run_error=None, close_error=None):
        self.calls = 0
        self.entered = entered
        self.release = release
        self.run_error = run_error
        self.close_error = close_error

    def run_shadow_cycle(self, _code):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=2)
        if self.run_error is not None:
            raise self.run_error
        return {"cycles": 1}

    def close(self):
        if self.close_error is not None:
            raise self.close_error


class _LatchClient:
    attempt_count = 1

    def __init__(self, close_error=None):
        self.close_error = close_error

    def safe_counts(self):
        return {"token": 1}

    def close(self):
        if self.close_error is not None:
            raise self.close_error
        return None


def _latch_runtime(monitor, client=None, session=None, local_counts=None):
    safe_local_counts = local_counts if local_counts is not None else {
        "status": 1,
        "paper_buy": 0,
        "paper_sell": 0,
        "error": 0,
        "critical": 0,
    }
    return ShadowRuntime(
        policy=_policy(),
        client=client or _LatchClient(),
        monitor=monitor,
        notifier=SimpleNamespace(safe_counts=lambda: dict(safe_local_counts)),
        db_path=SHADOW_DATABASE_PATH,
        session=session or SimpleNamespace(close=lambda: None),
    )


def test_runtime_latch_rejects_repeat_after_success_and_failure():
    success_monitor = _LatchMonitor()
    success = _latch_runtime(success_monitor)
    assert success.execute_once().cycles == 1
    with pytest.raises(RuntimeError, match="consumed"):
        success.execute_once()
    assert success_monitor.calls == 1

    failure = _latch_runtime(_LatchMonitor(run_error=ValueError("secret detail")))
    with pytest.raises(ShadowExecutionFailure) as first:
        failure.execute_once()
    assert first.value.primary_type == "ValueError"
    assert "secret detail" not in str(first.value)
    with pytest.raises(RuntimeError, match="consumed"):
        failure.execute_once()


@pytest.mark.parametrize(
    "local_counts",
    (
        {},
        {"status": 1, "paper_buy": 0, "paper_sell": 0, "error": 0, "critical": 0, "extra": 0},
        {"status": True, "paper_buy": 0, "paper_sell": 0, "error": 0, "critical": 0},
        {"status": 1.0, "paper_buy": 0, "paper_sell": 0, "error": 0, "critical": 0},
        {"status": 1, "paper_buy": 2, "paper_sell": 0, "error": 0, "critical": 0},
        {"status": 1, "paper_buy": 1, "paper_sell": 1, "error": 0, "critical": 0},
    ),
)
def test_runtime_rejects_invalid_local_evidence_schema(local_counts):
    runtime = _latch_runtime(_LatchMonitor(), local_counts=local_counts)
    with pytest.raises(RuntimeError, match="invalid local evidence"):
        runtime.execute_once()


def test_runtime_latch_rejects_concurrent_second_caller_before_work():
    entered = threading.Event()
    release = threading.Event()
    monitor = _LatchMonitor(entered, release)
    runtime = _latch_runtime(monitor)
    errors = []
    worker = threading.Thread(target=lambda: runtime.execute_once())
    worker.start()
    assert entered.wait(timeout=2)
    with pytest.raises(RuntimeError, match="consumed"):
        runtime.execute_once()
    release.set()
    worker.join(timeout=2)
    assert monitor.calls == 1
    assert errors == []


def test_runtime_preserves_redacted_cycle_and_cleanup_failure_types():
    runtime = _latch_runtime(
        _LatchMonitor(
            run_error=ValueError("provider secret"),
            close_error=OSError("filesystem secret"),
        ),
        client=_LatchClient(close_error=ConnectionError("client secret")),
        session=SimpleNamespace(
            close=lambda: (_ for _ in ()).throw(TimeoutError("session secret"))
        ),
    )
    with pytest.raises(ShadowExecutionFailure) as failure:
        runtime.execute_once()
    assert failure.value.primary_type == "ValueError"
    assert failure.value.cleanup_type == "OSError"
    assert failure.value.cleanup_types == (
        "OSError",
        "ConnectionError",
        "TimeoutError",
    )
    assert "secret" not in str(failure.value)


def test_policy_rejects_shadow_db_symlink_and_unknown_hardlink(monkeypatch):
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda candidate: candidate == SHADOW_DATABASE_PATH.parent
        or original_is_symlink(candidate),
    )
    with pytest.raises(ExecutionPolicyError, match="symlink"):
        _policy().assert_shadow_database_identity(SHADOW_DATABASE_PATH)

    monkeypatch.setattr(Path, "is_symlink", lambda _candidate: False)
    monkeypatch.setattr(
        Path,
        "exists",
        lambda candidate: candidate == SHADOW_DATABASE_PATH,
    )
    monkeypatch.setattr(
        Path,
        "stat",
        lambda _candidate: SimpleNamespace(st_nlink=2),
    )
    with pytest.raises(ExecutionPolicyError, match="exactly one"):
        _policy().assert_shadow_database_identity(SHADOW_DATABASE_PATH)


class _CloseRecorder:
    def __init__(self, events, label):
        self.events = events
        self.label = label
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.events.append(self.label)


def _shadow_credentials():
    return KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )


def _open_admission():
    return ShadowAdmission(
        now=datetime(2026, 8, 3, 10, tzinfo=ZoneInfo("Asia/Seoul")),
        kst_date=date(2026, 8, 3),
        decision=CalendarDecision.OPEN,
    )


def test_shadow_construction_closes_unclaimed_session_when_client_factory_fails(
    tmp_path,
):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    events = []
    session = _CloseRecorder(events, "session")

    with pytest.raises(ShadowExecutionFailure) as caught:
        create_shadow_runtime(
            policy=_policy(),
            settings=_shadow_settings(tmp_path, db_path),
            admission=_open_admission(),
            credential_provider_factory=lambda _path: SimpleNamespace(
                load=_shadow_credentials
            ),
            session_factory=lambda **_kwargs: session,
            market_client_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("client construction")
            ),
        )

    assert caught.value.primary_type == "RuntimeError"
    assert caught.value.resources_closed is True
    assert events == ["session"]


def test_shadow_construction_rollback_failure_is_typed_and_not_closed(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    events = []
    class FailingClose(_CloseRecorder):
        def close(self):
            super().close()
            raise RuntimeError("close failed")

    session = FailingClose(events, "session")

    with pytest.raises(ShadowExecutionFailure) as caught:
        create_shadow_runtime(
            policy=_policy(),
            settings=_shadow_settings(tmp_path, db_path),
            admission=_open_admission(),
            credential_provider_factory=lambda _path: SimpleNamespace(
                load=_shadow_credentials
            ),
            session_factory=lambda **_kwargs: session,
            market_client_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("client construction")
            ),
        )

    assert caught.value.reason is ShadowTerminalReason.FAILURE
    assert caught.value.resources_closed is False
    assert caught.value.cleanup_types == ("RuntimeError",)


def test_continuous_pre_resource_construction_stop_is_clean_typed_terminal(tmp_path):
    event = threading.Event()

    def factory(_policy_value, admission):
        admission.stop_event.set()
        admission.checkpoint()
        pytest.fail("stop checkpoint returned")

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=lambda _evidence: pytest.fail("construction stop emitted cycle"),
        lock_path=(tmp_path / "construction-stop.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        stop_event=event,
        monotonic=lambda: 0.0,
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "STOPPED"
    assert result.resources_closed is True
    assert result.exit_code == 0


def test_continuous_construction_run_deadline_is_clean_typed_terminal(tmp_path):
    def factory(_policy_value, _admission):
        raise ShadowRunDeadlineExceeded("construction deadline")

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=lambda _evidence: pytest.fail("construction deadline emitted cycle"),
        lock_path=(tmp_path / "construction-deadline.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        monotonic=lambda: 0.0,
        lock_factory=ShadowProcessLock,
    )

    assert result.status == "DEADLINE"
    assert result.reason == "run-deadline"
    assert result.resources_closed is True
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("close_at", "close_fails", "status", "reason", "closed", "exit_code"),
    (
        (29.0, False, "STOPPED", "stop-requested", True, 0),
        (30.0, False, "FAILED", "shutdown-deadline", True, 1),
        (29.0, True, "FAILED", "stop-requested", False, 1),
        (30.0, True, "FAILED", "shutdown-deadline", False, 1),
    ),
)
def test_continuous_construction_stop_resolves_after_rollback_cleanup(
    tmp_path,
    close_at,
    close_fails,
    status,
    reason,
    closed,
    exit_code,
):
    now = [0.0]
    credentials_dir = tmp_path / "continuous-credentials"
    credentials_dir.mkdir()
    settings = Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-continuous",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-worker",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir.resolve()),
            "KIWOOM_DB_PATH": str(SHADOW_DATABASE_PATH),
        }
    )

    class ConstructionSession:
        def close(self):
            now[0] = close_at
            if close_fails:
                raise RuntimeError("rollback close failure")

    def factory(policy, admission):
        def session_factory(**_kwargs):
            assert admission.stop_event is not None
            admission.stop_event.set()
            return ConstructionSession()

        return create_shadow_runtime(
            policy=policy,
            settings=settings,
            admission=admission,
            credential_provider_factory=lambda _path: SimpleNamespace(
                load=_shadow_credentials
            ),
            session_factory=session_factory,
        )

    result = run_shadow_continuous(
        _continuous_policy(),
        runtime_factory=factory,
        emit=lambda _evidence: pytest.fail("construction stop emitted cycle"),
        lock_path=(tmp_path / f"construction-{close_at}-{close_fails}.lock").resolve(),
        clock=lambda: datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
        calendar=lambda _target: CalendarDecision.OPEN,
        monotonic=lambda: now[0],
        lock_factory=ShadowProcessLock,
    )

    assert result.status == status
    assert result.reason == reason
    assert result.resources_closed is closed
    assert result.exit_code == exit_code


def test_shadow_runtime_rejects_non_kst_admission_before_credentials(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    calls = []

    with pytest.raises(RuntimeError, match="aware KST"):
        create_shadow_runtime(
            policy=_policy(),
            settings=_shadow_settings(tmp_path, db_path),
            admission=ShadowAdmission(
                now=datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
                kst_date=date(2026, 8, 3),
                decision=CalendarDecision.OPEN,
            ),
            credential_provider_factory=lambda _path: calls.append("credentials"),
        )

    assert calls == []


def test_shadow_runtime_rejects_non_exact_db_setting_before_credentials(tmp_path):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    settings = Settings.from_mapping(
        {
            "KIWOOM_EXECUTION_MODE": "shadow-once",
            "KIWOOM_API_MODE": "prod",
            "KIWOOM_APP_ENV": "prod",
            "KIWOOM_PROCESS_NAME": "kiwoom-shadow-once",
            "KIWOOM_CREDENTIALS_DIR": str(credentials_dir),
            "KIWOOM_DB_PATH": str(db_path),
        }
    )
    calls = []
    with pytest.raises(ExecutionPolicyError, match="admitted shadow ledger"):
        create_shadow_runtime(
            policy=_policy(),
            settings=settings,
            admission=_open_admission(),
            credential_provider_factory=lambda _path: calls.append("credentials"),
        )
    assert calls == []


def test_shadow_construction_closes_owned_resources_when_engine_factory_fails(
    tmp_path,
):
    db_path = (tmp_path / "kiwoom-shadow" / "trades.db").resolve()
    db_path.parent.mkdir()
    events = []
    session = _CloseRecorder(events, "session")

    class ClosingMarketClient(FakeMarketClient):
        def close(self):
            events.append("client")
            session.close()

    ledger = _CloseRecorder(events, "ledger")
    repository = _CloseRecorder(events, "repository")

    with pytest.raises(ShadowExecutionFailure) as caught:
        create_shadow_runtime(
            policy=_policy(),
            settings=_shadow_settings(tmp_path, db_path),
            admission=_open_admission(),
            credential_provider_factory=lambda _path: SimpleNamespace(
                load=_shadow_credentials
            ),
            session_factory=lambda **_kwargs: session,
            market_client_factory=ClosingMarketClient,
            ledger_factory=lambda _path, _clock: ledger,
            physical_state_repository_factory=lambda _ledger: repository,
            engine_factory=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("engine construction")
            ),
        )

    assert caught.value.primary_type == "RuntimeError"
    assert caught.value.resources_closed is True
    assert events == ["repository", "ledger", "client", "session"]
