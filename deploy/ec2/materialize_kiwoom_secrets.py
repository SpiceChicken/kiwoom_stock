#!/usr/bin/env python3
"""Materialize the two Kiwoom SSM parameters into root-managed files.

This module deliberately keeps the AWS client at the boundary: tests and
offline callers can inject a parameter client, while the command line entry
point lazily creates a boto3 SSM client.  Secret values are never logged.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import secrets
import stat
import sys
from typing import Any, Mapping, Protocol, Sequence

try:
    from botocore.exceptions import (  # type: ignore[import-not-found]
        BotoCoreError,
        ClientError,
    )
except ImportError:  # pragma: no cover - boto3 is only required on the host
    _AWS_ERRORS: tuple[type[BaseException], ...] = ()
else:
    _AWS_ERRORS = (BotoCoreError, ClientError)


DEFAULT_APP_PARAMETER = "/kiwoom-stock/prod/oauth/app-key"
DEFAULT_SECRET_PARAMETER = "/kiwoom-stock/prod/oauth/secret-key"
DEFAULT_TARGET_DIR = Path("/run/kiwoom-stock/credentials")
DEFAULT_UID = 0
DEFAULT_GID = 0
MAX_VALUE_BYTES = 4096


class ParameterClient(Protocol):
    def get_parameters(
        self, *, Names: Sequence[str], WithDecryption: bool
    ) -> Mapping[str, Any]: ...


class MaterializationError(RuntimeError):
    """A safe, non-sensitive materialization failure."""


def _safe_value(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MaterializationError(f"{label} is empty or malformed")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise MaterializationError(f"{label} contains a control character")
    if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
        raise MaterializationError(f"{label} exceeds the size limit")
    return value


def _ensure_directory(path: Path, uid: int, gid: int) -> None:
    try:
        if not path.is_absolute() or path == Path("/"):
            raise MaterializationError(
                "target directory must be an absolute non-root path"
            )
        # Do not silently follow a symlink in any existing path component.
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            if current.is_symlink():
                raise MaterializationError(
                    "target path must not contain symlinks"
                )
        if path.exists() and (path.is_symlink() or not path.is_dir()):
            raise MaterializationError("target path is not a directory")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            raise MaterializationError("target path must not be a symlink")
        os.chmod(path, 0o700)
        os.chown(path, uid, gid)
    except MaterializationError:
        raise
    except OSError as exc:
        raise MaterializationError(
            "unable to prepare target directory"
        ) from exc


def _write_atomic(path: Path, value: str, uid: int, gid: int) -> None:
    temp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    fd = -1
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        data = value.encode("utf-8")
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
        os.fsync(fd)
        os.fchmod(fd, stat.S_IRUSR)
        os.fchown(fd, uid, gid)
        os.close(fd)
        fd = -1
        os.replace(temp, path)
        os.chmod(path, 0o400)
        os.chown(path, uid, gid)
        dir_fd = os.open(
            path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise MaterializationError(
            "unable to atomically install secret file"
        ) from exc


def _create_staging_directory(target_dir: Path) -> Path:
    staging = target_dir / f".credentials.{secrets.token_hex(8)}.tmp"
    try:
        staging.mkdir(mode=0o700)
    except OSError as exc:
        raise MaterializationError(
            "unable to create secret staging directory"
        ) from exc
    return staging


def _remove_staging_directory(staging: Path) -> None:
    try:
        for child in staging.iterdir():
            child.unlink(missing_ok=True)
        staging.rmdir()
    except OSError:
        # A failed cleanup must not expose a value in an exception or log.
        pass


def _commit_pair(staging: Path, target_dir: Path) -> None:
    names = ("app-key", "secret-key")
    backups: dict[str, Path] = {}
    installed: list[str] = []
    try:
        for name in names:
            destination = target_dir / name
            if destination.is_symlink() or (
                destination.exists() and not destination.is_file()
            ):
                raise MaterializationError(
                    "existing credential target is not a regular file"
                )
            if destination.exists():
                backup = target_dir / f".{name}.{secrets.token_hex(8)}.bak"
                os.replace(destination, backup)
                backups[name] = backup
        for name in names:
            os.replace(staging / name, target_dir / name)
            installed.append(name)
        directory_fd = os.open(
            target_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except MaterializationError:
        _restore_pair(target_dir, backups, installed)
        raise
    except OSError as exc:
        _restore_pair(target_dir, backups, installed)
        raise MaterializationError(
            "unable to commit credential pair"
        ) from exc
    else:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def _restore_pair(
    target_dir: Path,
    backups: Mapping[str, Path],
    installed: Sequence[str],
) -> None:
    for name in installed:
        try:
            (target_dir / name).unlink(missing_ok=True)
        except OSError:
            pass
    for name, backup in backups.items():
        try:
            os.replace(backup, target_dir / name)
        except OSError:
            pass


def materialize(
    client: ParameterClient,
    *,
    app_parameter: str = DEFAULT_APP_PARAMETER,
    secret_parameter: str = DEFAULT_SECRET_PARAMETER,
    target_dir: Path = DEFAULT_TARGET_DIR,
    owner_uid: int = DEFAULT_UID,
    owner_gid: int = DEFAULT_GID,
) -> tuple[str, ...]:
    """Fetch exactly two named parameters and install their files."""
    names = (app_parameter, secret_parameter)
    if (
        not app_parameter
        or not secret_parameter
        or app_parameter == secret_parameter
    ):
        raise MaterializationError(
            "parameter names must be two distinct non-empty values"
        )
    if owner_uid < 0 or owner_gid < 0:
        raise MaterializationError("owner UID/GID must be non-negative")
    try:
        response = client.get_parameters(
            Names=list(names), WithDecryption=True
        )
    except _AWS_ERRORS + (OSError, TypeError, ValueError) as exc:
        raise MaterializationError("parameter store request failed") from exc
    returned = (
        response.get("Parameters") if isinstance(response, Mapping) else None
    )
    invalid = (
        response.get("InvalidParameters")
        if isinstance(response, Mapping)
        else None
    )
    if invalid or not isinstance(returned, list) or len(returned) != 2:
        raise MaterializationError(
            "parameter store did not return exactly two parameters"
        )
    values: dict[str, str] = {}
    for item in returned:
        if not isinstance(item, Mapping) or item.get("Name") not in names:
            raise MaterializationError(
                "parameter store returned an unexpected parameter"
            )
        name = str(item["Name"])
        if name in values:
            raise MaterializationError(
                "parameter store returned duplicate parameters"
            )
        values[name] = _safe_value(item.get("Value"), "parameter")
    if set(values) != set(names):
        raise MaterializationError("parameter store response is incomplete")
    _ensure_directory(target_dir, owner_uid, owner_gid)
    staging = _create_staging_directory(target_dir)
    try:
        _write_atomic(
            staging / "app-key", values[app_parameter], owner_uid, owner_gid
        )
        _write_atomic(
            staging / "secret-key",
            values[secret_parameter],
            owner_uid,
            owner_gid,
        )
        _commit_pair(staging, target_dir)
    finally:
        _remove_staging_directory(staging)
    return names


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-parameter",
        default=os.environ.get("KIWOOM_APP_PARAMETER", DEFAULT_APP_PARAMETER),
    )
    parser.add_argument(
        "--secret-parameter",
        default=os.environ.get(
            "KIWOOM_SECRET_PARAMETER", DEFAULT_SECRET_PARAMETER
        ),
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=Path(os.environ.get("KIWOOM_SECRET_DIR", DEFAULT_TARGET_DIR)),
    )
    parser.add_argument(
        "--owner-uid",
        type=int,
        default=int(os.environ.get("KIWOOM_SECRET_UID", DEFAULT_UID)),
    )
    parser.add_argument(
        "--owner-gid",
        type=int,
        default=int(os.environ.get("KIWOOM_SECRET_GID", DEFAULT_GID)),
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    client: ParameterClient | None = None,
) -> int:
    args = _parser().parse_args(argv)
    try:
        if client is None:
            region = os.environ.get("KIWOOM_AWS_REGION", "").strip()
            if not region:
                raise MaterializationError("AWS region is required")
            import boto3  # type: ignore[import-not-found]

            client = boto3.client("ssm", region_name=region)
        names = materialize(
            client,
            app_parameter=args.app_parameter,
            secret_parameter=args.secret_parameter,
            target_dir=args.target_dir,
            owner_uid=args.owner_uid,
            owner_gid=args.owner_gid,
        )
    except _AWS_ERRORS + (
        ImportError,
        MaterializationError,
        ValueError,
        OSError,
    ) as exc:
        print(f"kiwoom secret materialization failed: {exc}", file=sys.stderr)
        return 1
    print(f"kiwoom secret materialization succeeded ({len(names)} parameters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
