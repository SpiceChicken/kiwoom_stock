#!/usr/bin/env python3
"""Read-only bounded C* telemetry evidence exporter."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


INSTANCE_ID = "i-0e42e09d6c087ba29"
REGION = "ap-northeast-2"
MAX_LENGTH = 12_288
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-date-kst", required=True)
    parser.add_argument("--occurrence-id", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--offset", type=int, required=True)
    parser.add_argument("--length", type=int, required=True)
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--database-path", default="/var/lib/kiwoom/shadow-telemetry.db")
    args = parser.parse_args(argv)
    if (
        DATE_RE.fullmatch(args.session_date_kst) is None
        or HASH_RE.fullmatch(args.occurrence_id) is None
        or HASH_RE.fullmatch(args.release_id) is None
        or args.offset < 0
        or args.length < 1
        or args.length > MAX_LENGTH
        or args.expected_instance_id != INSTANCE_ID
        or args.region != REGION
    ):
        parser.error("bounded evidence identity is invalid")
    database = Path(args.database_path)
    try:
        info = database.lstat()
        if not database.is_file() or database.is_symlink() or info.st_nlink != 1:
            raise OSError("database identity invalid")
    except OSError as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        return 1
    completed = subprocess.run(
        [
            "/opt/kiwoom-stock/.venv/bin/python",
            "-m",
            "kiwoom_stock",
            "shadow-telemetry-export",
            "--database-path",
            str(database),
            "--activation-id",
            f"shadow-session-{args.session_date_kst.replace('-', '')}",
            "--session-date-kst",
            args.session_date_kst,
            "--offset",
            str(args.offset),
            "--length",
            str(args.length),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or len(completed.stdout.encode("utf-8")) > MAX_LENGTH:
        print(json.dumps({"status": "FAILED", "error_type": "telemetry_export_failed"}))
        return 1
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print(json.dumps({"status": "FAILED", "error_type": "telemetry_export_invalid"}))
        return 1
    output = {
        "schema_version": 1,
        "occurrence_id": args.occurrence_id,
        "release_id": args.release_id,
        "session_date_kst": args.session_date_kst,
        "payload": payload,
        "payload_sha256": hashlib.sha256(completed.stdout.encode("utf-8")).hexdigest(),
    }
    print(json.dumps(output, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
