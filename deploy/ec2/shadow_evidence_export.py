#!/usr/bin/env python3
"""Read-only bounded C* telemetry evidence exporter."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess


INSTANCE_ID = "i-0e42e09d6c087ba29"
REGION = "ap-northeast-2"
MAX_LENGTH = 12_288
MAX_PAGE_LENGTH = 4_096
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
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--expected-worker-sha256", required=True)
    parser.add_argument("--expected-validator-sha256", required=True)
    parser.add_argument("--expected-shadow-document-sha256", required=True)
    args = parser.parse_args(argv)
    if (
        DATE_RE.fullmatch(args.session_date_kst) is None
        or HASH_RE.fullmatch(args.occurrence_id) is None
        or HASH_RE.fullmatch(args.release_id) is None
        or args.offset < 0
        or args.length < 1
        or args.length > MAX_PAGE_LENGTH
        or args.expected_instance_id != INSTANCE_ID
        or args.region != REGION
        or re.fullmatch(
            r"ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}",
            args.image,
        ) is None
        or re.fullmatch(r"[0-9a-f]{40}", args.source_sha) is None
        or HASH_RE.fullmatch(args.expected_worker_sha256) is None
        or HASH_RE.fullmatch(args.expected_validator_sha256) is None
        or HASH_RE.fullmatch(args.expected_shadow_document_sha256) is None
    ):
        parser.error("bounded evidence identity is invalid")
    completed = subprocess.run(
        [
            "/usr/local/sbin/kiwoom-shadow-worker",
            "--inherited-lock-fd",
            "9",
            "--desired-state",
            "telemetry-export-page",
            "--image",
            args.image,
            "--source-sha",
            args.source_sha,
            "--activation-id",
            f"shadow-session-{args.session_date_kst.replace('-', '')}",
            "--compose-shadow-sha256",
            "0" * 64,
            "--telemetry-session-date-kst",
            args.session_date_kst,
            "--telemetry-offset",
            str(args.offset),
            "--telemetry-length",
            str(args.length),
            "--expected-worker-sha256",
            args.expected_worker_sha256,
            "--expected-validator-sha256",
            args.expected_validator_sha256,
            "--expected-shadow-document-sha256",
            args.expected_shadow_document_sha256,
            "--expected-instance-id",
            args.expected_instance_id,
            "--region",
            args.region,
        ],
        capture_output=True,
        text=True,
        check=False,
        pass_fds=(9,),
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
    rendered = json.dumps(output, sort_keys=True, separators=(",", ":"))
    if len(rendered.encode("utf-8")) > MAX_LENGTH:
        print(json.dumps({"status": "FAILED", "error_type": "telemetry_export_oversize"}))
        return 1
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
