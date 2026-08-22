#!/usr/bin/env python3
"""Build deterministic C* Lambda ZIP packages without network or source mutation."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path
import stat
import zipfile


PACKAGE_FILES = {
    "submitter": (
        "deploy/shadow_cstar_contract.py",
        "deploy/shadow_cstar_submitter.py",
    ),
    "observer": (
        "deploy/shadow_cstar_contract.py",
        "deploy/shadow_cstar_observer.py",
        "deploy/shadow_cstar_submitter.py",
    ),
}


class PackageError(ValueError):
    pass


def build(root: Path, role: str, output: Path) -> str:
    if role not in PACKAGE_FILES:
        raise PackageError("invalid role")
    entries: list[tuple[str, bytes]] = []
    for relative in PACKAGE_FILES[role]:
        source = root / relative
        try:
            source_info = source.lstat()
            content = source.read_bytes()
        except OSError:
            raise PackageError(f"missing package source: {relative}") from None
        if not stat.S_ISREG(source_info.st_mode) or source_info.st_nlink != 1:
            raise PackageError(f"unsafe package source: {relative}")
        entries.append((Path(relative).name, content))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for name, content in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return sha256(output.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--role", choices=sorted(PACKAGE_FILES), required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        digest = build(arguments.root, arguments.role, arguments.output)
    except (OSError, PackageError) as error:
        parser.error(str(error))
    print(f"{arguments.role} sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
