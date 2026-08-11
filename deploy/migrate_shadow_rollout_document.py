#!/usr/bin/env python3
"""Protected, resumable migration of the exact shadow rollout SSM document."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from typing import Callable, Literal, Mapping, NoReturn, Sequence, cast

from kiwoom_stock.deployment.shadow_rollout import (
    ROLLOUT_DOCUMENT,
    RolloutError,
    _canonical_bytes,
    expected_rollout_document,
    sha256,
    strict_json,
)


REGION = "ap-northeast-2"
DOCUMENT_PATH = "deploy/ssm/shadow-worker-rollout-document.yaml"
MIGRATION_PATH = "deploy/migrate_shadow_rollout_document.py"
CHECKER_PATH = "deploy/check_shadow_ssm_contract.py"
WORKFLOW_PATH = ".github/workflows/cd-shadow-rollout-document-migration.yml"
RELEVANT_PATHS = (DOCUMENT_PATH, MIGRATION_PATH, CHECKER_PATH, WORKFLOW_PATH)
LOCK_PARAMETER = "/kiwoom-stock/shadow-rollout-document-migration/lock"
JOURNAL_PREFIX = "/kiwoom-stock/shadow-rollout-document-migration/attempts/"
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
ACCOUNT_RE = re.compile(r"[0-9]{12}")
ATTEMPT_RE = re.compile(r"[1-9][0-9]{0,19}")
VERSION_RE = re.compile(r"[1-9][0-9]*")
SESSION_RE = re.compile(r"kiwoom-shadow-migration-[1-9][0-9]{0,19}-[1-9][0-9]{0,4}")
ROLE_ARN_RE = re.compile(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})")
VERSION_NAME_RE = re.compile(r"ksr-[1-9][0-9]{0,19}-[0-9a-f]{12}")
SSM_STATUSES = frozenset({"Creating", "Active", "Updating", "Deleting", "Failed"})
UPDATE_RESPONSE_STATUSES = frozenset({"Creating", "Active", "Updating"})
UPDATE_DESCRIPTION_KEYS = frozenset({
    "Name", "CreatedDate", "DisplayName", "VersionName", "DocumentVersion",
    "Status", "StatusInformation", "DocumentFormat", "DocumentType",
    "SchemaVersion", "LatestVersion", "DefaultVersion", "Description",
    "Parameters", "PlatformTypes", "TargetType", "Tags", "AttachmentsInformation",
    "Requires", "Author", "ReviewInformation", "ApprovedVersion",
    "PendingReviewVersion", "ReviewStatus", "Category", "CategoryEnum",
    "Hash", "HashType", "Owner",
})
PAGE_SIZE = 50
PAGE_LIMIT = 20
SETTLE_LIMIT = 8
TOKEN_LIMIT = 4096
JOURNAL_LIMIT = 4096
EXECUTION_BUDGET_SECONDS = 660.0
TERMINAL_RESERVE_SECONDS = 120.0
OperationClass = Literal["primary", "terminal"]
TERMINAL_PHASES = frozenset({"complete", "failed_safe", "manual_hold"})
TERMINAL_JOURNAL_PHASES = TERMINAL_PHASES | frozenset({
    "cutover_reconciled",
})
TRANSIENT_READ_ERRORS = frozenset({
    "aws_read_failed", "aws_timeout", "aws_response_invalid",
    "execution_deadline_exhausted",
})
JOURNAL_KEYS = frozenset({
    "schema", "status", "phase", "contract", "candidate", "submits",
    "actor_last", "failure", "prestate", "final", "response_version",
})
SUBMIT_KEYS = frozenset({"update", "cutover"})
AUDIT_KEYS = frozenset({
    "status", "default", "latest", "default_status", "latest_status",
    "default_sha256", "latest_sha256", "default_exact", "latest_exact",
})
MONOTONIC_SUBMITS = frozenset({
    (0, 0), (1, 0), (1, 1),
})


class MigrationError(RuntimeError):
    """A redacted, operator-safe failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class Deadline:
    """One process-wide primary cutoff plus a reserved total deadline."""

    primary: float
    total: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def start(
        cls, clock: Callable[[], float] = time.monotonic
    ) -> Deadline:
        started = clock()
        return cls(
            started + EXECUTION_BUDGET_SECONDS - TERMINAL_RESERVE_SECONDS,
            started + EXECUTION_BUDGET_SECONDS,
            clock,
        )

    def remaining(self, operation: OperationClass) -> float:
        boundary = self.primary if operation == "primary" else self.total
        remaining = boundary - self.clock()
        if remaining <= 0:
            raise MigrationError("execution_deadline_exhausted")
        return remaining


def _valid_version(value: object) -> bool:
    return isinstance(value, str) and VERSION_RE.fullmatch(value) is not None


def _valid_journal_name(value: str) -> bool:
    suffix = value.removeprefix(JOURNAL_PREFIX)
    return value.startswith(JOURNAL_PREFIX) and ATTEMPT_RE.fullmatch(suffix) is not None


def _journal_operation_from_value(value: str) -> OperationClass:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        raise MigrationError("admin_command_not_allowed") from error
    phase = payload.get("phase") if isinstance(payload, dict) else None
    if not isinstance(phase, str):
        raise MigrationError("admin_command_not_allowed")
    return "terminal" if phase in TERMINAL_JOURNAL_PHASES else "primary"


