"""Run a non-activating Compose configuration preflight.

The preflight validates host secret metadata first and then renders the
common Compose file with the selected mock/prod override.  It never starts a
container and never includes credential values in a subprocess argument.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping, Sequence

if __package__:
    from .validate_secret_paths import (
        SecretPathValidationError,
        validate_secret_paths,
    )
else:  # pragma: no cover - exercised by the direct script entrypoint
    from validate_secret_paths import (  # type: ignore[no-redef]
        SecretPathValidationError,
        validate_secret_paths,
    )


class ComposePreflightError(RuntimeError):
    """A safe-to-display preflight execution failure."""


def compose_command(mode: str) -> tuple[str, ...]:
    """Return the deterministic, render-only Compose command for a mode."""

    normalized = mode.strip().lower()
    if normalized not in {"mock", "prod"}:
        raise ComposePreflightError("mode must be one of: mock, prod")
    return (
        "docker",
        "compose",
        "-f",
        "compose.yaml",
        "-f",
        f"compose.{normalized}.yaml",
        "config",
        "--quiet",
    )


def run_preflight(
    mode: str,
    *,
    repository_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
    runner=subprocess.run,
) -> None:
    """Validate secrets and render merged Compose without starting services."""

    root = (repository_root or Path(__file__).resolve().parents[1]).resolve()
    source_environment = dict(os.environ if environ is None else environ)
    try:
        validate_secret_paths(
            mode,
            environ=source_environment,
            repository_root=root,
        )
    except SecretPathValidationError:
        raise
    command = compose_command(mode)
    try:
        result = runner(
            command,
            cwd=root,
            env=source_environment,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ComposePreflightError(
            "Docker Compose executable is unavailable"
        ) from exc
    if result.returncode != 0:
        raise ComposePreflightError(
            "Docker Compose configuration rendering failed"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate secret paths and render Compose config only."
    )
    parser.add_argument("--mode", required=True, choices=("mock", "prod"))
    parser.add_argument(
        "--repository-root",
        type=Path,
        default=None,
        help="repository root (defaults to this checkout)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        run_preflight(arguments.mode, repository_root=arguments.repository_root)
    except (ComposePreflightError, SecretPathValidationError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{arguments.mode} Compose preflight OK; no service was started")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
