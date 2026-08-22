#!/usr/bin/env python3
"""Build the exact, dependency-free ZIP used by the Shadow detector Lambda."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Mapping
import zipfile


PACKAGE_MEMBERS = (
    "shadow_missing_run_lambda.py",
    "shadow_missing_run_detector.py",
    "notify_shadow_status.py",
    "shadow_schedule_observation.py",
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _read_members(source_root: Path) -> Mapping[str, bytes]:
    members: dict[str, bytes] = {}
    for name in PACKAGE_MEMBERS:
        path = source_root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"detector source member unavailable: {name}")
        members[name] = path.read_bytes()
    return members


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_package(
    output: Path,
    *,
    source_root: Path | None = None,
) -> dict[str, object]:
    """Write one deterministic detector ZIP and return its release metadata."""

    root = source_root or Path(__file__).resolve().parent
    members = _read_members(root)
    requested_output = output
    if requested_output.is_symlink():
        raise ValueError("detector package output must not be a symlink")
    output = requested_output.resolve()
    source_paths = {
        (root / name).resolve() for name in PACKAGE_MEMBERS
    }
    if output in source_paths:
        raise ValueError("detector package output must not overwrite source")
    if not output.parent.is_dir():
        raise ValueError("detector package output directory is unavailable")

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            with zipfile.ZipFile(
                stream, mode="w", compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for name in PACKAGE_MEMBERS:
                    archive.writestr(_zip_info(name), members[name])
        os.replace(temporary_path, output)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    payload = output.read_bytes()
    digest = hashlib.sha256(payload).digest()
    return {
        "members": list(PACKAGE_MEMBERS),
        "bytes": len(payload),
        "sha256": digest.hex(),
        "sha256_base64": base64.b64encode(digest).decode("ascii"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = build_package(args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"shadow detector package build failed: {error}")
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
