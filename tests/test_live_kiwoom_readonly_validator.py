"""Offline contracts for the installed market-only live validator."""

from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest
from kiwoom_stock.application.credentials import (
    CredentialProviderError,
    KiwoomClientCredentials,
    SensitiveText,
)
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    APP_KEY_FILE,
    MATERIALIZED_APP_KEY_FILE,
    MATERIALIZED_SECRET_KEY_FILE,
    SECRET_KEY_FILE,
)
from kiwoom_stock.validation import live_readonly as validator


def test_read_only_boundary_remains_a_validation_error():
    assert issubclass(validator.ReadOnlyBoundaryError, validator.ValidationError)
    with pytest.raises(validator.ValidationError):
        raise validator.ReadOnlyBoundaryError("safe boundary")


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def _chart() -> list[dict[str, str]]:
    return [
        {
            "cur_prc": str(70_000 - index * 20),
            "open_pric": str(69_900 - index * 20),
            "high_pric": str(70_150 - index * 20),
            "low_pric": str(69_800 - index * 20),
            "trde_qty": str(1_000 + index * 50),
        }
        for index in range(20)
    ]


class RecordingSender:
    def __init__(
        self,
        *,
        basic_401_once: bool = False,
        basic_503_once: bool = False,
        empty_strength: bool = False,
        empty_proxy: bool = False,
        malformed_chart_field: str | None = None,
        missing_basic_field: str | None = None,
        missing_orderbook_field: str | None = None,
        missing_strength: bool = False,
        signed_comma_numbers: bool = False,
    ):
        self.calls: list[tuple[str, str]] = []
        self.basic_401_once = basic_401_once
        self.basic_503_once = basic_503_once
        self.empty_strength = empty_strength
        self.empty_proxy = empty_proxy
        self.malformed_chart_field = malformed_chart_field
        self.missing_basic_field = missing_basic_field
        self.missing_orderbook_field = missing_orderbook_field
        self.missing_strength = missing_strength
        self.signed_comma_numbers = signed_comma_numbers

    def __call__(self, method: str, url: str, **kwargs: Any):
        api_id = kwargs["headers"]["api-id"]
        payload = kwargs["json"]
        self.calls.append((method, api_id))
        if api_id == "au10001":
            return FakeResponse(
                {
                    "return_code": 0,
                    "token": "synthetic-token",
                    "token_type": "bearer",
                    "expires_dt": "20990101000000",
                }
            )
        if api_id == "ka10001":
            if self.basic_401_once:
                self.basic_401_once = False
                return FakeResponse({}, status_code=401)
            if self.basic_503_once:
                self.basic_503_once = False
                return FakeResponse({}, status_code=503)
            basic = {
                "return_code": 0,
                "cur_prc": "70380",
                "trde_pre": "125.5",
                "trde_qty": "2500000",
                "mac": "4200000",
            }
            if self.missing_basic_field is not None:
                basic.pop(self.missing_basic_field)
            if self.signed_comma_numbers:
                basic.update(
                    {
                        "cur_prc": "-70,380",
                        "trde_pre": "+125.5",
                        "trde_qty": "+2,500,000",
                        "mac": "4,200,000",
                    }
                )
            return FakeResponse(basic)
        if api_id == "ka10080":
            rows = (
                []
                if self.empty_proxy and payload["tic_scope"] == "60"
                else _chart()
            )
            if rows and self.malformed_chart_field is not None:
                rows[0][self.malformed_chart_field] = "raw-private-value"
            if rows and self.signed_comma_numbers:
                for row in rows:
                    for field_name in (
                        "cur_prc",
                        "open_pric",
                        "high_pric",
                        "low_pric",
                    ):
                        row[field_name] = f"-{int(row[field_name]):,}"
                    row["trde_qty"] = f"+{int(row['trde_qty']):,}"
            return FakeResponse(
                {"return_code": 0, "stk_min_pole_chart_qry": rows}
            )
        if api_id == "ka10046":
            strength = [
                {"cntr_str": str(125 - index)} for index in range(5)
            ]
            if self.empty_strength:
                strength = []
            elif self.missing_strength:
                strength[4] = {}
            elif self.signed_comma_numbers:
                for row in strength:
                    row["cntr_str"] = f"+{float(row['cntr_str']):,.1f}"
            return FakeResponse(
                {
                    "return_code": 0,
                    "cntr_str_tm": strength,
                }
            )
        if api_id == "ka10004":
            order_book = {
                "return_code": 0,
                "tot_sel_req": "9000",
                "tot_buy_req": "12000",
            }
            if self.missing_orderbook_field is not None:
                order_book.pop(self.missing_orderbook_field)
            if self.signed_comma_numbers:
                order_book.update(
                    {
                        "tot_sel_req": "9,000",
                        "tot_buy_req": "12,000",
                    }
                )
            return FakeResponse(order_book)
        raise AssertionError("allowlist should reject before sender")


