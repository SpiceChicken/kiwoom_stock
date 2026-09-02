#!/usr/bin/env python3
"""Bootstrap the non-admin C* observer role with exact read-only access.

This is an administrator-only, one-time IAM bootstrap.  The resulting role is
for human-operated evidence and health read-back; it cannot execute commands,
change schedules, mutate the C* ledger, read broker parameters, or change IAM.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import unquote


ROLE_NAME = "kiwoom-local-observer"
POLICY_NAME = "KiwoomLocalObserver"
USER_NAME = "kiwoom-local-user"
DEFAULT_USER_POLICY_NAME = "KiwoomLocalUserAssumeRole"
ALLOWED_SIGNIN_POLICY_ARN = (
    "arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess"
)
REGION = "ap-northeast-2"
IAM_DIR = Path(__file__).resolve().parent / "iam"
TRUST = IAM_DIR / "local-observer-trust-policy.json.example"
POLICY = IAM_DIR / "local-observer-policy.json.example"
USER_POLICY = IAM_DIR / "local-user-assume-role-policy.json.example"
ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
TABLE_RE = re.compile(r"^[A-Za-z0-9_.-]{3,255}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
QUEUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8,}$")
PLACEHOLDER_RE = re.compile(r"<[^>]+>")
EXPECTED_SIGNIN_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "signin:AuthorizeOAuth2Access",
                "signin:CreateOAuth2Token",
            ],
            "Resource": "arn:aws:signin:*:*:oauth2/public-client/*",
        }
    ],
}


class BootstrapError(RuntimeError):
    """A redacted bootstrap or read-back failure."""


def _run(
    args: list[str], profile: str | None, *, missing: str | None = None,
) -> Any:
    command = ["aws"]
    if profile:
        command.extend(["--profile", profile])
    command.extend(args)
    command.extend(["--region", REGION, "--output", "json"])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        if missing is not None and missing in completed.stderr:
            return None
        raise BootstrapError(
            "AWS observer bootstrap command failed (output redacted)"
        )
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise BootstrapError(
            "AWS observer bootstrap response was invalid"
        ) from error


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _decode_policy_document(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        for candidate in (value, unquote(value)):
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
    raise BootstrapError("policy document read-back shape mismatch")


def _render(
    path: Path, replacements: dict[str, str],
) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    if PLACEHOLDER_RE.search(text):
        raise BootstrapError(
            f"unresolved IAM template placeholder: {path.name}"
        )
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise BootstrapError(f"invalid IAM template: {path.name}") from error
    if not isinstance(value, dict):
        raise BootstrapError(
            f"IAM template root is not an object: {path.name}"
        )
    return value, text


def _role(
    profile: str | None, role_name: str = ROLE_NAME,
) -> dict[str, Any] | None:
    response = _run(
        ["iam", "get-role", "--role-name", role_name], profile,
        missing="NoSuchEntity",
    )
    role = response.get("Role") if isinstance(response, dict) else None
    if response is not None and not isinstance(role, dict):
        raise BootstrapError("role read-back shape mismatch")
    if role is not None:
        role = dict(role)
        role["AssumeRolePolicyDocument"] = _decode_policy_document(
            role.get("AssumeRolePolicyDocument")
        )
    return role


def _policy(
    profile: str | None, role_name: str = ROLE_NAME,
) -> dict[str, Any] | None:
    response = _run(
        [
            "iam", "get-role-policy", "--role-name", role_name,
            "--policy-name", POLICY_NAME,
        ],
        profile,
        missing="NoSuchEntity",
    )
    if response is None:
        return None
    value = (
        response.get("PolicyDocument") if isinstance(response, dict) else None
    )
    return _decode_policy_document(value)


def _user_policy_names(profile: str | None) -> list[str]:
    response = _run(
        ["iam", "list-user-policies", "--user-name", USER_NAME], profile,
    )
    names = response.get("PolicyNames") if isinstance(response, dict) else None
    if not isinstance(names, list) or not all(
        isinstance(name, str) for name in names
    ):
        raise BootstrapError("user inline policy read-back shape mismatch")
    return names


def _user_policy(
    policy_name: str, profile: str | None,
) -> dict[str, Any] | None:
    response = _run(
        [
            "iam", "get-user-policy", "--user-name", USER_NAME,
            "--policy-name", policy_name,
        ],
        profile,
        missing="NoSuchEntity",
    )
    if response is None:
        return None
    value = (
        response.get("PolicyDocument") if isinstance(response, dict) else None
    )
    return _decode_policy_document(value)


def _validate_user_boundary(profile: str | None) -> None:
    attached = _run(
        ["iam", "list-attached-user-policies", "--user-name", USER_NAME],
        profile,
    )
    attached_policies = (
        attached.get("AttachedPolicies")
        if isinstance(attached, dict)
        else None
    )
    if not isinstance(attached_policies, list):
        raise BootstrapError("managed user policy read-back shape mismatch")
    if len(attached_policies) > 1 or (
        attached_policies
        and attached_policies[0].get("PolicyArn") != ALLOWED_SIGNIN_POLICY_ARN
    ):
        raise BootstrapError(
            "unexpected managed policy attached to local user; refusing "
            "automatic policy change"
        )
    if attached_policies:
        metadata = _run(
            ["iam", "get-policy", "--policy-arn", ALLOWED_SIGNIN_POLICY_ARN],
            profile,
        )
        policy_metadata = (
            metadata.get("Policy") if isinstance(metadata, dict) else None
        )
        version_id = (
            policy_metadata.get("DefaultVersionId")
            if isinstance(policy_metadata, dict)
            else None
        )
        if not isinstance(version_id, str):
            raise BootstrapError(
                "signin managed policy version read-back mismatch"
            )
        version = _run(
            [
                "iam", "get-policy-version", "--policy-arn",
                ALLOWED_SIGNIN_POLICY_ARN, "--version-id", version_id,
            ],
            profile,
        )
        policy_version = (
            version.get("PolicyVersion") if isinstance(version, dict) else None
        )
        document = (
            policy_version.get("Document")
            if isinstance(policy_version, dict)
            else None
        )
        if _canonical(_decode_policy_document(document)) != _canonical(
            EXPECTED_SIGNIN_POLICY
        ):
            raise BootstrapError(
                "signin managed policy contents changed; refusing automatic "
                "policy change"
            )

    groups = _run(
        ["iam", "list-groups-for-user", "--user-name", USER_NAME], profile,
    )
    group_list = groups.get("Groups") if isinstance(groups, dict) else None
    if not isinstance(group_list, list):
        raise BootstrapError("user group read-back shape mismatch")
    if group_list:
        raise BootstrapError(
            "local user belongs to IAM groups; refusing automatic "
            "policy change"
        )


def _is_nonexpansive_update(
    current: dict[str, Any], expected: dict[str, Any],
) -> bool:
    current_statements = current.get("Statement")
    expected_statements = expected.get("Statement")
    if not isinstance(current_statements, list) or not isinstance(
        expected_statements, list
    ):
        return False
    expected_canonical = {_canonical(item) for item in expected_statements}
    return all(
        _canonical(item) in expected_canonical for item in current_statements
    )


def _is_reviewed_policy_revision(
    current: dict[str, Any], expected: dict[str, Any],
) -> bool:
    if current.get("Version") != expected.get("Version"):
        return False
    current_statements = current.get("Statement")
    expected_statements = expected.get("Statement")
    if not isinstance(current_statements, list) or not isinstance(
        expected_statements, list
    ):
        return False

    def without_condition(statement: Any) -> str:
        if not isinstance(statement, dict):
            return ""
        return _canonical({
            key: value for key, value in statement.items()
            if key != "Condition"
        })

    return sorted(
        without_condition(item) for item in current_statements
    ) == sorted(
        without_condition(item) for item in expected_statements
    )


def _validate_inputs(
    account_id: str,
    table_name: str,
    bucket_name: str,
    instance_id: str,
    queue_names: tuple[str, str, str],
) -> None:
    if ACCOUNT_RE.fullmatch(account_id) is None:
        raise BootstrapError("account ID must be exactly 12 digits")
    if TABLE_RE.fullmatch(table_name) is None:
        raise BootstrapError("C* table name is invalid")
    if BUCKET_RE.fullmatch(bucket_name) is None:
        raise BootstrapError("evidence bucket name is invalid")
    if INSTANCE_RE.fullmatch(instance_id) is None:
        raise BootstrapError("EC2 instance ID is invalid")
    if any(QUEUE_RE.fullmatch(name) is None for name in queue_names):
        raise BootstrapError("DLQ name is invalid")


def _validate_policy(profile: str | None, document: str, label: str) -> None:
    result = _run(
        [
            "accessanalyzer", "validate-policy", "--policy-type",
            "IDENTITY_POLICY", "--policy-document", document,
        ],
        profile,
    )
    findings = result.get("findings") if isinstance(result, dict) else None
    if not isinstance(findings, list) or any(
        isinstance(item, dict) and item.get("findingType") == "ERROR"
        for item in findings
    ):
        raise BootstrapError(f"{label} Access Analyzer validation failed")


def bootstrap(
    *,
    account_id: str,
    table_name: str,
    bucket_name: str,
    instance_id: str,
    queue_names: tuple[str, str, str],
    profile: str | None,
    apply: bool,
    update_reviewed_policy: bool,
) -> dict[str, Any]:
    _validate_inputs(
        account_id, table_name, bucket_name, instance_id, queue_names
    )
    replacements = {
        "<AWS_ACCOUNT_ID>": account_id,
        "<AWS_REGION>": REGION,
        "<CSTAR_TABLE_NAME>": table_name,
        "<EVIDENCE_BUCKET_NAME>": bucket_name,
        "<EC2_INSTANCE_ID>": instance_id,
        "<SUBMITTER_DLQ_NAME>": queue_names[0],
        "<OBSERVER_DLQ_NAME>": queue_names[1],
        "<RECONCILIATION_DLQ_NAME>": queue_names[2],
    }
    trust, trust_text = _render(TRUST, replacements)
    policy, policy_text = _render(POLICY, replacements)
    user_policy, user_policy_text = _render(USER_POLICY, replacements)
    _validate_policy(profile, policy_text, "observer policy")
    _validate_policy(profile, user_policy_text, "local user policy")

    _validate_user_boundary(profile)
    names = _user_policy_names(profile)
    if len(names) > 1:
        raise BootstrapError(
            "multiple local user inline policies; refusing automatic policy "
            "change"
        )
    user_policy_name = names[0] if names else DEFAULT_USER_POLICY_NAME
    current_user_policy = _user_policy(user_policy_name, profile)
    if (
        current_user_policy is not None
        and _canonical(current_user_policy) != _canonical(user_policy)
        and not _is_nonexpansive_update(current_user_policy, user_policy)
    ):
        raise BootstrapError(
            "existing local user policy contains unexpected permissions"
        )

    role = _role(profile)
    current_policy = _policy(profile)
    if role is not None and _canonical(
        role.get("AssumeRolePolicyDocument")
    ) != _canonical(trust):
        raise BootstrapError(
            "existing observer role trust drift; refusing overwrite"
        )
    if (
        current_policy is not None
        and _canonical(current_policy) != _canonical(policy)
        and not update_reviewed_policy
    ):
        raise BootstrapError(
            "existing observer policy differs; use --update-reviewed-policy "
            "after review"
        )
    if (
        current_policy is not None
        and _canonical(current_policy) != _canonical(policy)
        and not _is_reviewed_policy_revision(current_policy, policy)
    ):
        raise BootstrapError(
            "existing observer policy has unexpected permissions"
        )

    if not apply:
        return {
            "mode": "check",
            "role_name": ROLE_NAME,
            "role_exists": role is not None,
            "policy_matches": (
                current_policy is not None
                and _canonical(current_policy) == _canonical(policy)
            ),
            "user_policy_name": user_policy_name,
            "user_policy_matches": (
                current_user_policy is not None
                and _canonical(current_user_policy) == _canonical(user_policy)
            ),
            "access": "read-only C* evidence and health observation",
        }

    role_created = False
    if role is None:
        _run(
            [
                "iam", "create-role", "--role-name", ROLE_NAME,
                "--assume-role-policy-document", trust_text,
                "--description", "Non-admin C* evidence and health observer",
                "--max-session-duration", "3600",
            ],
            profile,
        )
        role_created = True
        role = _role(profile)
        if role is None or _canonical(
            role.get("AssumeRolePolicyDocument")
        ) != _canonical(trust):
            raise BootstrapError("observer role create read-back mismatch")

    policy_written = False
    if current_policy is None or _canonical(current_policy) != _canonical(
        policy
    ):
        _run(
            [
                "iam", "put-role-policy", "--role-name", ROLE_NAME,
                "--policy-name", POLICY_NAME,
                "--policy-document", policy_text,
            ],
            profile,
        )
        policy_written = True

    user_policy_written = False
    if (
        current_user_policy is None
        or _canonical(current_user_policy) != _canonical(user_policy)
    ):
        _run(
            [
                "iam", "put-user-policy", "--user-name", USER_NAME,
                "--policy-name", user_policy_name,
                "--policy-document", user_policy_text,
            ],
            profile,
        )
        user_policy_written = True

    final_role = _role(profile)
    final_policy = _policy(profile)
    final_user_policy = _user_policy(user_policy_name, profile)
    if (
        final_role is None
        or _canonical(final_role.get("AssumeRolePolicyDocument"))
        != _canonical(trust)
        or final_policy is None
        or _canonical(final_policy) != _canonical(policy)
        or final_user_policy is None
        or _canonical(final_user_policy) != _canonical(user_policy)
    ):
        raise BootstrapError("final observer IAM read-back mismatch")

    arn = final_role.get("Arn")
    expected_arn = f"arn:aws:iam::{account_id}:role/{ROLE_NAME}"
    if arn != expected_arn:
        raise BootstrapError("observer role ARN mismatch")
    return {
        "mode": "apply",
        "role_name": ROLE_NAME,
        "role_arn": arn,
        "policy_name": POLICY_NAME,
        "user_name": USER_NAME,
        "user_policy_name": user_policy_name,
        "role_created": role_created,
        "policy_written": policy_written,
        "user_policy_written": user_policy_written,
        "access": "read-only C* evidence and health observation",
        "forbidden": [
            "dynamodb writes",
            "s3 writes",
            "ssm StartSession/SendCommand",
            "scheduler updates",
            "sqs sends",
            "cloudwatch metric writes",
            "parameter-store and Secrets Manager reads",
            "IAM changes",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check", action="store_true", help="read-only preflight"
    )
    action.add_argument(
        "--apply",
        action="store_true",
        help="create/update the exact observer role",
    )
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--table-name", required=True)
    parser.add_argument("--evidence-bucket-name", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--submitter-dlq-name", required=True)
    parser.add_argument("--observer-dlq-name", required=True)
    parser.add_argument("--reconciliation-dlq-name", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--update-reviewed-policy", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = bootstrap(
            account_id=args.account_id,
            table_name=args.table_name,
            bucket_name=args.evidence_bucket_name,
            instance_id=args.instance_id,
            queue_names=(
                args.submitter_dlq_name,
                args.observer_dlq_name,
                args.reconciliation_dlq_name,
            ),
            profile=args.profile,
            apply=args.apply,
            update_reviewed_policy=args.update_reviewed_policy,
        )
    except (BootstrapError, OSError) as error:
        print(f"local observer bootstrap failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
