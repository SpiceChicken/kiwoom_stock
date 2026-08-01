"""Verifier-owned real-process checks for the r3 promotion HTTP boundary.

These tests intentionally use only loopback listeners.  They exercise the
actual local curl binary through the production ``CurlHttpClient`` adapter;
no GitHub, AWS, SSM, EC2, or Kiwoom endpoint is reachable from this fixture.
"""

from __future__ import annotations

from contextlib import contextmanager
import http.server
import os
from pathlib import Path
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Iterator

import pytest

from kiwoom_stock.deployment import promotion


TOKEN = "verifier_token_123"


class RecordingRunner(promotion.SubprocessCommandRunner):
    def __init__(self) -> None:
        self.result: promotion.BinaryCommandResult | None = None

    def run_binary(self, *args, **kwargs):
        self.result = super().run_binary(*args, **kwargs)
        return self.result


class NativeCurlDeadlineRunner(RecordingRunner):
    """Let curl's stricter argv deadline win over the wrapper parent."""

    def run_binary(
        self, argv, env, timeout_seconds, output_limit, stdin
    ):
        return super().run_binary(
            argv, env, timeout_seconds + 0.5, output_limit, stdin
        )


class _RouteHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        self.server.route(self)  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _QuietThreadingHTTPServer(http.server.ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:
        del request, client_address


@contextmanager
def _server(route, tls_context: ssl.SSLContext | None = None) -> Iterator[str]:
    server = _QuietThreadingHTTPServer(("127.0.0.1", 0), _RouteHandler)
    server.route = route  # type: ignore[attr-defined]
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    else:
        scheme = "http"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"{scheme}://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def local_curl_fixture(tmp_path_factory):
    real_curl = shutil.which("curl")
    openssl = shutil.which("openssl")
    if real_curl is None or openssl is None:
        pytest.skip("local curl and openssl are required")

    root = tmp_path_factory.mktemp("promotion-curl-r3")
    cert = root / "loopback-cert.pem"
    key = root / "loopback-key.pem"
    generated = subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if generated.returncode != 0:
        pytest.skip("local openssl cannot generate a SAN certificate")

    curl_home = root / "curl-home"
    curl_home.mkdir()
    (curl_home / ".curlrc").write_text(
        'header = "X-Ambient-Curlrc: leaked"\n', encoding="utf-8"
    )
    bin_dir = root / "bin"
    bin_dir.mkdir()
    argv_log = root / "curl.argv"
    env_log = root / "curl.env"
    stdin_log = root / "curl.stdin"
    wrapper = bin_dir / "curl"
    wrapper.write_text(
        "#!/bin/bash\n"
        "set -o pipefail\n"
        f"export CURL_HOME={curl_home}\n"
        f"printf '%s\\n' \"$@\" > {argv_log}\n"
        f"env > {env_log}\n"
        f"tee {stdin_log} | {real_curl} \"$@\" --cacert {cert}\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return {
        "curl": real_curl,
        "bin": str(bin_dir),
        "context": context,
        "argv_log": argv_log,
        "env_log": env_log,
        "stdin_log": stdin_log,
    }


def _client(fixture, runner=None):
    return promotion.CurlHttpClient(
        runner or promotion.SubprocessCommandRunner(),
        f'{fixture["bin"]}:/usr/bin:/bin',
    )


def _get(client, url: str, limit: int = 4096, seconds: float = 2.0) -> bytes:
    clock = promotion.SystemClock()
    return client.get_bytes(
        url, TOKEN, limit, clock, clock.monotonic() + seconds
    )


def _respond(handler, body: bytes = b"ok") -> None:
    handler.send_response(200)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_local_curl_version_and_required_options(local_curl_fixture):
    version_text = subprocess.run(
        [local_curl_fixture["curl"], "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    version = version_text.split()[1]
    numeric = tuple(int(part) for part in version.split(".")[:3])
    assert numeric >= (8, 4, 0), (
        "curl 8.4+ is required for --max-filesize to abort unknown-size bodies"
    )
    help_text = subprocess.run(
        [local_curl_fixture["curl"], "--help", "all"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout
    for option in (
        "--disable",
        "--config",
        "--location",
        "--max-redirs",
        "--proto-redir",
        "--connect-timeout",
        "--max-time",
        "--max-filesize",
        "--user-agent",
    ):
        assert option in help_text


def test_real_curl_same_origin_keeps_auth_and_blocks_ambient_curlrc(
    local_curl_fixture,
):
    observations: list[tuple[str, str | None, str | None, str | None]] = []

    def route(handler):
        observations.append(
            (
                handler.path,
                handler.headers.get("Authorization"),
                handler.headers.get("User-Agent"),
                handler.headers.get("X-Ambient-Curlrc"),
            )
        )
        if handler.path == "/start":
            handler.send_response(302)
            handler.send_header("Location", "/final")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
        else:
            _respond(handler)

    with _server(route, local_curl_fixture["context"]) as base:
        body = _get(_client(local_curl_fixture), f"{base}/start")

    assert body == b"ok"
    assert observations == [
        ("/start", f"Bearer {TOKEN}", promotion.HTTP_USER_AGENT, None),
        ("/final", f"Bearer {TOKEN}", promotion.HTTP_USER_AGENT, None),
    ]


def test_real_curl_cross_origin_strips_authorization(local_curl_fixture):
    first_auth: list[str | None] = []
    second_auth: list[str | None] = []

    def destination(handler):
        second_auth.append(handler.headers.get("Authorization"))
        _respond(handler)

    with _server(destination, local_curl_fixture["context"]) as destination_base:
        destination_url = f"{destination_base}/final"

        def source(handler):
            first_auth.append(handler.headers.get("Authorization"))
            handler.send_response(302)
            handler.send_header("Location", destination_url)
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        with _server(source, local_curl_fixture["context"]) as source_base:
            # Change the source hostname while keeping both origins loopback-only.
            source_url = source_base.replace("127.0.0.1", "localhost")
            assert _get(_client(local_curl_fixture), source_url) == b"ok"

    assert first_auth == [f"Bearer {TOKEN}"]
    assert second_auth == [None]


def test_real_curl_rejects_https_downgrade_without_contact(local_curl_fixture):
    downgrade_contacts: list[str] = []

    def insecure(handler):
        downgrade_contacts.append(handler.path)
        _respond(handler)

    with _server(insecure) as insecure_base:
        def secure(handler):
            handler.send_response(302)
            handler.send_header("Location", f"{insecure_base}/forbidden")
            handler.send_header("Content-Length", "0")
            handler.end_headers()

        with _server(secure, local_curl_fixture["context"]) as secure_base:
            with pytest.raises(promotion.PromotionError) as raised:
                _get(_client(local_curl_fixture), f"{secure_base}/start")

    assert raised.value.category == "github_http_failed"
    assert downgrade_contacts == []


def test_real_curl_enforces_redirect_limit(local_curl_fixture):
    contacts: list[str] = []

    def route(handler):
        contacts.append(handler.path)
        number = int(handler.path.rsplit("/", 1)[1])
        handler.send_response(302)
        handler.send_header("Location", f"/redirect/{number + 1}")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with _server(route, local_curl_fixture["context"]) as base:
        with pytest.raises(promotion.PromotionError) as raised:
            _get(_client(local_curl_fixture), f"{base}/redirect/0")

    assert raised.value.category == "github_http_failed"
    assert contacts == [f"/redirect/{number}" for number in range(6)]


def test_actual_wrapper_observes_stdin_only_token_and_minimal_env(
    local_curl_fixture,
):
    def route(handler):
        _respond(handler)

    with _server(route, local_curl_fixture["context"]) as base:
        assert _get(_client(local_curl_fixture), base) == b"ok"

    argv = local_curl_fixture["argv_log"].read_text(encoding="utf-8")
    environment = local_curl_fixture["env_log"].read_text(encoding="utf-8")
    stdin = local_curl_fixture["stdin_log"].read_text(encoding="utf-8")
    assert TOKEN not in argv
    assert TOKEN not in environment
    assert stdin == f'header = "Authorization: Bearer {TOKEN}"\n'
    assert argv.splitlines()[0] == "--disable"
    assert "--location-trusted" not in argv
    assert set(
        line.split("=", 1)[0] for line in environment.splitlines() if "=" in line
    ) <= {"PATH", "PWD", "SHLVL", "CURL_HOME", "_"}


@pytest.mark.parametrize("phase", ["header", "body"])
def test_real_curl_slow_response_maps_to_absolute_deadline_category(
    local_curl_fixture, phase
):
    def route(handler):
        if phase == "header":
            time.sleep(0.5)
            _respond(handler)
            return
        handler.send_response(200)
        handler.send_header("Content-Length", "2")
        handler.end_headers()
        handler.wfile.write(b"x")
        handler.wfile.flush()
        time.sleep(0.5)
        handler.wfile.write(b"y")

    runner = NativeCurlDeadlineRunner()
    with _server(route, local_curl_fixture["context"]) as base:
        with pytest.raises(promotion.PromotionError) as raised:
            _get(_client(local_curl_fixture, runner), base, seconds=0.12)

    assert runner.result is not None
    assert runner.result.returncode == 28
    assert raised.value.category == "execution_deadline_exhausted"


def test_real_curl_tls_handshake_maps_to_absolute_deadline_category(
    local_curl_fixture,
):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    stop = threading.Event()

    def stall_tls() -> None:
        connection, _ = listener.accept()
        try:
            stop.wait(0.5)
        finally:
            connection.close()

    thread = threading.Thread(target=stall_tls, daemon=True)
    thread.start()
    runner = NativeCurlDeadlineRunner()
    try:
        with pytest.raises(promotion.PromotionError) as raised:
            _get(
                _client(local_curl_fixture, runner),
                f"https://127.0.0.1:{port}/",
                seconds=0.12,
            )
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)

    assert runner.result is not None
    assert runner.result.returncode == 28
    assert raised.value.category == "execution_deadline_exhausted"


def test_runner_kills_dns_worker_process_group_and_redacts(local_curl_fixture, tmp_path):
    marker = tmp_path / "dns-child-survived"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/bin/sh\n"
        f"(sleep 0.2; touch {marker}) &\n"
        "printf 'dns resolver received sensitive input' >&2\n"
        "wait\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    clock = promotion.SystemClock()
    client = promotion.CurlHttpClient(
        promotion.SubprocessCommandRunner(), f"{fake_bin}:/usr/bin:/bin"
    )
    with pytest.raises(promotion.PromotionError) as raised:
        client.get_bytes(
            "https://dns.invalid/secret-path",
            TOKEN,
            64,
            clock,
            clock.monotonic() + 0.03,
        )
    assert raised.value.category == "execution_deadline_exhausted"
    assert TOKEN not in str(raised.value)
    assert "secret-path" not in str(raised.value)
    time.sleep(0.25)
    assert not marker.exists()


def test_real_curl_exit_63_maps_to_response_size_category(local_curl_fixture):
    body = b"x" * 4096

    def route(handler):
        _respond(handler, body)

    runner = NativeCurlDeadlineRunner()
    with _server(route, local_curl_fixture["context"]) as base:
        with pytest.raises(promotion.PromotionError) as raised:
            _get(_client(local_curl_fixture, runner), base, limit=128)

    assert runner.result is not None
    assert runner.result.returncode == 63
    assert len(runner.result.stdout) <= 129
    assert len(runner.result.stderr) <= 129
    assert raised.value.category == "github_response_size_invalid"
