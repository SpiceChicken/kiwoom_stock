#!/usr/bin/env python3
"""Administrator bootstrap for the local SSH/EC2 provisioner role.

This script is intentionally an administrator-only, one-time bootstrap. The
resulting role can run the reviewed EC2 clean-rebuild contract, but it cannot
modify IAM policies, delete infrastructure, use Session Manager, read
Parameter Store values, or invoke GitHub/SSM deployment commands.
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


ROLE_NAME = "kiwoom-local-provisioner"
POLICY_NAME = "KiwoomLocalProvisioner"
USER_NAME = "kiwoom-local-user"
DEFAULT_USER_POLICY_NAME = "KiwoomLocalUserAssumeRole"
ALLOWED_SIGNIN_POLICY_ARN = "arn:aws:iam::aws:policy/SignInLocalDevelopmentAccess"
REGION = "ap-northeast-2"
IAM_DIR = Path(__file__).resolve().parent / "iam"
TRUST = IAM_DIR / "local-provisioner-trust-policy.json.example"
POLICY = IAM_DIR / "local-provisioner-policy.json.example"
USER_POLICY = IAM_DIR / "local-user-assume-role-policy.json.example"
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


def _run(args: list[str], profile: str | None, *, missing: str | None = None) -> Any:
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
        raise BootstrapError("AWS bootstrap command failed (output redacted)")
    try:
        return json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise BootstrapError("AWS bootstrap response was invalid") from error


def _render(path: Path, account_id: str) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    text = text.replace("<AWS_ACCOUNT_ID>", account_id)
    text = text.replace("<AWS_REGION>", REGION)
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise BootstrapError(f"invalid template: {path}") from error
    if not isinstance(value, dict):
        raise BootstrapError(f"template root is not an object: {path}")
    return value, text


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


def _role(profile: str | None) -> dict[str, Any] | None:
    response = _run(
        ["iam", "get-role", "--role-name", ROLE_NAME], profile,
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


def _policy(profile: str | None) -> dict[str, Any] | None:
    response = _run(
        [
            "iam", "get-role-policy", "--role-name", ROLE_NAME,
            "--policy-name", POLICY_NAME,
        ],
        profile,
        missing="NoSuchEntity",
    )
    if response is None:
        return None
    value = response.get("PolicyDocument") if isinstance(response, dict) else None
    return _decode_policy_document(value)


def _user_policy_names(profile: str | None) -> list[str]:
    response = _run(
        ["iam", "list-user-policies", "--user-name", USER_NAME], profile,
    )
    names = response.get("PolicyNames") if isinstance(response, dict) else None
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise BootstrapError("user inline policy read-back shape mismatch")
    return names


def _user_policy(policy_name: str, profile: str | None) -> dict[str, Any] | None:
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
    value = response.get("PolicyDocument") if isinstance(response, dict) else None
    return _decode_policy_document(value)


def _user_has_only_expected_managed_or_group_policy(profile: str | None) -> None:
    attached = _run(
        ["iam", "list-attached-user-policies", "--user-name", USER_NAME],
        profile,
    )
    attached_policies = attached.get("AttachedPolicies") if isinstance(attached, dict) else None
    if not isinstance(attached_policies, list):
        raise BootstrapError("managed user policy read-back shape mismatch")
    if len(attached_policies) > 1 or (
        attached_policies
        and attached_policies[0].get("PolicyArn") != ALLOWED_SIGNIN_POLICY_ARN
    ):
        raise BootstrapError("unexpected managed policy attached to local user")
    if attached_policies:
        metadata = _run(
            [
                "iam", "get-policy", "--policy-arn",
                ALLOWED_SIGNIN_POLICY_ARN,
            ],
            profile,
        )
        policy_metadata = metadata.get("Policy") if isinstance(metadata, dict) else None
        version_id = policy_metadata.get("DefaultVersionId") if isinstance(policy_metadata, dict) else None
        if not isinstance(version_id, str):
            raise BootstrapError("signin managed policy version read-back mismatch")
        version = _run(
            [
                "iam", "get-policy-version", "--policy-arn",
                ALLOWED_SIGNIN_POLICY_ARN, "--version-id", version_id,
            ],
            profile,
        )
        policy_version = version.get("PolicyVersion") if isinstance(version, dict) else None
        document = policy_version.get("Document") if isinstance(policy_version, dict) else None
        if _canonical(_decode_policy_document(document)) != _canonical(EXPECTED_SIGNIN_POLICY):
            raise BootstrapError("signin managed policy contents changed; refusing automatic policy change")

    groups = _run(
        ["iam", "list-groups-for-user", "--user-name", USER_NAME], profile,
    )
    group_list = groups.get("Groups") if isinstance(groups, dict) else None
    if not isinstance(group_list, list):
        raise BootstrapError("user group read-back shape mismatch")
    if group_list:
        raise BootstrapError("local user belongs to IAM groups; refusing automatic policy change")


def _is_nonexpansive_update(
    current: dict[str, Any], expected: dict[str, Any],
) -> bool:
    current_statements = current.get("Statement")
    expected_statements = expected.get("Statement")
    if not isinstance(current_statements, list) or not isinstance(expected_statements, list):
        return False
    expected_canonical = {_canonical(item) for item in expected_statements}
    return all(_canonical(item) in expected_canonical for item in current_statements)


def _is_reviewed_policy_revision(
    current: dict[str, Any], expected: dict[str, Any],
) -> bool:
    if current.get("Version") != expected.get("Version"):
        return False
    current_statements = current.get("Statement")
    expected_statements = expected.get("Statement")
    if not isinstance(current_statements, list) or not isinstance(expected_statements, list):
        return False

    def without_condition(statement: Any) -> str:
        if not isinstance(statement, dict):
            return ""
        return _canonical({key: value for key, value in statement.items() if key != "Condition"})

    return sorted(without_condition(item) for item in current_statements) == sorted(
        without_condition(item) for item in expected_statements
    )


def bootstrap(
    account_id: str,
    profile: str | None,
    *,
    update_reviewed_policy: bool = False,
) -> dict[str, Any]:
    if re.fullmatch(r"[0-9]{12}", account_id) is None:
        raise BootstrapError("account ID must be exactly 12 digits")

    trust, trust_text = _render(TRUST, account_id)
    policy, policy_text = _render(POLICY, account_id)
    user_policy, user_policy_text = _render(USER_POLICY, account_id)
    validation = _run(
        [
            "accessanalyzer", "validate-policy", "--policy-type",
            "IDENTITY_POLICY", "--policy-document", policy_text,
        ],
        profile,
    )
    findings = validation.get("findings") if isinstance(validation, dict) else None
    if not isinstance(findings, list) or any(
        isinstance(item, dict) and item.get("findingType") == "ERROR"
        for item in findings
    ):
        raise BootstrapError("provisioner policy Access Analyzer validation failed")
    user_validation = _run(
        [
            "accessanalyzer", "validate-policy", "--policy-type",
            "IDENTITY_POLICY", "--policy-document", user_policy_text,
        ],
        profile,
    )
    user_findings = user_validation.get("findings") if isinstance(user_validation, dict) else None
    if not isinstance(user_findings, list) or any(
        isinstance(item, dict) and item.get("findingType") == "ERROR"
        for item in user_findings
    ):
        raise BootstrapError("local user policy Access Analyzer validation failed")

    _user_has_only_expected_managed_or_group_policy(profile)
    user_policy_names = _user_policy_names(profile)
    if len(user_policy_names) > 1:
        raise BootstrapError("multiple local user inline policies; refusing automatic policy change")
    user_policy_name = user_policy_names[0] if user_policy_names else DEFAULT_USER_POLICY_NAME
    current_user_policy = _user_policy(user_policy_name, profile)
    if (
        current_user_policy is not None
        and _canonical(current_user_policy) != _canonical(user_policy)
        and not _is_nonexpansive_update(current_user_policy, user_policy)
    ):
        raise BootstrapError("existing local user policy contains unexpected permissions")

    role = _role(profile)
    role_created = False
    if role is None:
        _run(
            [
                "iam", "create-role", "--role-name", ROLE_NAME,
                "--assume-role-policy-document", trust_text,
                "--description", "Bounded local EC2 provisioner; no IAM or delete authority",
                "--max-session-duration", "3600",
            ],
            profile,
        )
        role_created = True
        role = _role(profile)
        if role is None or _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust):
            raise BootstrapError("role create read-back mismatch; manual recovery required")
    elif _canonical(role.get("AssumeRolePolicyDocument")) != _canonical(trust):
        raise BootstrapError("existing provisioner role trust drift; refusing overwrite")

    current_policy = _policy(profile)
    policy_written = False
    if current_policy is None:
        _run(
            [
                "iam", "put-role-policy", "--role-name", ROLE_NAME,
                "--policy-name", POLICY_NAME,
                "--policy-document", policy_text,
            ],
            profile,
        )
        policy_written = True
    elif _canonical(current_policy) != _canonical(policy):
        if not update_reviewed_policy or not _is_reviewed_policy_revision(
            current_policy, policy
        ):
            raise BootstrapError("existing provisioner policy drift; refusing overwrite")
        _run(
            [
                "iam", "put-role-policy", "--role-name", ROLE_NAME,
                "--policy-name", POLICY_NAME,
                "--policy-document", policy_text,
            ],
            profile,
        )
        policy_written = True

    final_role = _role(profile)
    final_policy = _policy(profile)
    if (
        final_role is None
        or _canonical(final_role.get("AssumeRolePolicyDocument")) != _canonical(trust)
        or final_policy is None
        or _canonical(final_policy) != _canonical(policy)
    ):
        raise BootstrapError("final provisioner read-back mismatch")

    arn = final_role.get("Arn")
    if not isinstance(arn, str) or arn != f"arn:aws:iam::{account_id}:role/{ROLE_NAME}":
        raise BootstrapError("provisioner role ARN mismatch")

    user_policy_written = False
    if current_user_policy is None or _canonical(current_user_policy) != _canonical(user_policy):
        _run(
            [
                "iam", "put-user-policy", "--user-name", USER_NAME,
                "--policy-name", user_policy_name,
                "--policy-document", user_policy_text,
            ],
            profile,
        )
        user_policy_written = True
    final_user_policy = _user_policy(user_policy_name, profile)
    if final_user_policy is None or _canonical(final_user_policy) != _canonical(user_policy):
        raise BootstrapError("final local user policy read-back mismatch")

    return {
        "role_name": ROLE_NAME,
        "role_arn": arn,
        "policy_name": POLICY_NAME,
        "user_name": USER_NAME,
        "user_policy_name": user_policy_name,
        "role_created": role_created,
        "policy_written": policy_written,
        "user_policy_written": user_policy_written,
        "iam_mutation_scope": "create-or-reuse exact role and inline policy only",
        "forbidden": [
            "iam policy mutation",
            "ec2 delete/terminate/release",
            "ssm session/send-command",
            "parameter-store read",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--update-reviewed-policy", action="store_true")
    args = parser.parse_args()
    try:
        result = bootstrap(
            args.account_id,
            args.profile,
            update_reviewed_policy=args.update_reviewed_policy,
        )
    except (BootstrapError, OSError) as error:
        print(f"local provisioner bootstrap failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