def _credentials() -> KiwoomClientCredentials:
    return KiwoomClientCredentials(
        SensitiveText("synthetic-app-key"),
        SensitiveText("synthetic-secret-key"),
    )


def _client(
    sender: RecordingSender,
    *,
    max_attempts: int = validator.MAX_HTTP_ATTEMPTS,
) -> validator.MarketOnlyClient:
    session = validator.AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        max_attempts=max_attempts,
        sender=sender,
    )
    return validator.MarketOnlyClient(
        _credentials(),
        session=session,
        clock=lambda: datetime(2026, 7, 26, tzinfo=timezone.utc),
        sleeper=lambda _delay: None,
    )


def test_market_only_pipeline_has_exact_calls_regime_state_and_safe_dto():
    sender = RecordingSender()
    client = _client(sender)

    assert not hasattr(client, "account")
    result = validator.run_with_client(
        client,
        stock_code="005930",
        proxy_code="069500",
    )

    assert sender.calls == [
        ("POST", "au10001"),
        ("POST", "ka10001"),
        ("POST", "ka10080"),
        ("POST", "ka10080"),
        ("POST", "ka10046"),
        ("POST", "ka10004"),
    ]
    required_sequence = [
        "stock_basic",
        "stock_chart_5m",
        "proxy_chart_60m",
        "stock_strength",
        "stock_orderbook",
    ]
    assert list(validator.EXPECTED_LOGICAL_SEQUENCE) == required_sequence
    assert result["logical_api_sequence"] == required_sequence
    assert result["market_regime"] != "UNKNOWN"
    assert result["verdict"] == {
        "status": "🛑수급 빈곤 (Thrust Low)",
        "is_buy_signal": False,
        "regime": "QUIET_BEAR",
    }
    assert result["verdict"]["regime"] == result["market_regime"]
    assert result["state_submissions"] == 1
    assert set(result["forces"]) == validator.EXPECTED_FORCE_KEYS
    assert result["side_effects"] == {
        "orders": False,
        "account": False,
        "revoke": False,
        "database": False,
        "reports": False,
        "notifications": False,
    }
    assert result["metrics"]["trend_rsi"] >= 0.0
    assert all(
        value > 0 and isinstance(value, float)
        for key, value in result["metrics"].items()
        if key != "trend_rsi"
    )
    rendered = json.dumps(result, sort_keys=True)
    for forbidden in (
        "synthetic-app-key",
        "synthetic-secret-key",
        "synthetic-token",
        "raw_response",
        "credential",
    ):
        assert forbidden not in rendered


def test_401_refresh_is_counted_inside_bounded_shared_session():
    sender = RecordingSender(basic_401_once=True)
    client = _client(sender)

    result = validator.run_with_client(
        client,
        stock_code="005930",
        proxy_code="069500",
    )

    assert result["api_counts"]["token"] == 2
    assert result["api_counts"]["stock_basic"] == 2
    assert result["http_attempts"] == 8
    assert result["http_attempts"] <= validator.MAX_HTTP_ATTEMPTS