def _classify_admin_command(
    args: Sequence[str],
    *,
    approved_content: str,
    approved_version_name: str,
    candidate_version: str | None,
) -> tuple[bool, frozenset[OperationClass]]:
    """Return write status and exact allowed operation classes for one argv."""

    if not isinstance(args, (list, tuple)) or any(type(item) is not str for item in args):
        raise MigrationError("admin_command_not_allowed")
    command = tuple(args)
    both = frozenset({cast(OperationClass, "primary"), cast(OperationClass, "terminal")})
    if command == ("sts", "get-caller-identity"):
        return False, both
    if len(command) == 4 and command[:3] == ("ssm", "get-parameter", "--name"):
        if command[3] == LOCK_PARAMETER or _valid_journal_name(command[3]):
            return False, both
    if len(command) == 9 and command[:3] == ("ssm", "put-parameter", "--name"):
        name = command[3]
        if command[4:6] != ("--type", "String") or command[6] != "--value":
            raise MigrationError("admin_command_not_allowed")
        if name == LOCK_PARAMETER and command[8] == "--no-overwrite":
            return True, frozenset({"primary"})
        if _valid_journal_name(name) and command[8] in {"--no-overwrite", "--overwrite"}:
            return True, frozenset({_journal_operation_from_value(command[7])})
    if command == ("ssm", "delete-parameter", "--name", LOCK_PARAMETER):
        return True, frozenset({"terminal"})
    if command == ("ssm", "describe-document", "--name", ROLLOUT_DOCUMENT):
        return False, both
    if (
        len(command) == 8
        and command[:4] == ("ssm", "get-document", "--name", ROLLOUT_DOCUMENT)
        and command[4] == "--document-version"
        and _valid_version(command[5])
        and command[6:] == ("--document-format", "JSON")
    ):
        return False, both
    list_base = (
        "ssm", "list-document-versions", "--name", ROLLOUT_DOCUMENT,
        "--max-results", str(PAGE_SIZE), "--no-paginate",
    )
    if command == list_base or (
        len(command) == len(list_base) + 2
        and command[:len(list_base)] == list_base
        and command[-2] == "--next-token"
        and 0 < len(command[-1]) <= TOKEN_LIMIT
    ):
        return False, both
    if command == (
        "ssm", "update-document", "--name", ROLLOUT_DOCUMENT,
        "--document-version", "$LATEST", "--document-format", "YAML",
        "--version-name", approved_version_name, "--content", approved_content,
    ):
        if command[-1].startswith("file://"):
            raise MigrationError("admin_command_not_allowed")
        return True, frozenset({"primary"})
    if (
        len(command) == 6
        and command[:4] == (
            "ssm", "update-document-default-version", "--name", ROLLOUT_DOCUMENT,
        )
        and command[4] == "--document-version"
    ):
        if candidate_version is not None and command[5] == candidate_version:
            return True, frozenset({"primary"})
    raise MigrationError("admin_command_not_allowed")


class AdminAwsCli:
    """Exact-bound OIDC adapter with explicit primary/terminal authority."""

    def __init__(
        self,
        deadline: Deadline,
        approved_content: str,
        approved_version_name: str,
        prior_version: str,
    ) -> None:
        if (
            VERSION_NAME_RE.fullmatch(approved_version_name) is None
            or not _valid_version(prior_version)
            or approved_content.startswith("file://")
        ):
            raise MigrationError("adapter_contract_invalid")
        self.deadline = deadline
        self.approved_content = approved_content
        self.approved_content_sha256 = sha256(approved_content.encode())
        self.approved_version_name = approved_version_name
        self.prior_version = prior_version
        self.candidate_version: str | None = None
        self.actor_observation: str | None = None

    def authorize_candidate(self, version: str) -> None:
        if not _valid_version(version) or version == self.prior_version:
            raise MigrationError("candidate_authority_invalid")
        if self.candidate_version not in (None, version):
            raise MigrationError("candidate_authority_changed")
        self.candidate_version = version

    def remaining(self, operation: OperationClass) -> float:
        return self.deadline.remaining(operation)

    def call(self, args: Sequence[str], *, operation: OperationClass) -> object:
        is_write, allowed = _classify_admin_command(
            args,
            approved_content=self.approved_content,
            approved_version_name=self.approved_version_name,
            candidate_version=self.candidate_version,
        )
        if operation not in allowed:
            raise MigrationError("admin_command_operation_mismatch")
        remaining = self.remaining(operation)
        env = {
            **os.environ,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_PAGER": "",
            "AWS_MAX_ATTEMPTS": "1" if is_write else "3",
            "AWS_RETRY_MODE": "standard",
        }
        try:
            completed = subprocess.run(
                ["aws", *args, "--region", REGION, "--output", "json"],
                check=False, capture_output=True, text=True,
                timeout=min(60.0, remaining), env=env,
            )
        except subprocess.TimeoutExpired as error:
            raise MigrationError(
                "aws_write_response_lost" if is_write else "aws_timeout"
            ) from error
        if completed.returncode != 0:
            stderr = completed.stderr
            if "ParameterNotFound" in stderr:
                raise MigrationError("parameter_not_found")
            if "ParameterAlreadyExists" in stderr:
                raise MigrationError("parameter_exists")
            if "DocumentVersionLimitExceeded" in stderr:
                raise MigrationError("document_version_limit")
            raise MigrationError(
                "aws_write_response_lost" if is_write else "aws_read_failed"
            )
        try:
            return cast(object, json.loads(completed.stdout or "{}"))
        except json.JSONDecodeError as error:
            raise MigrationError(
                "aws_write_response_lost" if is_write else "aws_response_invalid"
            ) from error


@dataclass(frozen=True)
class VersionContent:
    status: str
    canonical_hash: str
    exact: bool


@dataclass(frozen=True)
class DocumentState:
    status: str
    default: str
    latest: str
    default_content: VersionContent
    latest_content: VersionContent

    def audit(self) -> dict[str, object]:
        return {
            "status": self.status,
            "default": self.default,
            "latest": self.latest,
            "default_status": self.default_content.status,
            "latest_status": self.latest_content.status,
            "default_sha256": self.default_content.canonical_hash,
            "latest_sha256": self.latest_content.canonical_hash,
            "default_exact": self.default_content.exact,
            "latest_exact": self.latest_content.exact,
        }


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _parameter_value(response: object) -> str:
    parameter = response.get("Parameter") if isinstance(response, dict) else None
    value = parameter.get("Value") if isinstance(parameter, dict) else None
    if not isinstance(value, str) or len(value.encode()) > JOURNAL_LIMIT:
        raise MigrationError("parameter_value_invalid")
    return value


def _get_parameter(
    aws: AdminAwsCli, name: str, *, operation: OperationClass
) -> str | None:
    try:
        return _parameter_value(aws.call(
            ["ssm", "get-parameter", "--name", name], operation=operation
        ))
    except MigrationError as error:
        if error.category == "parameter_not_found":
            return None
        raise


def _put_parameter(
    aws: AdminAwsCli,
    name: str,
    value: str,
    *,
    overwrite: bool,
    operation: OperationClass,
) -> None:
    if len(value.encode()) > JOURNAL_LIMIT:
        raise MigrationError("journal_oversize")
    aws.call([
        "ssm", "put-parameter", "--name", name, "--type", "String",
        "--value", value, "--overwrite" if overwrite else "--no-overwrite",
    ], operation=operation)


