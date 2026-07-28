"""Validate host-side file paths before a secret-bearing Compose launch.

This command checks metadata only.  It never opens a credential file, reads a
credential value, or prints a path's contents.  The container entrypoint and
the strict application provider remain responsible for the in-container
mount and bounded value validation.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys
from typing import Iterable


APP_KEY = "KIWOOM_APP_KEY"
SECRET_KEY = "KIWOOM_SECRET_KEY"
MAX_CREDENTIAL_BYTES = 8 * 1024
MODE_ENV_NAMES = {
    "mock": ("KIWOOM_MOCK_APP_KEY_FILE", "KIWOOM_MOCK_SECRET_KEY_FILE"),
    "prod": ("KIWOOM_PROD_APP_KEY_FILE", "KIWOOM_PROD_SECRET_KEY_FILE"),
}


class SecretPathValidationError(ValueError):
    """Aggregate safe-to-display host path validation failures."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("\n".join(self.issues))


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _path_from_environment(name: str, environ: dict[str, str]) -> Path:
    raw = environ.get(name, "").strip()
    if not raw:
        raise SecretPathValidationError((f"{name} is required",))
    path = Path(raw)
    if not path.is_absolute():
        raise SecretPathValidationError((f"{name} must be an absolute path",))
    return path


def _path_has_symlink_component(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return False
    return False


def _validate_one(
    path: Path,
    *,
    variable: str,
    repository_root: Path,
) -> list[str]:
    issues: list[str] = []
    if _path_has_symlink_component(path):
        issues.append(f"{variable} must not contain symbolic links")
        return issues
    try:
        metadata = path.lstat()
    except OSError:
        return [f"{variable} must reference an existing regular file"]

    if not stat.S_ISREG(metadata.st_mode):
        issues.append(f"{variable} must reference a regular file")
    if metadata.st_nlink != 1:
        issues.append(f"{variable} must have exactly one hard link")
    if metadata.st_size > MAX_CREDENTIAL_BYTES:
        issues.append(f"{variable} exceeds the {MAX_CREDENTIAL_BYTES}-byte size limit")

    permissions = stat.S_IMODE(metadata.st_mode)
    owner_only = metadata.st_uid == os.geteuid() and permissions == 0o400
    root_group = (
        metadata.st_uid == 0
        and metadata.st_gid == os.getegid()
        and permissions == 0o440
    )
    if not (owner_only or root_group):
        issues.append(
            f"{variable} must be owned by the launcher (or root) with mode 0400 "
            "or root:effective-group 0440"
        )
    if not os.access(path, os.R_OK):
        issues.append(f"{variable} is not readable by the launcher")

    try:
        parent_metadata = path.parent.lstat()
    except OSError:
        issues.append(f"{variable} parent directory is unavailable")
    else:
        if not stat.S_ISDIR(parent_metadata.st_mode):
            issues.append(f"{variable} parent must be a directory")
        if parent_metadata.st_uid not in (0, os.geteuid()):
            issues.append(f"{variable} parent has an untrusted owner")
        if stat.S_IMODE(parent_metadata.st_mode) & 0o022:
            issues.append(f"{variable} parent must not be group/world writable")

    try:
        resolved = path.resolve(strict=True)
        root = repository_root.resolve(strict=True)
    except OSError:
        issues.append(f"{variable} path cannot be resolved")
    else:
        if resolved == root or root in resolved.parents:
            issues.append(f"{variable} must be outside the repository")
    return issues


def validate_secret_paths(
    mode: str,
    *,
    environ: dict[str, str] | None = None,
    repository_root: Path | None = None,
) -> tuple[Path, Path]:
    """Validate the two mode-specific host files and return their paths."""

    if os.name != "posix":
        raise SecretPathValidationError(("host secret path validation requires POSIX",))
    normalized_mode = mode.strip().lower()
    try:
        app_variable, secret_variable = MODE_ENV_NAMES[normalized_mode]
    except KeyError as exc:
        raise SecretPathValidationError(
            ("mode must be one of: mock, prod",)
        ) from exc

    source = dict(os.environ if environ is None else environ)
    app_path = _path_from_environment(app_variable, source)
    secret_path = _path_from_environment(secret_variable, source)
    issues = []
    if app_path == secret_path:
        issues.append("app-key and secret-key files must be distinct")
    root = repository_root or _repository_root()
    issues.extend(
        _validate_one(app_path, variable=app_variable, repository_root=root)
    )
    issues.extend(
        _validate_one(secret_path, variable=secret_variable, repository_root=root)
    )
    if issues:
        raise SecretPathValidationError(issues)
    return app_path, secret_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate host secret file metadata without reading values."
    )
    parser.add_argument("--mode", required=True, choices=sorted(MODE_ENV_NAMES))
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=None,
        help="repository boundary to reject (defaults to this checkout)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        validate_secret_paths(
            arguments.mode,
            repository_root=arguments.repository_root,
        )
    except SecretPathValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{arguments.mode} secret path metadata OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