def test_signed_comma_numeric_snapshot_remains_compatible_and_passes():
    result = validator.run_with_client(
        _client(RecordingSender(signed_comma_numbers=True)),
        stock_code="005930",
        proxy_code="069500",
    )

    assert result["status"] == "PASS"
    assert result["verdict"]["regime"] == result["market_regime"]


def test_retryable_read_is_counted_inside_bounded_shared_session():
    sender = RecordingSender(basic_503_once=True)
    client = _client(sender)

    result = validator.run_with_client(
        client,
        stock_code="005930",
        proxy_code="069500",
    )

    assert result["api_counts"]["token"] == 1
    assert result["api_counts"]["stock_basic"] == 2
    assert result["http_attempts"] == 7
    assert result["http_attempts"] <= validator.MAX_HTTP_ATTEMPTS


@pytest.mark.parametrize(
    ("path", "api_id"),
    [
        ("/oauth2/revoke", "au10002"),
        ("/api/dostk/acnt", "kt00004"),
        ("/api/dostk/ordr", "kt10000"),
        ("/api/dostk/stkinfo", "unknown"),
    ],
)
def test_unknown_account_revoke_and_order_pairs_fail_before_transport(
    path,
    api_id,
):
    sender = RecordingSender()
    session = validator.AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=sender,
    )

    with pytest.raises(validator.ReadOnlyBoundaryError):
        session.post(
            f"https://api.kiwoom.com{path}",
            headers={"api-id": api_id},
            json={"stk_cd": "005930"},
            timeout=(5, 30),
            allow_redirects=False,
            verify=True,
        )
    assert sender.calls == []


def test_chart_boundary_allows_only_stock_5m_and_proxy_60m():
    sender = RecordingSender()
    session = validator.AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        sender=sender,
    )

    with pytest.raises(validator.ReadOnlyBoundaryError, match="5m"):
        session.post(
            "https://api.kiwoom.com/api/dostk/chart",
            headers={
                "Content-Type": "application/json;charset=UTF-8",
                "api-id": "ka10080",
                "authorization": "Bearer synthetic-token",
            },
            json={
                "stk_cd": "005930",
                "tic_scope": "1",
                "upd_stkpc_tp": "1",
            },
            timeout=(5, 30),
            allow_redirects=False,
            verify=True,
        )


def test_total_http_attempt_budget_fails_closed():
    sender = RecordingSender()
    client = _client(sender, max_attempts=1)
    client.ensure_auth_ready()

    with pytest.raises(validator.ReadOnlyBoundaryError, match="budget"):
        client.market.get_stock_basic_info("005930")
    client.close()