class RemoteJournal:
    """Bounded remote attempt journal with exact read-after-write."""

    def __init__(self, aws: AdminAwsCli, name: str, payload: dict[str, object]) -> None:
        self.aws = aws
        self.name = name
        self.payload = payload

    @classmethod
    def create(
        cls, aws: AdminAwsCli, name: str, payload: dict[str, object]
    ) -> RemoteJournal:
        encoded = _canonical_json(payload)
        try:
            _put_parameter(
                aws, name, encoded, overwrite=False, operation="primary"
            )
        except MigrationError as error:
            if error.category == "parameter_exists":
                raise MigrationError("apply_requires_reconcile") from error
            if error.category != "aws_write_response_lost":
                raise
        actual = _get_parameter(aws, name, operation="primary")
        if actual != encoded:
            raise MigrationError("journal_create_unconfirmed")
        return cls(aws, name, payload)

    @classmethod
    def open(cls, aws: AdminAwsCli, name: str) -> RemoteJournal:
        encoded = _get_parameter(aws, name, operation="terminal")
        if encoded is None:
            raise MigrationError("journal_missing")
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as error:
            raise MigrationError("journal_malformed") from error
        if not isinstance(value, dict):
            raise MigrationError("journal_malformed")
        return cls(aws, name, value)

    def update(
        self, phase: str, *, operation: OperationClass, **values: object
    ) -> None:
        self.payload.update(values)
        self.payload["phase"] = phase
        encoded = _canonical_json(self.payload)
        try:
            _put_parameter(
                self.aws, self.name, encoded, overwrite=True, operation=operation
            )
        except MigrationError as error:
            if error.category != "aws_write_response_lost":
                raise
        if _get_parameter(self.aws, self.name, operation=operation) != encoded:
            raise MigrationError("journal_write_unconfirmed")


def _identity(response: object) -> tuple[str, str, str]:
    if not isinstance(response, dict) or set(response) != {"Account", "Arn", "UserId"}:
        raise MigrationError("caller_identity_invalid")
    values = tuple(response[key] for key in ("Account", "Arn", "UserId"))
    if any(not isinstance(value, str) for value in values):
        raise MigrationError("caller_identity_invalid")
    return cast(tuple[str, str, str], values)


def _attest_identity(
    response: object, account: str, role_arn: str, session_name: str
) -> str:
    actual_account, actual_arn, user_id = _identity(response)
    match = ROLE_ARN_RE.fullmatch(role_arn)
    if match is None or match.group(1) != account or actual_account != account:
        raise MigrationError("caller_account_or_role_invalid")
    role_name = match.group(2).rsplit("/", 1)[-1]
    expected = f"arn:aws:sts::{account}:assumed-role/{role_name}/{session_name}"
    if actual_arn != expected or not user_id.endswith(":" + session_name):
        raise MigrationError("caller_session_mismatch")
    return sha256(actual_arn.encode())


def _document(response: object) -> Mapping[str, object]:
    value = response.get("Document") if isinstance(response, dict) else None
    if (
        not isinstance(value, dict)
        or value.get("Name") != ROLLOUT_DOCUMENT
        or value.get("Status") not in SSM_STATUSES
        or not _valid_version(value.get("DefaultVersion"))
        or not _valid_version(value.get("LatestVersion"))
    ):
        raise MigrationError("document_description_invalid")
    return value


def _content(response: object, version: str, expected: object) -> VersionContent:
    if (
        not isinstance(response, dict)
        or response.get("Name") != ROLLOUT_DOCUMENT
        or response.get("DocumentVersion") != version
        or response.get("DocumentFormat") != "JSON"
        or response.get("Status") not in SSM_STATUSES
        or not isinstance(response.get("Content"), str)
    ):
        raise MigrationError("document_content_invalid")
    try:
        semantic = strict_json(cast(str, response["Content"]).encode())
    except (RolloutError, UnicodeError) as error:
        raise MigrationError("document_content_invalid") from error
    return VersionContent(
        cast(str, response["Status"]), sha256(_canonical_bytes(semantic)),
        semantic == expected,
    )


def _get(
    aws: AdminAwsCli,
    version: str,
    expected: object,
    *,
    operation: OperationClass,
) -> VersionContent:
    return _content(aws.call([
        "ssm", "get-document", "--name", ROLLOUT_DOCUMENT,
        "--document-version", version, "--document-format", "JSON",
    ], operation=operation), version, expected)


def _state(
    aws: AdminAwsCli, expected: object, *, operation: OperationClass
) -> DocumentState:
    document = _document(aws.call([
        "ssm", "describe-document", "--name", ROLLOUT_DOCUMENT,
    ], operation=operation))
    default = cast(str, document["DefaultVersion"])
    latest = cast(str, document["LatestVersion"])
    default_content = _get(aws, default, expected, operation=operation)
    latest_content = (
        default_content
        if latest == default
        else _get(aws, latest, expected, operation=operation)
    )
    return DocumentState(
        cast(str, document["Status"]), default, latest,
        default_content, latest_content,
    )


def _versions(
    aws: AdminAwsCli, *, operation: OperationClass
) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    token: str | None = None
    seen: set[str] = set()
    for _ in range(PAGE_LIMIT):
        args = [
            "ssm", "list-document-versions", "--name", ROLLOUT_DOCUMENT,
            "--max-results", str(PAGE_SIZE), "--no-paginate",
        ]
        if token is not None:
            args.extend(["--next-token", token])
        response = aws.call(args, operation=operation)
        if (
            not isinstance(response, dict)
            or not set(response).issubset({"DocumentVersions", "NextToken"})
            or not isinstance(response.get("DocumentVersions"), list)
            or len(response["DocumentVersions"]) > PAGE_SIZE
        ):
            raise MigrationError("document_versions_invalid")
        for item in response["DocumentVersions"]:
            if (
                not isinstance(item, dict)
                or item.get("Name") != ROLLOUT_DOCUMENT
                or item.get("Status") not in SSM_STATUSES
                or not _valid_version(item.get("DocumentVersion"))
                or type(item.get("IsDefaultVersion")) is not bool
            ):
                raise MigrationError("document_versions_invalid")
            version = cast(str, item["DocumentVersion"])
            version_name = item.get("VersionName")
            if version_name is not None and not isinstance(version_name, str):
                raise MigrationError("document_versions_invalid")
            if version in result:
                raise MigrationError("document_versions_duplicate")
            result[version] = (cast(str, item["Status"]), version_name)
        new_token = response.get("NextToken")
        if new_token in (None, ""):
            return result
        if (
            not isinstance(new_token, str)
            or len(new_token) > TOKEN_LIMIT
            or new_token in seen
        ):
            raise MigrationError("document_versions_next_token_invalid")
        seen.add(new_token)
        token = new_token
    raise MigrationError("document_versions_page_limit")


