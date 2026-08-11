#!/usr/bin/env python3
"""Create-only bootstrap for the exact GitHub shadow migration role."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence, cast


ROLE_NAME = "kiwoom-stock-github-shadow-migration"
POLICY_NAME = "KiwoomStockGithubShadowMigration"
REGION = "ap-northeast-2"
TRUST = Path("deploy/iam/github-shadow-migration-trust.json.example")
POLICY = Path("deploy/iam/github-shadow-migration-policy.json.example")


class BootstrapError(RuntimeError):
    """Redacted create-only bootstrap failure."""


def _run(args: Sequence[str], *, missing: str | None = None) -> object | None:
    completed = subprocess.run(
        ["aws", *args, "--region", REGION, "--output", "json"],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        if missing is not None and missing in completed.stderr:
            return None
        raise BootstrapError("AWS migration bootstrap command failed (output redacted)")
    try:
        return cast(object, json.loads(completed.stdout or "{}"))
    except json.JSONDecodeError as error:
        raise BootstrapError("AWS migration bootstrap response invalid") from error


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _render(path: Path, account: str) -> tuple[object, str]:
    text = path.read_text(encoding="utf-8").replace("<AWS_ACCOUNT_ID>", account)
    text = text.replace("<AWS_REGION>", REGION)
    return json.loads(text), text


def _role() -> dict[str, object] | None:
    response = _run(["iam", "get-role", "--role-name", ROLE_NAME], missing="NoSuchEntity")
    if response is None:
        return None
    role = response.get("Role") if isinstance(response, dict) else None
    if not isinstance(role, dict):
        raise BootstrapError("migration role read-back shape mismatch")
    return role


def _policy() -> object | None:
    response = _run([
        "iam", "get-role-policy", "--role-name", ROLE_NAME,
        "--policy-name", POLICY_NAME,
    ], missing="NoSuchEntity")
    if response is None:
        return None
    value = response.get("PolicyDocument") if isinstance(response, dict) else None
    if value is None:
        raise BootstrapError("migration policy read-back shape mismatch")
    return cast(object, value)


def bootstrap(account: str) -> dict[str, object]:
    if re.fullmatch(r"[0-9]{12}", account) is None:
        raise BootstrapError("account ID must be exactly 12 digits")
    trust, trust_text = _render(TRUST, account)
    policy, policy_text = _render(POLICY, account)
    validation = _run([
        "accessanalyzer", "validate-policy", "--policy-type", "IDENTITY_POLICY",
        "--policy-document", policy_text,
    ])
    findings = validation.get("findings") if isinstance(validation, dict) else None
    if not isinstance(findings, list) or any(
        isinstance(item, dict) and item.get("findingType") == "ERROR" for item in findings
    ):
        raise BootstrapError("migration policy Access Analyzer validation failed")

    role = _role()
    created_role = False
    if role is None:
        response_seen = True
        try:
            _run([
                "iam", "create-role", "--role-name", ROLE_NAME,
                "--assume-role-policy-document", trust_text,
            ])
        except BootstrapError:
            response_seen = False
        role = _role()
        if (
            role is None
            or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust)
            or not response_seen
        ):
            raise BootstrapError("migration role create ownership uncertain; refusing drift")
        created_role = True
    elif _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust):
        raise BootstrapError("existing migration role trust drift; refusing overwrite")

    current_policy = _policy()
    if current_policy is None:
        response_seen = True
        try:
            _run([
                "iam", "put-role-policy", "--role-name", ROLE_NAME,
                "--policy-name", POLICY_NAME, "--policy-document", policy_text,
            ])
        except BootstrapError:
            response_seen = False
        current_policy = _policy()
        if _canonical(current_policy) != _canonical(policy) or not response_seen:
            raise BootstrapError(
                "migration policy create ownership uncertain; no overwrite or cleanup performed"
            )
    elif _canonical(current_policy) != _canonical(policy):
        raise BootstrapError("existing migration policy drift; refusing overwrite")

    final_role = _role()
    final_policy = _policy()
    if (
        final_role is None
        or _canonical(final_role.get("AssumeRolePolicyDocument")) != _canonical(trust)
        or _canonical(final_policy) != _canonical(policy)
    ):
        raise BootstrapError("migration bootstrap final read-back mismatch")
    arn = final_role.get("Arn")
    if not isinstance(arn, str) or not arn.endswith("/" + ROLE_NAME):
        raise BootstrapError("migration role ARN mismatch")
    return {
        "status": "PASS", "role_arn": arn, "role_created": created_role,
        "github_environment_variable": "KIWOOM_AWS_SHADOW_MIGRATION_ROLE_ARN",
        "github_mutation": "not-performed-manual-readback-required",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(bootstrap(args.account_id), sort_keys=True, separators=(",", ":")))
    except (BootstrapError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