def test_attempt_cap_allows_23_transports_and_blocks_24th():
    sender = RecordingSender()
    session = validator.AllowlistedReadOnlySession(
        stock_code="005930",
        proxy_code="069500",
        max_attempts=23,
        sender=sender,
    )
    request = {
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

    for _ in range(23):
        session.post(
            "https://api.kiwoom.com/oauth2/token",
            **request,
        )
    with pytest.raises(
        validator.ReadOnlyBoundaryError,
        match="budget",
    ):
        session.post(
            "https://api.kiwoom.com/oauth2/token",
            **request,
        )

    assert len(sender.calls) == 23
    assert session.attempt_count == 24


def test_silent_analyzer_fallback_cannot_pass_with_unknown_regime():
    sender = RecordingSender(empty_proxy=True)
    client = _client(sender)

    with pytest.raises(
        validator.ValidationError,
        match=r"market snapshot collection failed \(empty\)",
    ):
        validator.run_with_client(
            client,
            stock_code="005930",
            proxy_code="069500",
        )


@pytest.mark.parametrize("empty_strength", [True, False])
def test_empty_or_missing_strength_fails_before_analyzer_fallback(
    empty_strength,
):
    sender = RecordingSender(
        empty_strength=empty_strength,
        missing_strength=not empty_strength,
    )

    expected_kind = "empty" if empty_strength else "malformed"
    with pytest.raises(
        validator.ValidationError,
        match=rf"market snapshot collection failed \({expected_kind}\)",
    ) as caught:
        validator.run_with_client(
            _client(sender),
            stock_code="005930",
            proxy_code="069500",
        )

    assert "raw-private-value" not in str(caught.value)


def test_non_mapping_strength_row_is_normalized_to_validation_error():
    snapshot = validator.MarketSnapshot(
        basic={
            "cur_prc": 70000.0,
            "trde_pre": 100.0,
            "trde_qty": 1000.0,
            "mac": 1000000.0,
        },
        stock_chart=[
            {key: float(value) for key, value in row.items()}
            for row in _chart()
        ],
        proxy_chart=[
            {key: float(value) for key, value in row.items()}
            for row in _chart()
        ],
        strength=["malformed"] * 5,
        order_book={"tot_sel_req": 100.0, "tot_buy_req": 200.0},
    )
    with pytest.raises(validator.ValidationError, match="strength.row0") as caught:
        validator.validate_market_snapshot(snapshot)
    assert isinstance(caught.value, validator.ReadOnlyBoundaryError)


@pytest.mark.parametrize(
    "field_name",
    ["tot_sel_req", "tot_buy_req"],
)
def test_missing_orderbook_total_fails_before_analyzer_fallback(field_name):
    sender = RecordingSender(missing_orderbook_field=field_name)

    with pytest.raises(
        validator.ValidationError,
        match=r"market snapshot collection failed \(malformed\)",
    ):
        validator.run_with_client(
            _client(sender),
            stock_code="005930",
            proxy_code="069500",
        )


@pytest.mark.parametrize("field_name", ["trde_qty", "mac"])
def test_missing_basic_volume_or_market_cap_fails_closed(field_name):
    sender = RecordingSender(missing_basic_field=field_name)

    with pytest.raises(
        validator.ValidationError,
        match=r"market snapshot collection failed \(malformed\)",
    ):
        validator.run_with_client(
            _client(sender),
            stock_code="005930",
            proxy_code="069500",
        )


@pytest.mark.parametrize(
    "field_name",
    ["cur_prc", "open_pric", "high_pric", "low_pric", "trde_qty"],
)
def test_malformed_consumed_chart_field_fails_closed_without_raw_value(
    field_name,
):
    sender = RecordingSender(malformed_chart_field=field_name)

    with pytest.raises(
        validator.ValidationError,
        match=r"market snapshot collection failed \(parse\)",
    ) as caught:
        validator.run_with_client(
            _client(sender),
            stock_code="005930",
            proxy_code="069500",
        )

    assert "raw-private-value" not in str(caught.value)


def test_dependency_error_filter_does_not_render_raw_error(caplog):
    dependency_logger = logging.getLogger(
        "kiwoom_stock.monitoring.analyzer"
    )
    with caplog.at_level(logging.ERROR):
        with validator._safe_dependency_logging():
            dependency_logger.error("credential-material raw provider error")

    assert "credential-material" not in caplog.text
    assert "details redacted" in caplog.text


def test_confirmation_guard_blocks_before_credentials(monkeypatch, capsys):
    def forbidden_provider(*args, **kwargs):
        raise AssertionError("credentials must not be opened")

    monkeypatch.setattr(
        validator,
        "StrictFileCredentialProvider",
        forbidden_provider,
    )
    result = validator.main(
        ["--credentials-dir", "/external/credentials"]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "ValidationError" in output
    assert "credential" not in output.casefold()


@pytest.mark.parametrize(
    ("names", "expected"),
    [
        ((APP_KEY_FILE, SECRET_KEY_FILE), (APP_KEY_FILE, SECRET_KEY_FILE)),
        (
            (MATERIALIZED_APP_KEY_FILE, MATERIALIZED_SECRET_KEY_FILE),
            (MATERIALIZED_APP_KEY_FILE, MATERIALIZED_SECRET_KEY_FILE),
        ),
    ],
)
def test_credential_layout_selector_accepts_only_complete_approved_pairs(
    tmp_path, names, expected
):
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    for name in names:
        (credentials_dir / name).write_text("placeholder", encoding="utf-8")

    assert validator._credential_file_names(credentials_dir) == expected


@pytest.mark.parametrize(
    "names",
    [
        (APP_KEY_FILE,),
        (SECRET_KEY_FILE,),
        (MATERIALIZED_APP_KEY_FILE,),
        (MATERIALIZED_SECRET_KEY_FILE,),
        (APP_KEY_FILE, MATERIALIZED_SECRET_KEY_FILE),
        (
            APP_KEY_FILE,
            SECRET_KEY_FILE,
            MATERIALIZED_APP_KEY_FILE,
            MATERIALIZED_SECRET_KEY_FILE,
        ),
    ],
)
def test_credential_layout_selector_rejects_missing_or_mixed_pairs(
    tmp_path, names
):
    credentials_dir = tmp_path / "credentials"
    credentials_dir.mkdir()
    for name in names:
        (credentials_dir / name).write_text("placeholder", encoding="utf-8")

    with pytest.raises(
        CredentialProviderError,
        match="single approved file pair",
    ):
        validator._credential_file_names(credentials_dir)


def test_production_cli_keeps_regime_proxy_fixed():
    help_text = validator._parser().format_help()

    assert "--proxy-code" not in help_text
    assert validator.REGIME_PROXY_CODE == "069500"


def test_root_tool_is_thin_wrapper_and_console_script_is_declared():
    wrapper = Path("tools/validate_live_kiwoom_readonly.py").read_text(
        encoding="utf-8"
    )
    project = Path("pyproject.toml").read_text(encoding="utf-8")

    assert "KiwoomClient" not in wrapper
    assert "AccountService" not in wrapper
    assert "kiwoom_stock.validation.live_readonly import main" in wrapper
    assert (
        "kiwoom-live-readonly-validate = "
        '"kiwoom_stock.validation.live_readonly:main"'
    ) in project


def test_installed_validator_excludes_forbidden_side_effect_imports():
    source = Path(validator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported.isdisjoint(
        {
            "kiwoom_stock.api.client",
            "kiwoom_stock.api.services.account",
            "kiwoom_stock.core.database",
            "kiwoom_stock.monitoring.engine",
            "kiwoom_stock.monitoring.notifier",
            "kiwoom_stock.reporting",
            "kiwoom_stock.utils.gemini_client",
            "kiwoom_stock.utils.s3_manager",
        }
    )


def test_console_help_uses_package_entrypoint_without_network():
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path("src").resolve())
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from kiwoom_stock.validation.live_readonly import main; "
                "main(['--help'])"
            ),
        ],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert "--confirm-prod-read-only" in completed.stdout