def _find_candidate(
    aws: AdminAwsCli,
    expected: object,
    version_name: str,
    *,
    operation: OperationClass,
) -> tuple[str | None, bool]:
    matches = [
        (version, status)
        for version, (status, name) in _versions(aws, operation=operation).items()
        if name == version_name
    ]
    if len(matches) > 1:
        raise MigrationError("version_name_duplicate")
    if not matches:
        return None, False
    version, status = matches[0]
    content = _get(aws, version, expected, operation=operation)
    if not content.exact:
        raise MigrationError("candidate_content_mismatch")
    if status == "Active" and content.status == "Active":
        return version, True
    if status in {"Creating", "Updating"} or content.status in {"Creating", "Updating"}:
        return version, False
    raise MigrationError("candidate_not_active")


def _verify_candidate_binding(
    aws: AdminAwsCli,
    candidate: str,
    version_name: str,
    *,
    operation: OperationClass,
) -> None:
    if _versions(aws, operation=operation).get(candidate) != (
        "Active", version_name
    ):
        raise MigrationError("candidate_version_binding_drift")


def _settle_candidate(
    aws: AdminAwsCli,
    expected: object,
    version_name: str,
    sleeper: Callable[[float], None],
) -> str | None:
    for attempt in range(SETTLE_LIMIT):
        candidate, active = _find_candidate(
            aws, expected, version_name, operation="terminal"
        )
        if candidate is not None and active:
            return candidate
        if attempt + 1 < SETTLE_LIMIT:
            sleeper(min(1.0, aws.remaining("terminal")))
    return None


def _put_lock(aws: AdminAwsCli, encoded: str) -> None:
    actual = _get_parameter(aws, LOCK_PARAMETER, operation="terminal")
    if actual is not None:
        if actual != encoded:
            raise MigrationError("lease_conflict")
        return
    try:
        _put_parameter(
            aws, LOCK_PARAMETER, encoded, overwrite=False, operation="primary"
        )
    except MigrationError as error:
        if error.category not in {"parameter_exists", "aws_write_response_lost"}:
            raise
    actual = _get_parameter(aws, LOCK_PARAMETER, operation="terminal")
    if actual != encoded:
        raise MigrationError("lease_conflict")


def _release_lock(aws: AdminAwsCli, encoded: str) -> None:
    if _get_parameter(aws, LOCK_PARAMETER, operation="terminal") != encoded:
        raise MigrationError("lease_owner_changed")
    try:
        aws.call(
            ["ssm", "delete-parameter", "--name", LOCK_PARAMETER],
            operation="terminal",
        )
    except MigrationError as error:
        if error.category != "aws_write_response_lost":
            raise
    if _get_parameter(aws, LOCK_PARAMETER, operation="terminal") is not None:
        raise MigrationError("lease_release_unconfirmed")


def _git(
    args: Sequence[str],
    deadline: Deadline,
    *,
    text: bool = False,
) -> bytes | str:
    remaining = deadline.remaining("primary")
    try:
        completed = subprocess.run(
            ["git", *args], check=False, capture_output=True, text=text,
            timeout=min(15.0, remaining),
        )
    except subprocess.TimeoutExpired as error:
        raise MigrationError("source_provenance_timeout") from error
    if completed.returncode != 0:
        raise MigrationError("source_provenance_invalid")
    return cast(bytes | str, completed.stdout)


def approved_sources(
    source_sha: str, deadline: Deadline
) -> tuple[bytes, dict[str, str]]:
    """Read exact committed blobs inside the process-wide primary deadline."""

    if SOURCE_RE.fullmatch(source_sha) is None:
        raise MigrationError("source_sha_invalid")
    if cast(str, _git(["rev-parse", "HEAD"], deadline, text=True)).strip() != source_sha:
        raise MigrationError("source_head_mismatch")
    if cast(str, _git(
        ["status", "--porcelain", "--untracked-files=all"], deadline, text=True
    )):
        raise MigrationError("source_checkout_dirty")
    blobs: dict[str, bytes] = {}
    for path in RELEVANT_PATHS:
        value = cast(bytes, _git(["show", f"{source_sha}:{path}"], deadline))
        try:
            value.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise MigrationError("source_utf8_invalid") from error
        blobs[path] = value
    return blobs[DOCUMENT_PATH], {
        path: sha256(value) for path, value in blobs.items()
    }


def _contract(
    account: str,
    role_arn: str,
    source_sha: str,
    attempt: str,
    prior_version: str,
    prior_hash: str,
    target_hash: str,
    provenance: Mapping[str, str],
) -> dict[str, object]:
    """Stable equality key; never include run/session-scoped identity."""

    return {
        "schema": 3,
        "account": account,
        "role_arn_sha256": sha256(role_arn.encode()),
        "source_sha": source_sha,
        "attempt": attempt,
        "prior_version": prior_version,
        "prior_sha256": prior_hash,
        "target_sha256": target_hash,
        "version_name": f"ksr-{attempt}-{source_sha[:12]}",
        "provenance": dict(provenance),
    }


def _new_journal(
    contract: Mapping[str, object], actor: str
) -> dict[str, object]:
    return {
        "schema": 3,
        "status": "IN_PROGRESS",
        "phase": "attempt_created",
        "contract": dict(contract),
        "candidate": None,
        "submits": {"update": 0, "cutover": 0},
        "actor_last": actor,
        "failure": None,
        "prestate": None,
        "final": None,
        "response_version": None,
    }


