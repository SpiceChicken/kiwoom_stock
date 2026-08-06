#!/usr/bin/env python3
"""Create-only, journaled bootstrap for the exact shadow rollout boundary."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable, cast, Sequence


ROLE_NAME = "kiwoom-stock-github-shadow-rollout"
POLICY_NAME = "KiwoomStockGithubShadowRollout"
DOCUMENT_NAME = "KiwoomStock-ShadowWorkerRollout"
REGION = "ap-northeast-2"
INSTANCE_ID = "i-02cb0a404794bd43a"
TRUST = Path("deploy/iam/github-shadow-rollout-trust.json.example")
POLICY = Path("deploy/iam/github-shadow-rollout-policy.json.example")
DOCUMENT = Path("deploy/ssm/shadow-worker-rollout-document.yaml")


class BootstrapError(RuntimeError):
    """A redacted bootstrap or cleanup failure."""


def _run(args: Sequence[str], *, missing: str | None = None) -> object | None:
    completed = subprocess.run(
        ["aws", *args, "--region", REGION, "--output", "json"],
        check=False, capture_output=True, text=True, timeout=60,
    )
    if completed.returncode != 0:
        if missing is not None and missing in completed.stderr:
            return None
        raise BootstrapError("AWS bootstrap command failed (output redacted)")
    try:
        return cast(object, json.loads(completed.stdout or "{}"))
    except json.JSONDecodeError as error:
        raise BootstrapError("AWS bootstrap response was invalid") from error


def _render(path: Path, account_id: str) -> tuple[object, str]:
    text = path.read_text(encoding="utf-8")
    text = text.replace("<AWS_ACCOUNT_ID>", account_id)
    text = text.replace("<AWS_REGION>", REGION)
    text = text.replace("<EC2_INSTANCE_ID>", INSTANCE_ID)
    return json.loads(text), text


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _role() -> dict[str, object] | None:
    response = _run(["iam", "get-role", "--role-name", ROLE_NAME], missing="NoSuchEntity")
    role = response.get("Role") if isinstance(response, dict) else None
    if response is not None and not isinstance(role, dict):
        raise BootstrapError("role read-back shape mismatch")
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
        raise BootstrapError("policy read-back shape mismatch")
    return cast(object, value)


def _document_once() -> dict[str, object] | None:
    response = _run([
        "ssm", "describe-document", "--name", DOCUMENT_NAME,
    ], missing="InvalidDocument")
    if response is None:
        return None
    value = response.get("Document") if isinstance(response, dict) else None
    if not isinstance(value, dict):
        raise BootstrapError("document read-back shape mismatch")
    return value


def _wait_document(
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, object]:
    expected = DOCUMENT.read_text(encoding="utf-8")
    last_category = "document_not_visible"
    for attempt in range(8):
        try:
            value = _document_once()
            if value is not None and all((
                value.get("Status") == "Active",
                value.get("DefaultVersion") == "1",
                value.get("LatestVersion") == "1",
            )):
                response = _run([
                    "ssm", "get-document", "--name", DOCUMENT_NAME,
                    "--document-version", "1", "--document-format", "YAML",
                ])
                content = response.get("Content") if isinstance(response, dict) else None
                if content == expected:
                    return value
                last_category = "document_content_mismatch"
            else:
                last_category = "document_not_active_v1"
        except BootstrapError:
            last_category = "document_readback_failed"
        if attempt < 7:
            sleeper(1)
    raise BootstrapError(last_category)


def _cleanup(created: dict[str, bool]) -> tuple[dict[str, str], list[str]]:
    results: dict[str, str] = {}
    operations = [
        ("document", ["ssm", "delete-document", "--name", DOCUMENT_NAME], _document_once),
        ("policy", ["iam", "delete-role-policy", "--role-name", ROLE_NAME,
                    "--policy-name", POLICY_NAME], _policy),
        ("role", ["iam", "delete-role", "--role-name", ROLE_NAME], _role),
    ]
    for name, args, probe in operations:
        if not created[name]:
            results[name] = "not-owned"
            continue
        try:
            _run(args)
        except BootstrapError:
            results[name] = "delete-error"
        try:
            if probe() is None:
                results[name] = "deleted"
            elif results.get(name) != "delete-error":
                results[name] = "still-present"
        except BootstrapError:
            results[name] = "readback-error"
    orphans = [name for name, result in results.items() if result not in {"deleted", "not-owned"}]
    return results, orphans


def bootstrap(account_id: str) -> dict[str, object]:
    if re.fullmatch(r"[0-9]{12}", account_id) is None:
        raise BootstrapError("account ID must be exactly 12 digits")
    trust, trust_text = _render(TRUST, account_id)
    policy, policy_text = _render(POLICY, account_id)
    validation = _run([
        "accessanalyzer", "validate-policy", "--policy-type", "IDENTITY_POLICY",
        "--policy-document", policy_text,
    ])
    findings = validation.get("findings") if isinstance(validation, dict) else None
    if not isinstance(findings, list) or any(
        isinstance(item, dict) and item.get("findingType") == "ERROR" for item in findings
    ):
        raise BootstrapError("rollout policy Access Analyzer validation failed")

    created = {"role": False, "policy": False, "document": False}
    policy_commit_started = False
    journal: list[dict[str, object]] = []
    try:
        role = _role()
        if role is None:
            journal.append({"resource": "role", "state": "write-uncertain"})
            write_confirmed = True
            try:
                _run(["iam", "create-role", "--role-name", ROLE_NAME,
                      "--assume-role-policy-document", trust_text])
            except BootstrapError:
                write_confirmed = False
            role = _role()
            if role is None or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust):
                journal[-1]["state"] = "orphan-or-absent"
                raise BootstrapError("role create could not be reconciled")
            if not write_confirmed:
                journal[-1]["state"] = "ownership-uncertain"
                raise BootstrapError("role create ownership is uncertain")
            created["role"] = True
            journal[-1]["state"] = "created-by-attempt"
        elif _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust):
            raise BootstrapError("existing rollout role trust drift; refusing overwrite")

        document = _document_once()
        if document is None:
            journal.append({"resource": "document", "state": "write-uncertain"})
            write_confirmed = True
            try:
                _run(["ssm", "create-document", "--name", DOCUMENT_NAME,
                      "--document-type", "Command", "--document-format", "YAML",
                      "--content", f"file://{DOCUMENT.resolve()}"])
            except BootstrapError:
                write_confirmed = False
            try:
                document = _wait_document()
            except BootstrapError:
                journal[-1]["state"] = "orphan-or-absent"
                raise
            if not write_confirmed:
                journal[-1]["state"] = "ownership-uncertain"
                raise BootstrapError("document create ownership is uncertain")
            created["document"] = True
            journal[-1]["state"] = "created-by-attempt"
        else:
            document = _wait_document()

        # Entering the policy phase is the irreversible boundary. A concurrent
        # writer may commit an exact policy before this read, so every failure
        # from the pre-read onward is fail-closed without automatic delete.
        policy_commit_started = True
        current_policy = _policy()
        if current_policy is None:
            journal.append({"resource": "policy", "state": "commit-write-uncertain"})
            write_confirmed = True
            try:
                _run(["iam", "put-role-policy", "--role-name", ROLE_NAME,
                      "--policy-name", POLICY_NAME, "--policy-document", policy_text])
            except BootstrapError:
                write_confirmed = False
            current_policy = _policy()
            if _canonical(current_policy) != _canonical(policy):
                journal[-1]["state"] = "commit-readback-absent-or-drifted"
                raise BootstrapError("policy write could not be reconciled; manual recovery required")
            if not write_confirmed:
                journal[-1]["state"] = "ownership-uncertain"
                raise BootstrapError("policy write ownership is uncertain; manual recovery required")
            journal[-1]["state"] = "commit-readback-exact"
        elif _canonical(current_policy) != _canonical(policy):
            raise BootstrapError("existing rollout inline policy drift; refusing overwrite")
        else:
            journal.append({"resource": "policy", "state": "existing-exact-no-mutation"})

        final_role = _role()
        final_policy = _policy()
        final_document = _wait_document()
        if final_role is None or _canonical(final_role.get("AssumeRolePolicyDocument")) != _canonical(trust):
            raise BootstrapError("final trust read-back mismatch")
        if _canonical(final_policy) != _canonical(policy):
            raise BootstrapError("final policy read-back mismatch")
        arn = final_role.get("Arn")
        if not isinstance(arn, str) or not arn.endswith("/" + ROLE_NAME):
            raise BootstrapError("role ARN read-back mismatch")
        return {
            "status": "PASS", "role_arn": arn, "document_name": DOCUMENT_NAME,
            "document": final_document, "journal": journal,
            "github_environment_variable": "KIWOOM_AWS_SHADOW_ROLLOUT_ROLE_ARN",
            "github_mutation": "not-performed-manual-readback-required",
        }
    except Exception as original:
        if policy_commit_started:
            cleanup = {
                "document": "commit-boundary-no-delete",
                "policy": "commit-boundary-no-delete",
                "role": "commit-boundary-no-delete",
            }
            orphans = ["role", "policy", "document"]
        else:
            cleanup, orphans = _cleanup(created)
        detail = {
            "original": str(original), "journal": journal,
            "cleanup": cleanup, "orphans": orphans,
        }
        raise BootstrapError(
            "bootstrap failed: " + json.dumps(detail, sort_keys=True, separators=(",", ":"))
        ) from original


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