def test_built_wheel_installs_console_help(tmp_path):
    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    executable_dir = "Scripts" if sys.platform == "win32" else "bin"
    base_python_name = "python.exe" if sys.platform == "win32" else "python3"
    build_python = Path(sys.base_prefix) / executable_dir / base_python_name
    build = subprocess.run(
        [
            str(build_python),
            "-m",
            "pip",
            "wheel",
            "--no-build-isolation",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            ".",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    wheel = next(wheel_dir.glob("kiwoom_stock-*.whl"))

    install_prefix = tmp_path / "installed"
    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--ignore-installed",
            "--prefix",
            str(install_prefix),
            str(wheel),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, install.stderr

    installed_package = next(
        install_prefix.glob("lib/python*/site-packages/kiwoom_stock")
    )
    installed_site = installed_package.parent
    clean_environment = os.environ.copy()
    clean_environment["PYTHONPATH"] = str(installed_site)
    import_result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import kiwoom_stock; print(kiwoom_stock.__file__)",
        ],
        cwd=tmp_path,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert import_result.returncode == 0, import_result.stderr
    assert str(installed_package) in import_result.stdout

    command = install_prefix / executable_dir / "kiwoom-live-readonly-validate"
    help_result = subprocess.run(
        [str(command), "--help"],
        cwd=tmp_path,
        env=clean_environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0, help_result.stderr
    assert "--confirm-prod-read-only" in help_result.stdout