def _valid_audit(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != AUDIT_KEYS:
        return False
    return (
        value.get("status") in SSM_STATUSES
        and _valid_version(value.get("default"))
        and _valid_version(value.get("latest"))
        and value.get("default_status") in SSM_STATUSES
        and value.get("latest_status") in SSM_STATUSES
        and isinstance(value.get("default_sha256"), str)
        and HASH_RE.fullmatch(cast(str, value["default_sha256"])) is not None
        and isinstance(value.get("latest_sha256"), str)
        and HASH_RE.fullmatch(cast(str, value["latest_sha256"])) is not None
        and type(value.get("default_exact")) is bool
        and type(value.get("latest_exact")) is bool
    )


def _audit_matches_prior(
    value: object, contract: Mapping[str, object]
) -> bool:
    if not _valid_audit(value):
        return False
    audit = cast(dict[str, object], value)
    prior = contract.get("prior_version")
    prior_hash = contract.get("prior_sha256")
    target_hash = contract.get("target_sha256")
    expected_exact = prior_hash == target_hash
    return (
        audit["status"] == "Active"
        and audit["default"] == prior
        and audit["latest"] == prior
        and audit["default_status"] == "Active"
        and audit["latest_status"] == "Active"
        and audit["default_sha256"] == prior_hash
        and audit["latest_sha256"] == prior_hash
        and audit["default_exact"] is expected_exact
        and audit["latest_exact"] is expected_exact
    )


def _audit_matches_candidate(
    value: object, contract: Mapping[str, object], candidate: object
) -> bool:
    if not _valid_audit(value) or not _valid_version(candidate):
        return False
    audit = cast(dict[str, object], value)
    target_hash = contract.get("target_sha256")
    return (
        audit["status"] == "Active"
        and audit["default"] == candidate
        and audit["latest"] == candidate
        and audit["default_status"] == "Active"
        and audit["latest_status"] == "Active"
        and audit["default_sha256"] == target_hash
        and audit["latest_sha256"] == target_hash
        and audit["default_exact"] is True
        and audit["latest_exact"] is True
    )


def _valid_failure(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and re.fullmatch(r"[a-z0-9_:]+", value) is not None
    )


def _validate_journal(payload: Mapping[str, object]) -> str | None:
    if set(payload) != JOURNAL_KEYS or payload.get("schema") != 3:
        return "journal_schema_invalid"
    phase = payload.get("phase")
    status = payload.get("status")
    candidate = payload.get("candidate")
    submits = payload.get("submits")
    actor = payload.get("actor_last")
    contract = payload.get("contract")
    if (
        not isinstance(phase, str)
        or phase not in {
            "attempt_created", "lease_acquired", "prestate_verified",
            "update_submitting", "candidate_verified", "cutover_submitting",
            "cutover_reconciled", "complete", "failed_safe", "manual_hold",
        }
        or not isinstance(submits, dict)
        or set(submits) != SUBMIT_KEYS
        or any(type(submits[key]) is not int or submits[key] not in {0, 1}
               for key in SUBMIT_KEYS)
        or not isinstance(actor, str)
        or HASH_RE.fullmatch(actor) is None
        or not isinstance(contract, dict)
    ):
        return "journal_schema_invalid"
    expected_status = (
        "PASS" if phase == "complete"
        else "FAIL" if phase == "failed_safe"
        else "MANUAL_HOLD" if phase == "manual_hold"
        else "IN_PROGRESS"
    )
    if status != expected_status:
        return "journal_status_invalid"
    counts = tuple(submits[key] for key in ("update", "cutover"))
    allowed_counts = {
        "attempt_created": {(0, 0)},
        "lease_acquired": {(0, 0)},
        "prestate_verified": {(0, 0)},
        "update_submitting": {(1, 0)},
        "candidate_verified": {(1, 0)},
        "cutover_submitting": {(1, 1)},
        "cutover_reconciled": {(1, 1)},
        "complete": {(0, 0), (1, 1)},
        "failed_safe": {(0, 0), (1, 0)},
        "manual_hold": set(MONOTONIC_SUBMITS),
    }
    if counts not in allowed_counts[phase]:
        return "journal_submit_invariant_invalid"
    if candidate is not None and not _valid_version(candidate):
        return "journal_candidate_invariant_invalid"
    response = payload.get("response_version")
    if response is not None and not _valid_version(response):
        return "journal_response_version_invalid"
    for key in ("prestate", "final"):
        if payload.get(key) is not None and not _valid_audit(payload.get(key)):
            return "journal_evidence_invalid"

    prestate = payload.get("prestate")
    final = payload.get("final")
    failure = payload.get("failure")
    empty_evidence = (
        candidate is None and response is None and failure is None
        and prestate is None and final is None
    )
    if phase in {"attempt_created", "lease_acquired"}:
        return None if empty_evidence else "journal_phase_evidence_invalid"
    if phase == "prestate_verified":
        return None if (
            candidate is None and response is None and failure is None
            and _audit_matches_prior(prestate, contract)
            and final is None
        ) else "journal_phase_evidence_invalid"
    if phase == "update_submitting":
        return None if (
            candidate is None and failure is None
            and _audit_matches_prior(prestate, contract)
            and final is None
        ) else "journal_phase_evidence_invalid"
    if phase in {"candidate_verified", "cutover_submitting"}:
        return None if (
            _valid_version(candidate)
            and (response is None or response == candidate)
            and failure is None
            and _audit_matches_prior(prestate, contract)
            and final is None
        ) else "journal_phase_evidence_invalid"
    if phase == "cutover_reconciled":
        return None if (
            _valid_version(candidate)
            and (response is None or response == candidate)
            and failure is None
            and _audit_matches_prior(prestate, contract)
            and _audit_matches_candidate(final, contract, candidate)
        ) else "journal_phase_evidence_invalid"
    if phase == "complete":
        no_op = (
            counts == (0, 0)
            and candidate == contract.get("prior_version")
            and contract.get("prior_sha256") == contract.get("target_sha256")
        )
        migrated = counts == (1, 1) and _valid_version(candidate)
        return None if (
            (no_op or migrated)
            and (response is None or response == candidate)
            and failure is None
            and _audit_matches_prior(prestate, contract)
            and _audit_matches_candidate(final, contract, candidate)
        ) else "journal_phase_evidence_invalid"
    if phase == "failed_safe":
        valid_prestate = (
            prestate is None if counts == (0, 0)
            else _audit_matches_prior(prestate, contract)
        )
        return None if (
            candidate is None and response is None and _valid_failure(failure)
            and valid_prestate and final is None
        ) else "journal_phase_evidence_invalid"
    if phase == "manual_hold":
        return None if _valid_failure(failure) else "journal_phase_evidence_invalid"
    return "journal_phase_evidence_invalid"


def _replace_with_manual_hold(
    journal: RemoteJournal,
    contract: Mapping[str, object],
    actor: str,
    category: str,
) -> NoReturn:
    submits = journal.payload.get("submits")
    raw_counts = None
    if (
        isinstance(submits, dict)
        and set(submits) == SUBMIT_KEYS
        and all(type(submits[key]) is int and submits[key] in {0, 1}
                for key in SUBMIT_KEYS)
    ):
        raw_counts = tuple(
            submits[key] for key in ("update", "cutover")
        )
    counts = raw_counts if raw_counts in MONOTONIC_SUBMITS else (1, 1)
    safe_submits = dict(zip(("update", "cutover"), counts))
    journal.payload = _new_journal(contract, actor)
    journal.payload["submits"] = safe_submits
    journal.update(
        "manual_hold",
        operation="terminal",
        status="MANUAL_HOLD",
        actor_last=actor,
        failure=category,
    )
    raise MigrationError(category)


def _manual_hold(
    journal: RemoteJournal, actor: str, category: str
) -> NoReturn:
    journal.update(
        "manual_hold",
        operation="terminal",
        status="MANUAL_HOLD",
        actor_last=actor,
        failure=category,
    )
    raise MigrationError(category)


def _verify_prior(state: DocumentState, version: str, digest: str) -> None:
    if not (
        state.status == "Active"
        and state.default == version
        and state.latest == version
        and state.default_content.status == "Active"
        and state.default_content.canonical_hash == digest
    ):
        raise MigrationError("unknown_prestate")


def _verify_candidate_state(
    state: DocumentState,
    prior: str,
    prior_hash: str,
    candidate: str,
    target_hash: str,
) -> None:
    if not (
        state.status == "Active"
        and state.default == prior
        and state.default_content.status == "Active"
        and state.default_content.canonical_hash == prior_hash
        and state.latest == candidate
        and state.latest_content.status == "Active"
        and state.latest_content.exact
        and state.latest_content.canonical_hash == target_hash
    ):
        raise MigrationError("candidate_state_drift")


def _verify_cutover_state(
    state: DocumentState, candidate: str, target_hash: str
) -> None:
    if not (
        state.status == "Active"
        and state.default == candidate
        and state.latest == candidate
        and state.default_content.status == "Active"
        and state.latest_content.status == "Active"
        and state.default_content.exact
        and state.latest_content.exact
        and state.default_content.canonical_hash == target_hash
        and state.latest_content.canonical_hash == target_hash
    ):
        raise MigrationError("cutover_state_drift")


def _require_candidate_absent(
    aws: AdminAwsCli, version_name: str, *, operation: OperationClass
) -> None:
    if any(
        name == version_name
        for _status, name in _versions(aws, operation=operation).values()
    ):
        raise MigrationError("failed_safe_candidate_present")


def _release_failed_safe(
    journal: RemoteJournal,
    aws: AdminAwsCli,
    actor: str,
    version_name: str,
    lock_value: str,
    phase_hook: Callable[[str], None],
) -> dict[str, object]:
    """Release only after an authoritative same-attempt absence proof."""

    try:
        _require_candidate_absent(aws, version_name, operation="terminal")
    except MigrationError as error:
        if error.category in TRANSIENT_READ_ERRORS:
            raise
        _manual_hold(journal, actor, error.category)
    journal.update(
        "failed_safe",
        operation="terminal",
        status="FAIL",
        actor_last=actor,
    )
    phase_hook("failed_safe_release")
    _release_lock(aws, lock_value)
    return journal.payload


def _update_response_version(
    response: object, version_name: str
) -> str:
    if not isinstance(response, dict) or set(response) != {"DocumentDescription"}:
        raise MigrationError("update_response_invalid")
    description = response["DocumentDescription"]
    if (
        not isinstance(description, dict)
        or not set(description).issubset(UPDATE_DESCRIPTION_KEYS)
        or not {"Name", "VersionName", "DocumentVersion", "Status"} <= set(description)
        or description.get("Name") != ROLLOUT_DOCUMENT
        or description.get("VersionName") != version_name
        or description.get("Status") not in UPDATE_RESPONSE_STATUSES
        or not _valid_version(description.get("DocumentVersion"))
    ):
        raise MigrationError("update_response_invalid")
    return cast(str, description["DocumentVersion"])


def _default_submit(
    aws: AdminAwsCli, version: str, *, operation: OperationClass
) -> None:
    aws.call([
        "ssm", "update-document-default-version", "--name", ROLLOUT_DOCUMENT,
        "--document-version", version,
    ], operation=operation)


def _authoritative_or_hold(
    journal: RemoteJournal,
    actor: str,
    category: str,
    reader: Callable[[], DocumentState],
) -> DocumentState:
    try:
        return reader()
    except MigrationError as error:
        if error.category in TRANSIENT_READ_ERRORS:
            raise
        _manual_hold(journal, actor, f"{category}:{error.category}")
    raise AssertionError("unreachable")


def execute(
    aws: AdminAwsCli,
    *,
    mode: str,
    account: str,
    role_arn: str,
    session_name: str,
    source_sha: str,
    source_blob: bytes,
    provenance: Mapping[str, str],
    attempt: str,
    prior_version: str,
    prior_hash: str,
    sleeper: Callable[[float], None] = time.sleep,
    phase_hook: Callable[[str], None] = lambda _phase: None,
) -> dict[str, object]:
    """Apply or reconcile one stable attempt with bounded total transitions."""

    if mode not in {"apply", "reconcile"}:
        raise MigrationError("mode_invalid")
    if (
        ACCOUNT_RE.fullmatch(account) is None
        or ATTEMPT_RE.fullmatch(attempt) is None
        or SESSION_RE.fullmatch(session_name) is None
        or not _valid_version(prior_version)
        or HASH_RE.fullmatch(prior_hash) is None
        or SOURCE_RE.fullmatch(source_sha) is None
    ):
        raise MigrationError("identity_input_invalid")
    try:
        source_text = source_blob.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise MigrationError("source_utf8_invalid") from error
    expected = expected_rollout_document(source_blob)
    target_hash = sha256(_canonical_bytes(expected))
    version_name = f"ksr-{attempt}-{source_sha[:12]}"
    if isinstance(aws, AdminAwsCli) and (
        aws.approved_content != source_text
        or aws.approved_content_sha256 != sha256(source_blob)
        or aws.approved_version_name != version_name
        or aws.prior_version != prior_version
    ):
        raise MigrationError("adapter_contract_mismatch")
    actor = _attest_identity(
        aws.call(["sts", "get-caller-identity"], operation="terminal"),
        account,
        role_arn,
        session_name,
    )
    if isinstance(aws, AdminAwsCli):
        aws.actor_observation = actor
    contract = _contract(
        account, role_arn, source_sha, attempt, prior_version, prior_hash,
        target_hash, provenance,
    )
    lock_value = _canonical_json({"contract": contract})
    journal_name = JOURNAL_PREFIX + attempt
    if mode == "apply":
        journal = RemoteJournal.create(
            aws, journal_name, _new_journal(contract, actor)
        )
        phase_hook("attempt_created")
    else:
        journal = RemoteJournal.open(aws, journal_name)
    if journal.payload.get("contract") != contract:
        raise MigrationError("journal_contract_mismatch")

    before_lock = _canonical_json(journal.payload)
    _put_lock(aws, lock_value)
    journal = RemoteJournal.open(aws, journal_name)
    if journal.payload.get("contract") != contract:
        raise MigrationError("journal_contract_changed_after_lock")
    if _canonical_json(journal.payload) != before_lock:
        _replace_with_manual_hold(
            journal, contract, actor, "journal_changed_during_lock"
        )

    invalid = _validate_journal(journal.payload)
    if invalid is not None:
        _replace_with_manual_hold(journal, contract, actor, invalid)

    phase = cast(str, journal.payload["phase"])
    if phase == "attempt_created":
        journal.update(
            "lease_acquired", operation="primary", actor_last=actor
        )
        phase_hook("lease_acquired")
        phase = "lease_acquired"
    candidate_value = journal.payload["candidate"]
    candidate = cast(str | None, candidate_value)
    if candidate is not None and candidate != prior_version:
        aws.authorize_candidate(candidate)
    if phase in TERMINAL_PHASES:
        if phase == "manual_hold":
            raise MigrationError("manual_hold")
        if phase == "complete":
            if candidate is None:
                _manual_hold(journal, actor, "complete_candidate_missing")
            state = _authoritative_or_hold(
                journal, actor, "complete_state",
                lambda: _state(aws, expected, operation="terminal"),
            )
            try:
                _verify_cutover_state(state, candidate, target_hash)
                submits = cast(dict[str, int], journal.payload["submits"])
                if submits["update"] == 1:
                    _verify_candidate_binding(
                        aws, candidate, version_name, operation="terminal"
                    )
            except MigrationError as error:
                if error.category in TRANSIENT_READ_ERRORS:
                    raise
                _manual_hold(journal, actor, error.category)
            if journal.payload.get("final") != state.audit():
                _manual_hold(journal, actor, "complete_evidence_drift")
        elif phase == "failed_safe":
            return _release_failed_safe(
                journal, aws, actor, version_name, lock_value, phase_hook
            )
        journal.update(
            phase,
            operation="terminal",
            actor_last=actor,
        )
        phase_hook(f"{phase}_release")
        _release_lock(aws, lock_value)
        return journal.payload

    if phase == "lease_acquired":
        try:
            state = _state(aws, expected, operation="primary")
            _verify_prior(state, prior_version, prior_hash)
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            journal.update(
                "failed_safe",
                operation="terminal",
                status="FAIL",
                actor_last=actor,
                failure=error.category,
            )
            phase_hook("failed_safe")
            return _release_failed_safe(
                journal, aws, actor, version_name, lock_value, phase_hook
            )
        journal.update(
            "prestate_verified",
            operation="primary",
            actor_last=actor,
            prestate=state.audit(),
        )
        phase_hook("prestate_verified")
        phase = "prestate_verified"

    if phase == "prestate_verified":
        state = _authoritative_or_hold(
            journal, actor, "prestate_read",
            lambda: _state(aws, expected, operation="terminal"),
        )
        try:
            _verify_prior(state, prior_version, prior_hash)
        except MigrationError as error:
            _manual_hold(journal, actor, error.category)
        if prior_hash == target_hash:
            journal.update(
                "complete",
                operation="terminal",
                status="PASS",
                candidate=prior_version,
                actor_last=actor,
                final=state.audit(),
            )
            phase_hook("complete")
            _release_lock(aws, lock_value)
            return journal.payload
        try:
            existing, _active = _find_candidate(
                aws, expected, version_name, operation="terminal"
            )
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            _manual_hold(journal, actor, f"version_name_precheck:{error.category}")
        if existing is not None:
            _manual_hold(journal, actor, "version_name_preexists")
        submits = cast(dict[str, int], journal.payload["submits"])
        submits["update"] = 1
        try:
            journal.update(
                "update_submitting",
                operation="primary",
                actor_last=actor,
            )
        except MigrationError as error:
            if error.category == "execution_deadline_exhausted":
                _manual_hold(journal, actor, "primary_deadline_exhausted")
            raise
        phase_hook("update_submitting")
        try:
            response = aws.call([
                "ssm", "update-document", "--name", ROLLOUT_DOCUMENT,
                "--document-version", "$LATEST", "--document-format", "YAML",
                "--version-name", version_name, "--content", source_text,
            ], operation="primary")
            response_version = _update_response_version(response, version_name)
            journal.update(
                "update_submitting",
                operation="primary",
                actor_last=actor,
                response_version=response_version,
            )
        except MigrationError as error:
            if error.category == "document_version_limit":
                journal.update(
                    "failed_safe",
                    operation="terminal",
                    status="FAIL",
                    actor_last=actor,
                    failure=error.category,
                )
                phase_hook("failed_safe")
                return _release_failed_safe(
                    journal, aws, actor, version_name, lock_value, phase_hook
                )
            if error.category not in {
                "aws_write_response_lost", "update_response_invalid",
            }:
                raise
        phase_hook("update_submitted")
        phase = "update_submitting"

    if phase == "update_submitting":
        try:
            candidate = _settle_candidate(aws, expected, version_name, sleeper)
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            _manual_hold(journal, actor, f"candidate_reconcile:{error.category}")
        if candidate is None:
            _manual_hold(journal, actor, "update_uncertain")
        journal_response_version = journal.payload.get("response_version")
        if (
            journal_response_version is not None
            and journal_response_version != candidate
        ):
            _manual_hold(journal, actor, "update_response_version_mismatch")
        aws.authorize_candidate(candidate)
        try:
            journal.update(
                "candidate_verified",
                operation="primary",
                candidate=candidate,
                actor_last=actor,
            )
        except MigrationError as error:
            if error.category == "execution_deadline_exhausted":
                _manual_hold(journal, actor, "primary_deadline_exhausted")
            raise
        phase_hook("candidate_verified")
        phase = "candidate_verified"

    if candidate is None:
        _manual_hold(journal, actor, "candidate_missing")
    aws.authorize_candidate(candidate)

    if phase == "candidate_verified":
        try:
            _verify_candidate_binding(
                aws, candidate, version_name, operation="terminal"
            )
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            _manual_hold(journal, actor, error.category)
        state = _authoritative_or_hold(
            journal, actor, "candidate_state",
            lambda: _state(aws, expected, operation="terminal"),
        )
        try:
            _verify_candidate_state(
                state, prior_version, prior_hash, candidate, target_hash
            )
        except MigrationError as error:
            _manual_hold(journal, actor, error.category)
        submits = cast(dict[str, int], journal.payload["submits"])
        submits["cutover"] = 1
        try:
            journal.update(
                "cutover_submitting",
                operation="primary",
                actor_last=actor,
            )
        except MigrationError as error:
            if error.category == "execution_deadline_exhausted":
                _manual_hold(journal, actor, "primary_deadline_exhausted")
            raise
        phase_hook("cutover_submitting")
        try:
            _default_submit(aws, candidate, operation="primary")
        except MigrationError as error:
            if error.category != "aws_write_response_lost":
                raise
        phase = "cutover_submitting"

    if phase == "cutover_submitting":
        try:
            _verify_candidate_binding(
                aws, candidate, version_name, operation="terminal"
            )
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            _manual_hold(journal, actor, error.category)
        state = _authoritative_or_hold(
            journal, actor, "cutover_state",
            lambda: _state(aws, expected, operation="terminal"),
        )
        if state.default == prior_version:
            _manual_hold(journal, actor, "cutover_uncertain_no_cas")
        try:
            _verify_cutover_state(state, candidate, target_hash)
        except MigrationError:
            _manual_hold(journal, actor, "cutover_remote_drift")
        journal.update(
            "cutover_reconciled",
            operation="terminal",
            actor_last=actor,
            final=state.audit(),
        )
        phase_hook("cutover_reconciled")
        phase = "cutover_reconciled"

    if phase == "cutover_reconciled":
        try:
            _verify_candidate_binding(
                aws, candidate, version_name, operation="terminal"
            )
        except MigrationError as error:
            if error.category in TRANSIENT_READ_ERRORS:
                raise
            _manual_hold(journal, actor, error.category)
        state = _authoritative_or_hold(
            journal, actor, "final_state",
            lambda: _state(aws, expected, operation="terminal"),
        )
        try:
            _verify_cutover_state(state, candidate, target_hash)
        except MigrationError as error:
            _manual_hold(journal, actor, error.category)
        journal.update(
            "complete",
            operation="terminal",
            status="PASS",
            actor_last=actor,
            final=state.audit(),
        )
        phase_hook("complete")
        _release_lock(aws, lock_value)
        return journal.payload

    _manual_hold(journal, actor, "journal_phase_invalid")
    raise AssertionError("unreachable")


def _write_local_summary(path: Path, value: Mapping[str, object]) -> None:
    encoded = (_canonical_json(value) + "\n").encode()
    if len(encoded) > 16384 or path.is_symlink():
        raise MigrationError("audit_path_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_NOFOLLOW
    if path.exists():
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise MigrationError("audit_path_invalid")
        flags |= os.O_TRUNC
    else:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


def main(argv: Sequence[str] | None = None) -> int:
    deadline = Deadline.start()
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("apply", "reconcile"), required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--expected-role-arn", required=True)
    parser.add_argument("--expected-session-name", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--migration-attempt-id", required=True)
    parser.add_argument("--expected-current-version", required=True)
    parser.add_argument("--expected-current-canonical-sha256", required=True)
    parser.add_argument("--audit-path", type=Path, required=True)
    args = parser.parse_args(argv)
    result: dict[str, object] = {"status": "FAIL", "category": "not_started"}
    actor_observation: str | None = None
    try:
        source, provenance = approved_sources(args.source_sha, deadline)
        source_text = source.decode("utf-8", errors="strict")
        version_name = (
            f"ksr-{args.migration_attempt_id}-{args.source_sha[:12]}"
        )
        aws = AdminAwsCli(
            deadline, source_text, version_name, args.expected_current_version
        )
        payload = execute(
            aws,
            mode=args.mode,
            account=args.account_id,
            role_arn=args.expected_role_arn,
            session_name=args.expected_session_name,
            source_sha=args.source_sha,
            source_blob=source,
            provenance=provenance,
            attempt=args.migration_attempt_id,
            prior_version=args.expected_current_version,
            prior_hash=args.expected_current_canonical_sha256,
        )
        actor = payload.get("actor_last")
        actor_observation = actor if isinstance(actor, str) else None
        result = {
            "status": payload.get("status"),
            "phase": payload.get("phase"),
            "attempt": args.migration_attempt_id,
            "source_sha": args.source_sha,
            "provenance": provenance,
            "actor_session_sha256": actor_observation,
        }
        return_code = 0 if payload.get("status") == "PASS" else 1
    except (MigrationError, OSError, UnicodeError) as error:
        candidate_aws = locals().get("aws")
        if isinstance(candidate_aws, AdminAwsCli):
            actor_observation = candidate_aws.actor_observation
        category = (
            error.category if isinstance(error, MigrationError) else "local_io_error"
        )
        result = {
            "status": "FAIL",
            "category": category,
            "attempt": args.migration_attempt_id,
            "source_sha": args.source_sha,
            "actor_session_sha256": actor_observation,
        }
        return_code = 1
    try:
        _write_local_summary(args.audit_path, result)
    except (MigrationError, OSError):
        return 1
    print(_canonical_json(result))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
