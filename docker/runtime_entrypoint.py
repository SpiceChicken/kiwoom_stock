"""Harden file-backed Compose secrets before dropping to the runtime user."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import sys


RUNTIME_UID = 10001
RUNTIME_GID = 10001
SECRET_NAMES = ("KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY")
STAGING_DIR = Path("/run/kiwoom-secrets")
_IMMUTABLE_IMAGE = re.compile(
    r"^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$"
)
_SHADOW_PROCESS_PAIRS = frozenset(
    {
        ("shadow-once", "kiwoom-shadow-once"),
        ("shadow-continuous", "kiwoom-shadow-worker"),
    }
)


def _validate_shadow_image_tuple() -> None:
    execution_mode = os.environ.get("KIWOOM_EXECUTION_MODE")
    process_name = os.environ.get("KIWOOM_PROCESS_NAME")
    pair = (execution_mode, process_name)
    shadow_selected = (
        execution_mode in {item[0] for item in _SHADOW_PROCESS_PAIRS}
        or process_name in {item[1] for item in _SHADOW_PROCESS_PAIRS}
    )
    if not shadow_selected:
        return
    if pair not in _SHADOW_PROCESS_PAIRS:
        raise RuntimeError("shadow execution mode and process name must match")
    image_ref = os.environ.get("KIWOOM_IMAGE_REF")
    image_digest = os.environ.get("KIWOOM_IMAGE_DIGEST")
    if (
        image_ref is None
        or image_digest is None
        or image_ref != image_digest
        or _IMMUTABLE_IMAGE.fullmatch(image_ref) is None
    ):
        raise RuntimeError("shadow image reference and activation digest must match")


def _copy_secret(source_dir: Path, name: str) -> None:
    source = source_dir / name
    destination = STAGING_DIR / name
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise RuntimeError(f"{name} source secret is unavailable") from exc
    try:
        try:
            destination.unlink()
        except FileNotFoundError:
            pass
        destination_fd = os.open(
            destination,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | os.O_CLOEXEC,
            0o400,
        )
        try:
            with os.fdopen(source_fd, "rb", closefd=False) as source_stream:
                with os.fdopen(
                    destination_fd,
                    "wb",
                    closefd=False,
                ) as destination_stream:
                    shutil.copyfileobj(source_stream, destination_stream)
            os.fchmod(destination_fd, 0o400)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    os.chown(destination, RUNTIME_UID, RUNTIME_GID)


def _prepare_credentials() -> None:
    source_dir = Path(os.environ.get("KIWOOM_CREDENTIALS_DIR", "/run/secrets"))
    if source_dir != Path("/run/secrets"):
        raise RuntimeError("runtime secret source must be /run/secrets")
    STAGING_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STAGING_DIR, 0o700)
    for name in SECRET_NAMES:
        _copy_secret(source_dir, name)
    os.chown(STAGING_DIR, RUNTIME_UID, RUNTIME_GID)
    os.environ["KIWOOM_CREDENTIALS_DIR"] = str(STAGING_DIR)


def _drop_privileges() -> None:
    if os.geteuid() != 0:
        return
    os.setgroups([])
    os.setgid(RUNTIME_GID)
    os.setuid(RUNTIME_UID)


def _healthcheck() -> None:
    api_mode = os.environ.get("KIWOOM_API_MODE", "disabled").strip().lower()
    if api_mode in {"mock", "prod"}:
        if not STAGING_DIR.is_dir():
            raise RuntimeError(
                "hardened credential staging directory is unavailable"
            )
        os.environ["KIWOOM_CREDENTIALS_DIR"] = str(STAGING_DIR)
        _drop_privileges()
    os.execvp("python", ["python", "-m", "kiwoom_stock", "--check-config"])


def main() -> int:
    _validate_shadow_image_tuple()
    if len(sys.argv) == 2 and sys.argv[1] == "--healthcheck":
        _healthcheck()
        return 0
    api_mode = os.environ.get("KIWOOM_API_MODE", "disabled").strip().lower()
    if api_mode in {"mock", "prod"}:
        _prepare_credentials()
    _drop_privileges()
    os.execvp(sys.argv[1], sys.argv[1:])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
