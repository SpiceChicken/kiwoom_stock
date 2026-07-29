"""Non-activating host Compose preflight contract tests."""

from pathlib import Path
import json
import os
import shutil
import subprocess

import pytest

from docker.compose_preflight import ComposePreflightError
from docker.compose_preflight import compose_command
from docker.compose_preflight import run_preflight
from docker.validate_secret_paths import SecretPathValidationError


PRODUCTION_DIGEST = (
    "ghcr.io/spicechicken/kiwoom_stock@sha256:" + ("a" * 64)
)


def _pair(root: Path) -> tuple[Path, Path]:
    directory = root / "credentials"
    directory.mkdir(mode=0o700)
    app = directory / "app-key"
    secret = directory / "secret-key"
    app.write_text("synthetic-app-key\n", encoding="utf-8")
    secret.write_text("synthetic-secret-key\n", encoding="utf-8")
    app.chmod(0o400)
    secret.chmod(0o400)
    return app, secret


def _environment(app: Path, secret: Path) -> dict[str, str]:
    check_data = app.parent / "check-data"
    check_data.mkdir(exist_ok=True)
    return {
        "KIWOOM_IMAGE": PRODUCTION_DIGEST,
        "KIWOOM_PROD_APP_KEY_FILE": str(app),
        "KIWOOM_PROD_SECRET_KEY_FILE": str(secret),
        "KIWOOM_CHECK_DATA_DIR": str(check_data),
    }


def test_compose_command_is_render_only():
    assert compose_command("prod") == (
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        "compose.prod.yaml",
        "config",
        "--quiet",
    )
    assert "run" not in compose_command("prod")
    assert "up" not in compose_command("prod")


def test_preflight_validates_then_renders_without_secret_values(tmp_path):
    app, secret = _pair(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir()
    calls = []

    def fake_runner(command, **kwargs):
        calls.append((command, kwargs))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    run_preflight(
        "prod",
        repository_root=root,
        environ=_environment(app, secret),
        runner=fake_runner,
    )

    assert len(calls) == 1
    command, kwargs = calls[0]
    assert command == compose_command("prod")
    assert kwargs["cwd"] == root
    assert kwargs["check"] is False
    assert kwargs["capture_output"] is True
    assert kwargs["env"]["KIWOOM_PROD_APP_KEY_FILE"] == str(app)
    assert "synthetic-app-key" not in command
    assert "synthetic-secret-key" not in command


def test_preflight_does_not_render_when_secret_validation_fails(tmp_path):
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(command)

    with pytest.raises(SecretPathValidationError):
        run_preflight(
            "prod",
            repository_root=tmp_path,
            environ={},
            runner=fake_runner,
        )
    assert calls == []


def test_preflight_hides_compose_failure_output(tmp_path):
    app, secret = _pair(tmp_path)
    root = tmp_path / "checkout"
    root.mkdir()

    def fake_runner(command, **kwargs):
        class Result:
            returncode = 1
            stdout = "credential-value-must-not-be-forwarded"
            stderr = "compose failure"

        return Result()

    with pytest.raises(ComposePreflightError, match="rendering failed"):
        run_preflight(
            "prod",
            repository_root=root,
            environ=_environment(app, secret),
            runner=fake_runner,
        )


def test_prod_compose_requires_immutable_image_and_labels_check_containers():
    text = Path("compose.prod.yaml").read_text(encoding="utf-8")

    assert "${KIWOOM_IMAGE:?" in text
    assert "KIWOOM_IMAGE:-" not in text
    assert "io.kiwoom-stock.project: kiwoom-stock" in text
    assert (
        "io.kiwoom-stock.lifecycle: production-check-only"
        in text
    )
    assert "restart:" not in text


def test_effective_prod_compose_has_no_network_or_production_named_volume(
    tmp_path,
):
    docker_available = shutil.which("docker") is not None and subprocess.run(
        ["docker", "version"],
        check=False,
        capture_output=True,
        text=True,
    ).returncode == 0
    if not docker_available:
        pytest.skip("Docker Compose is unavailable")
    app, secret = _pair(tmp_path)
    environment = dict(os.environ)
    environment.update(_environment(app, secret))
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "compose.yaml",
            "-f",
            "compose.prod.yaml",
            "config",
            "--format",
            "json",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    effective = json.loads(completed.stdout)
    app_service = effective["services"]["app"]
    assert app_service["network_mode"] == "none"
    assert app_service["privileged"] is False
    assert app_service["read_only"] is True
    assert app_service["user"] == "0:0"
    assert app_service["cap_drop"] == ["ALL"]
    assert app_service["cap_add"] == ["CHOWN", "SETGID", "SETUID"]
    assert app_service["security_opt"] == ["no-new-privileges:true"]
    assert app_service["command"] == [
        "python",
        "-m",
        "kiwoom_stock",
        "--check-config",
    ]
    assert float(app_service["cpus"]) == 0.75
    assert int(app_service["mem_limit"]) == 536870912
    assert int(app_service["pids_limit"]) == 128
    assert app_service.get("networks") in (None, {})
    assert app_service["volumes"] == [
        {
            "type": "bind",
            "source": environment["KIWOOM_CHECK_DATA_DIR"],
            "target": "/var/lib/kiwoom",
            "read_only": True,
        }
    ]
    assert "kiwoom-data" not in {
        volume.get("source") for volume in app_service["volumes"]
    }
