#!/usr/bin/env python3
"""Interactively install a Kiwoom app/secret pair outside the repository.

The values are read with terminal echo disabled and are never printed.  The
result is a fresh, hardened directory containing exactly the two files the
runtime's strict credential provider expects.  This helper intentionally does
not call Kiwoom or any AWS API.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
from getpass import getpass


APP_KEY_FILE = "KIWOOM_APP_KEY"
SECRET_KEY_FILE = "KIWOOM_SECRET_KEY"
MAX_CREDENTIAL_BYTES = 8 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install Kiwoom credentials into an external hardened directory."
    )
    parser.add_argument(
        "--directory",
        required=True,
        type=Path,
        help="absolute repository-external target directory",
    )
    parser.add_argument(
        "--owner-uid",
        type=int,
        default=10001,
        help="numeric file owner UID (default: 10001)",
    )
    parser.add_argument(
        "--owner-gid",
        type=int,
        default=10001,
        help="numeric file owner GID (default: 10001)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="move an existing pair to a timestamped sibling before replacing it",
    )
    return parser


def _validate_identity(value: str, label: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be non-empty and must not have surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    if len(value.encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise ValueError(f"{label} exceeds the size limit")
    return value


def _repository_root() -> Path | None:
    for start in (Path(__file__).resolve(), Path.cwd()):
        candidate = start if start.is_dir() else start.parent
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return parent.resolve()
    return None


def _ensure_target_is_external(target: Path) -> Path:
    if not target.is_absolute():
        raise ValueError("--directory must be an absolute path")
    resolved = target.resolve()
    repository = _repository_root()
    if repository is not None and (resolved == repository or repository in resolved.parents):
        raise ValueError("credential directory must be outside the repository")
    if resolved == Path("/"):
        raise ValueError("credential directory must not be filesystem root")
    return resolved


def _ensure_owner_can_be_set(uid: int, gid: int) -> None:
    if uid < 0 or gid < 0:
        raise ValueError("owner UID/GID must be non-negative")
    if os.geteuid() != 0 and (uid != os.geteuid() or gid != os.getegid()):
        raise PermissionError(
            "run as root (for example through sudo) to install files for another UID/GID"
        )


def _chown(path: Path, uid: int, gid: int) -> None:
    try:
        os.chown(path, uid, gid)
    except OSError as exc:
        raise PermissionError(f"cannot set owner on {path}") from exc


def _write_secret(path: Path, value: str, uid: int, gid: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(path, flags, 0o400)
    except OSError as exc:
        raise OSError(f"cannot create credential file {path.name}") from exc
    try:
        payload = value.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fchmod(descriptor, stat.S_IRUSR)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _chown(path, uid, gid)


def _install_pair(target: Path, app_key: str, secret_key: str, uid: int, gid: int) -> None:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.is_dir():
        raise FileExistsError("credential target exists but is not a directory")
    if target.exists() and not os.access(target, os.W_OK) and os.geteuid() != 0:
        raise PermissionError("credential target exists and is not writable")

    temp_dir = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=parent))
    try:
        os.chmod(temp_dir, 0o700)
        _chown(temp_dir, uid, gid)
        _write_secret(temp_dir / APP_KEY_FILE, app_key, uid, gid)
        _write_secret(temp_dir / SECRET_KEY_FILE, secret_key, uid, gid)
        directory_fd = os.open(temp_dir, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

        if target.exists():
            backup = target.with_name(f"{target.name}.previous-{int(time.time())}")
            if backup.exists():
                raise FileExistsError("timestamped credential backup already exists")
            if not target.is_dir():
                raise FileExistsError("credential target is not a directory")
            target.rename(backup)
        temp_dir.rename(target)
        temp_dir = Path()
        directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temp_dir != Path() and temp_dir.exists():
            for child in temp_dir.iterdir():
                child.unlink()
            temp_dir.rmdir()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        target = _ensure_target_is_external(args.directory)
        _ensure_owner_can_be_set(args.owner_uid, args.owner_gid)
        if target.exists() and not args.replace:
            raise FileExistsError(
                "credential directory already exists; use --replace only for an intentional rotation"
            )
        app_key = _validate_identity(getpass("Kiwoom App Key (hidden): "), "app key")
        secret_key = _validate_identity(getpass("Kiwoom Secret Key (hidden): "), "secret key")
        _install_pair(target, app_key, secret_key, args.owner_uid, args.owner_gid)
    except (OSError, PermissionError, ValueError) as exc:
        print(f"credential setup failed: {exc}", file=sys.stderr)
        return 1
    print(f"credential pair installed at {target}; values were not printed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

