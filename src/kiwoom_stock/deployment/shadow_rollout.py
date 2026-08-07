"""Exact, fail-closed shadow worker/document rollout command plane."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping, Sequence


REPOSITORY = "SpiceChicken/kiwoom_stock"
REGION = "ap-northeast-2"
INSTANCE_ID = "i-02cb0a404794bd43a"
ROLLOUT_DOCUMENT = "KiwoomStock-ShadowWorkerRollout"
SHADOW_DOCUMENT = "KiwoomStock-ShadowWorker"
ROLLOUT_ROLE_NAME = "kiwoom-stock-github-shadow-rollout"
WORKER_PATH = Path("deploy/ec2/shadow_worker_control.sh")
SHADOW_DOCUMENT_PATH = Path("deploy/ssm/shadow-worker-document.yaml")
ROLLOUT_DOCUMENT_PATH = Path("deploy/ssm/shadow-worker-rollout-document.yaml")
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[1-9][0-9]{0,19}")
COMMAND_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
TERMINAL = {"Success", "Failed", "Cancelled", "TimedOut", "Cancelling"}
LEGACY_QUIET_WINDOW_SECONDS = 3600
LEGACY_SETTLING_SECONDS = 60
LEGACY_SCAN_OFFSETS = (0, 30, 60)
LEGACY_HISTORY_MAX_PAGES = 20
LEGACY_HISTORY_PAGE_SIZE = 50
LEGACY_TERMINAL_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}
LEGACY_ALL_STATUSES = LEGACY_TERMINAL_STATUSES | {
    "Pending", "InProgress", "Delayed", "Cancelling",
}
LEGACY_COMMAND_STATUSES = LEGACY_TERMINAL_STATUSES | {
    "Pending", "InProgress", "Cancelling",
}
HISTORY_COMMAND_KEYS = {
    "CommandId", "DocumentName", "DocumentVersion", "Comment",
    "ExpiresAfter", "Parameters", "InstanceIds", "Targets",
    "RequestedDateTime", "Status", "StatusDetails", "OutputS3Region",
    "OutputS3BucketName", "OutputS3KeyPrefix", "MaxConcurrency", "MaxErrors",
    "TargetCount", "CompletedCount", "ErrorCount", "DeliveryTimedOutCount",
    "ServiceRole", "NotificationConfig", "CloudWatchOutputConfig",
    "TimeoutSeconds", "AlarmConfiguration", "TriggeredAlarms",
}
HISTORY_ITEM_KEYS = {
    "CommandId", "InstanceId", "InstanceName", "Comment", "DocumentName",
    "DocumentVersion", "RequestedDateTime", "Status", "StatusDetails",
    "TraceOutput", "StandardOutputUrl", "StandardErrorUrl", "CommandPlugins",
    "ServiceRole",
    "NotificationConfig", "CloudWatchOutputConfig",
}
HOST_EVIDENCE_KEYS = {
    "action", "source_sha", "worker_sha256", "shadow_document_sha256",
    "rollout_attempt_id", "observed_worker_sha256", "worker_present",
    "worker_owner", "worker_mode", "worker_links", "worker_regular",
    "worker_metadata_valid", "binding_present", "binding_owner",
    "binding_mode", "binding_links", "binding_regular",
    "binding_metadata_valid",
}
ROLLOUT_YAML_PREFIX = b'''schemaVersion: "2.2"
description: Install, read back, or roll back the exact shadow worker pair
parameters:
  Action:
    type: String
    allowedValues: [install, readback, rollback]
    interpolationType: ENV_VAR
  SourceSha:
    type: String
    allowedPattern: '^[0-9a-f]{40}$'
    interpolationType: ENV_VAR
  WorkerSha256:
    type: String
    allowedPattern: '^[0-9a-f]{64}$'
    interpolationType: ENV_VAR
  ShadowDocumentSha256:
    type: String
    allowedPattern: '^[0-9a-f]{64}$'
    interpolationType: ENV_VAR
  RolloutAttemptId:
    type: String
    allowedPattern: '^[1-9][0-9]{0,19}$'
    interpolationType: ENV_VAR
  ExpectedInstanceId:
    type: String
    allowedPattern: '^i-02cb0a404794bd43a$'
    interpolationType: ENV_VAR
  Region:
    type: String
    allowedPattern: '^ap-northeast-2$'
    interpolationType: ENV_VAR
mainSteps:
  - action: aws:runShellScript
    name: exactShadowPairRollout
    precondition:
      StringEquals: [platformType, Linux]
    inputs:
      timeoutSeconds: "300"
      runCommand:
'''


class RolloutError(RuntimeError):
    """An operator-safe rollout failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RolloutError("document_duplicate_key")
        result[key] = value
    return result


def strict_json(data: bytes) -> object:
    """Parse the JSON-compatible SSM source while rejecting duplicate keys."""

    try:
        return json.loads(data, object_pairs_hook=_no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RolloutError("document_json_invalid") from error


def canonical_json(data: bytes) -> tuple[object, bytes]:
    value = strict_json(data)
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return value, encoded


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _rollout_script(source: bytes) -> str:
    """Extract the sole literal runCommand from the repository YAML source."""

    marker = b"        - |\n"
    if source.count(marker) != 1:
        raise RolloutError("rollout_source_shape_invalid")
    prefix, encoded_body = source.split(marker, 1)
    if prefix != ROLLOUT_YAML_PREFIX:
        raise RolloutError("rollout_source_contract_invalid")
    body = encoded_body.decode("utf-8")
    lines = body.splitlines()
    if not lines or any(not line.startswith("          ") for line in lines if line):
        raise RolloutError("rollout_source_indentation_invalid")
    return "\n".join(line[10:] if line else "" for line in lines) + "\n"


def expected_rollout_document(source: bytes | None = None) -> dict[str, object]:
    """Build the exact semantic SSM v1 attestation without a YAML dependency."""

    raw = ROLLOUT_DOCUMENT_PATH.read_bytes() if source is None else source
    patterns = {
        "SourceSha": "^[0-9a-f]{40}$",
        "WorkerSha256": "^[0-9a-f]{64}$",
        "ShadowDocumentSha256": "^[0-9a-f]{64}$",
        "RolloutAttemptId": "^[1-9][0-9]{0,19}$",
        "ExpectedInstanceId": "^i-02cb0a404794bd43a$",
        "Region": "^ap-northeast-2$",
    }
    parameters: dict[str, object] = {
        "Action": {
            "type": "String",
            "allowedValues": ["install", "readback", "rollback"],
            "interpolationType": "ENV_VAR",
        }
    }
    parameters.update({
        name: {
            "type": "String", "allowedPattern": pattern,
            "interpolationType": "ENV_VAR",
        }
        for name, pattern in patterns.items()
    })
    return {
        "schemaVersion": "2.2",
        "description": "Install, read back, or roll back the exact shadow worker pair",
        "parameters": parameters,
        "mainSteps": [{
            "action": "aws:runShellScript",
            "name": "exactShadowPairRollout",
            "precondition": {"StringEquals": ["platformType", "Linux"]},
            "inputs": {"timeoutSeconds": "300", "runCommand": [_rollout_script(raw)]},
        }],
    }


@dataclass(frozen=True)
class RolloutTuple:
    """Immutable rollout identity (Git SHA, content hashes, positive attempt)."""

    source_sha: str
    worker_sha256: str
    shadow_document_sha256: str
    shadow_document_raw_sha256: str
    rollout_document_sha256: str
    rollout_attempt_id: str

    def validate(self) -> None:
        if SOURCE_RE.fullmatch(self.source_sha) is None:
            raise RolloutError("source_sha_invalid")
        for value in (
            self.worker_sha256,
            self.shadow_document_sha256,
            self.shadow_document_raw_sha256,
            self.rollout_document_sha256,
        ):
            if HASH_RE.fullmatch(value) is None:
                raise RolloutError("hash_invalid")
        if ID_RE.fullmatch(self.rollout_attempt_id) is None:
            raise RolloutError("rollout_attempt_id_invalid")


class AwsCli:
    """Bounded AWS CLI adapter; write operations are never blindly retried."""

    def __init__(self, deadline: float, clock: Callable[[], float] = time.monotonic):
        self.deadline = deadline
        self.clock = clock
        self.command_ids: list[str] = []
        self.host_evidence: list[dict[str, object]] = []
        self.commands: list[dict[str, object]] = []

    def call(self, args: Sequence[str], *, write: bool = False) -> object:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise RolloutError("execution_deadline_exhausted")
        env = {
            **os.environ,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AWS_PAGER": "",
            "AWS_MAX_ATTEMPTS": "1" if write else "3",
            "AWS_RETRY_MODE": "standard",
        }
        try:
            completed = subprocess.run(
                ["aws", *args, "--output", "json"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=min(60.0, remaining),
            )
        except subprocess.TimeoutExpired as error:
            raise RolloutError("aws_timeout") from error
        if completed.returncode != 0:
            category = (
                "invocation_not_found"
                if "InvocationDoesNotExist" in completed.stderr
                else "aws_command_failed"
            )
            raise RolloutError(category)
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise RolloutError("aws_response_invalid") from error

    def send(
        self, action: str, rollout: RolloutTuple, *, expect_tuple: bool = False
    ) -> dict[str, object]:
        record: dict[str, object] = {
            "action": action, "command_id": None, "accepted": "uncertain",
            "status": "unknown", "response_code": None,
        }
        self.commands.append(record)
        response = self.call([
                "ssm", "send-command",
                "--document-name", ROLLOUT_DOCUMENT,
                "--document-version", "1",
                "--instance-ids", INSTANCE_ID,
                "--parameters", json.dumps({
                    "Action": [action],
                    "SourceSha": [rollout.source_sha],
                    "WorkerSha256": [rollout.worker_sha256],
                    "ShadowDocumentSha256": [rollout.shadow_document_sha256],
                    "RolloutAttemptId": [rollout.rollout_attempt_id],
                    "ExpectedInstanceId": [INSTANCE_ID],
                    "Region": [REGION],
                }, separators=(",", ":")),
                "--timeout-seconds", "300",
                "--max-concurrency", "1",
                "--max-errors", "0",
            ], write=True)
        if not isinstance(response, dict) or not isinstance(response.get("Command"), dict):
            raise RolloutError("send_response_invalid")
        record["accepted"] = True
        command_id = response["Command"].get("CommandId")
        if not isinstance(command_id, str) or COMMAND_RE.fullmatch(command_id) is None:
            raise RolloutError("command_id_invalid")
        self.command_ids.append(command_id)
        record["command_id"] = command_id
        invocation = self.poll(command_id)
        record["status"] = invocation.get("Status", "unknown")
        record["response_code"] = invocation.get("ResponseCode")
        if invocation.get("Status") != "Success" or invocation.get("ResponseCode") != 0:
            raise RolloutError("host_action_failed")
        evidence = self._host_evidence(invocation)
        self.host_evidence.append(evidence)
        if evidence.get("action") != action:
            raise RolloutError("host_evidence_action_mismatch")
        if expect_tuple and any((
            evidence.get("source_sha") != rollout.source_sha,
            evidence.get("worker_sha256") != rollout.worker_sha256,
            evidence.get("shadow_document_sha256") != rollout.shadow_document_sha256,
            evidence.get("rollout_attempt_id") != rollout.rollout_attempt_id,
            evidence.get("observed_worker_sha256") != rollout.worker_sha256,
            evidence.get("worker_present") is not True,
            evidence.get("worker_metadata_valid") is not True,
            evidence.get("binding_present") is not True,
            evidence.get("binding_metadata_valid") is not True,
        )):
            raise RolloutError("host_evidence_tuple_mismatch")
        return evidence

    @staticmethod
    def _host_evidence(invocation: Mapping[str, object]) -> dict[str, object]:
        stdout = invocation.get("StandardOutputContent")
        if not isinstance(stdout, str) or len(stdout) > 65536:
            raise RolloutError("host_evidence_invalid")
        records: list[dict[str, object]] = []
        for line in stdout.splitlines():
            if not line.startswith("{"):
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and set(value) == HOST_EVIDENCE_KEYS:
                records.append(value)
        if not records:
            raise RolloutError("host_evidence_missing")
        return records[-1]

    def poll(self, command_id: str) -> dict[str, object]:
        for attempt in range(60):
            try:
                response = self.call([
                    "ssm", "get-command-invocation", "--command-id", command_id,
                    "--instance-id", INSTANCE_ID,
                ])
            except RolloutError as error:
                if error.category == "invocation_not_found" and attempt < 5:
                    time.sleep(2)
                    continue
                raise
            if not isinstance(response, dict):
                raise RolloutError("invocation_invalid")
            status = response.get("Status")
            if status in TERMINAL:
                return response
            time.sleep(5)
        raise RolloutError("host_action_timeout")


def _document_content(response: object) -> tuple[object, bytes]:
    if not isinstance(response, dict) or not isinstance(response.get("Content"), str):
        raise RolloutError("document_readback_invalid")
    raw = response["Content"].encode("utf-8")
    return strict_json(raw), raw


def _document_description(response: object) -> dict[str, object]:
    document = response.get("Document") if isinstance(response, dict) else None
    if not isinstance(document, dict):
        raise RolloutError("document_description_invalid")
    return document


def attest_activation_document(expected_hash: str) -> str:
    """Return the explicit active default version after canonical attestation."""

    if HASH_RE.fullmatch(expected_hash) is None:
        raise RolloutError("activation_document_hash_invalid")
    aws = AwsCli(time.monotonic() + 90.0)
    document = _document_description(aws.call([
        "ssm", "describe-document", "--name", SHADOW_DOCUMENT,
    ]))
    default = document.get("DefaultVersion")
    if (
        document.get("Status") != "Active"
        or not isinstance(default, str)
        or re.fullmatch(r"[1-9][0-9]*", default) is None
    ):
        raise RolloutError("activation_document_default_invalid")
    content, _ = _document_content(aws.call([
        "ssm", "get-document", "--name", SHADOW_DOCUMENT,
        "--document-version", default, "--document-format", "JSON",
    ]))
    if sha256(_canonical_bytes(content)) != expected_hash:
        raise RolloutError("activation_document_hash_mismatch")
    return default


def attest_rollout_document(
    aws: AwsCli, expected: Mapping[str, object]
) -> str:
    """Attest fixed v1/default/latest and semantic/canonical content."""

    document = _document_description(aws.call([
        "ssm", "describe-document", "--name", ROLLOUT_DOCUMENT,
    ]))
    if any((
        document.get("Status") != "Active",
        document.get("DefaultVersion") != "1",
        document.get("LatestVersion") != "1",
    )):
        raise RolloutError("rollout_document_version_invalid")
    actual, _ = _document_content(aws.call([
        "ssm", "get-document", "--name", ROLLOUT_DOCUMENT,
        "--document-version", "1", "--document-format", "JSON",
    ]))
    if actual != expected:
        raise RolloutError("rollout_document_semantic_mismatch")
    actual_hash = sha256(_canonical_bytes(actual))
    if actual_hash != sha256(_canonical_bytes(expected)):
        raise RolloutError("rollout_document_canonical_mismatch")
    return actual_hash


def _utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise RolloutError("legacy_history_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise RolloutError("legacy_history_timestamp_invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RolloutError("legacy_history_timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _scan_legacy_commands(
    aws: AwsCli, cutoff: datetime, plane: dict[str, object]
) -> None:
    seen_commands: set[str] = set()
    seen_tokens: set[str] = set()
    next_token: str | None = None
    count = recent_count = nonterminal_count = 0
    try:
        for page in range(LEGACY_HISTORY_MAX_PAGES):
            args = [
                "ssm", "list-commands", "--instance-id", INSTANCE_ID,
                "--filters", f"key=DocumentName,value={SHADOW_DOCUMENT}",
                "--max-results", str(LEGACY_HISTORY_PAGE_SIZE), "--no-paginate",
            ]
            if next_token is not None:
                args.extend(["--next-token", next_token])
            response = aws.call(args)
            if (
                not isinstance(response, dict)
                or not set(response).issubset({"Commands", "NextToken"})
                or not isinstance(response.get("Commands"), list)
            ):
                raise RolloutError("legacy_commands_shape_invalid")
            commands = response["Commands"]
            if len(commands) > LEGACY_HISTORY_PAGE_SIZE:
                raise RolloutError("legacy_commands_page_oversized")
            for command in commands:
                if (
                    not isinstance(command, dict)
                    or not set(command).issubset(HISTORY_COMMAND_KEYS)
                    or not {"CommandId", "DocumentName", "InstanceIds",
                            "RequestedDateTime", "Status"}.issubset(command)
                ):
                    raise RolloutError("legacy_commands_shape_invalid")
                command_id = command["CommandId"]
                status = command["Status"]
                if (
                    not isinstance(command_id, str)
                    or COMMAND_RE.fullmatch(command_id) is None
                    or command_id in seen_commands
                    or command["DocumentName"] != SHADOW_DOCUMENT
                    or command["InstanceIds"] != [INSTANCE_ID]
                    or command.get("Targets", []) != []
                    or not isinstance(status, str)
                    or status not in LEGACY_COMMAND_STATUSES
                ):
                    raise RolloutError("legacy_commands_item_invalid")
                seen_commands.add(command_id)
                requested = _utc_timestamp(command["RequestedDateTime"])
                count += 1
                if requested >= cutoff:
                    recent_count += 1
                if status not in LEGACY_TERMINAL_STATUSES:
                    nonterminal_count += 1
                plane.update({
                    "count": count, "recent_count": recent_count,
                    "nonterminal_count": nonterminal_count,
                })
            token = response.get("NextToken")
            if token is None or token == "":
                next_token = None
                break
            if (
                not isinstance(token, str) or not token or len(token) > 4096
                or token in seen_tokens
            ):
                raise RolloutError("legacy_commands_next_token_invalid")
            seen_tokens.add(token)
            next_token = token
        else:
            raise RolloutError("legacy_commands_page_limit")
        if next_token is not None:
            raise RolloutError("legacy_commands_page_limit")
        if nonterminal_count != 0:
            raise RolloutError("legacy_commands_nonterminal")
        if recent_count != 0:
            raise RolloutError("legacy_commands_not_quiet")
        plane["result"] = "PASS"
    except RolloutError:
        plane["result"] = "FAIL"
        raise


def _scan_legacy_invocations(
    aws: AwsCli, cutoff: datetime, plane: dict[str, object]
) -> None:
    seen_commands: set[str] = set()
    seen_tokens: set[str] = set()
    next_token: str | None = None
    count = recent_count = nonterminal_count = 0
    try:
        for page in range(LEGACY_HISTORY_MAX_PAGES):
            args = [
                "ssm", "list-command-invocations", "--instance-id", INSTANCE_ID,
                "--filters", f"key=DocumentName,value={SHADOW_DOCUMENT}",
                "--no-details", "--max-results", str(LEGACY_HISTORY_PAGE_SIZE),
                "--no-paginate",
            ]
            if next_token is not None:
                args.extend(["--next-token", next_token])
            response = aws.call(args)
            if (
                not isinstance(response, dict)
                or not set(response).issubset({"CommandInvocations", "NextToken"})
                or (
                    "CommandInvocations" in response
                    and response["CommandInvocations"] is not None
                    and not isinstance(response["CommandInvocations"], list)
                )
            ):
                raise RolloutError("legacy_history_shape_invalid")
            # AWS documents CommandInvocations as optional; an empty result
            # may be returned with the member omitted or represented as null.
            invocations_value = response.get("CommandInvocations")
            invocations = [] if invocations_value is None else invocations_value
            if len(invocations) > LEGACY_HISTORY_PAGE_SIZE:
                raise RolloutError("legacy_history_page_oversized")
            for invocation in invocations:
                if (
                    not isinstance(invocation, dict)
                    or not set(invocation).issubset(HISTORY_ITEM_KEYS)
                    or not {"CommandId", "InstanceId", "DocumentName",
                            "RequestedDateTime", "Status"}.issubset(invocation)
                ):
                    raise RolloutError("legacy_history_shape_invalid")
                command_id = invocation["CommandId"]
                status = invocation["Status"]
                if (
                    not isinstance(command_id, str)
                    or COMMAND_RE.fullmatch(command_id) is None
                    or command_id in seen_commands
                    or invocation["InstanceId"] != INSTANCE_ID
                    or invocation["DocumentName"] != SHADOW_DOCUMENT
                    or not isinstance(status, str)
                    or status not in LEGACY_ALL_STATUSES
                ):
                    raise RolloutError("legacy_history_item_invalid")
                seen_commands.add(command_id)
                requested = _utc_timestamp(invocation["RequestedDateTime"])
                count += 1
                if requested >= cutoff:
                    recent_count += 1
                if status not in LEGACY_TERMINAL_STATUSES:
                    nonterminal_count += 1
                plane.update({
                    "count": count, "recent_count": recent_count,
                    "nonterminal_count": nonterminal_count,
                })
            token = response.get("NextToken")
            if token is None or token == "":
                next_token = None
                break
            if (
                not isinstance(token, str) or not token or len(token) > 4096
                or token in seen_tokens
            ):
                raise RolloutError("legacy_history_next_token_invalid")
            seen_tokens.add(token)
            next_token = token
        else:
            raise RolloutError("legacy_history_page_limit")
        if next_token is not None:
            raise RolloutError("legacy_history_page_limit")
        if nonterminal_count != 0:
            raise RolloutError("legacy_history_nonterminal")
        if recent_count != 0:
            raise RolloutError("legacy_history_not_quiet")
        plane["result"] = "PASS"
    except RolloutError:
        plane["result"] = "FAIL"
        raise


def attest_legacy_command_quiet(
    aws: AwsCli,
    *,
    now: datetime | None = None,
    evidence: dict[str, object] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Prove three complete quiet snapshots over a 60-second settling window."""

    base_checked = now or datetime.now(timezone.utc)
    if base_checked.tzinfo is None or base_checked.utcoffset() is None:
        raise RolloutError("legacy_history_checked_at_invalid")
    base_checked = base_checked.astimezone(timezone.utc)
    record = evidence if evidence is not None else {}
    scans: list[dict[str, object]] = []
    record.update({
        "mode": "legacy", "quiet_window_seconds": LEGACY_QUIET_WINDOW_SECONDS,
        "required_scan_count": len(LEGACY_SCAN_OFFSETS), "scan_count": 0,
        "settling_seconds": LEGACY_SETTLING_SECONDS,
        "first_checked_at": None, "last_checked_at": None,
        "scans": scans, "result": "checking",
    })
    started = monotonic()
    try:
        for index, offset in enumerate(LEGACY_SCAN_OFFSETS, start=1):
            target = started + offset
            for _attempt in range(4):
                before = monotonic()
                remaining = target - before
                if remaining <= 0:
                    break
                sleeper(remaining)
                if monotonic() <= before:
                    raise RolloutError("legacy_settling_clock_invalid")
            else:
                raise RolloutError("legacy_settling_clock_invalid")
            record["observed_settling_seconds"] = round(
                max(0.0, monotonic() - started), 3
            )
            checked = base_checked + timedelta(seconds=offset)
            checked_at = checked.isoformat().replace("+00:00", "Z")
            aggregate = {
                "count": 0, "recent_count": 0,
                "nonterminal_count": 0, "result": "checking",
            }
            invocations = {
                "count": 0, "recent_count": 0,
                "nonterminal_count": 0, "result": "not-run",
            }
            scan: dict[str, object] = {
                "index": index, "checked_at": checked_at,
                "aggregate_commands": aggregate,
                "node_invocations": invocations, "result": "checking",
            }
            scans.append(scan)
            record["scan_count"] = index
            if record["first_checked_at"] is None:
                record["first_checked_at"] = checked_at
            record["last_checked_at"] = checked_at
            cutoff = checked - timedelta(seconds=LEGACY_QUIET_WINDOW_SECONDS)
            _scan_legacy_commands(aws, cutoff, aggregate)
            invocations["result"] = "checking"
            _scan_legacy_invocations(aws, cutoff, invocations)
            scan["result"] = "PASS"
        record["result"] = "PASS"
        return record
    except RolloutError:
        if scans:
            scans[-1]["result"] = "FAIL"
        record["result"] = "FAIL"
        raise


def set_default_reconciled(aws: AwsCli, version: str) -> str:
    """Write once, then make DescribeDocument the authority on response loss."""

    response_seen = True
    try:
        aws.call([
            "ssm", "update-document-default-version", "--name", SHADOW_DOCUMENT,
            "--document-version", version,
        ], write=True)
    except RolloutError:
        response_seen = False
    for attempt in range(8):
        try:
            document = _document_description(aws.call([
                "ssm", "describe-document", "--name", SHADOW_DOCUMENT,
            ]))
            if document.get("DefaultVersion") == version:
                return "response+readback" if response_seen else "ambiguous+readback"
        except RolloutError:
            pass
        if attempt < 7:
            time.sleep(1)
    raise RolloutError("document_default_unconfirmed")


def _host_identity(evidence: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        evidence.get(name) for name in sorted(HOST_EVIDENCE_KEYS - {"action"})
    )


def _write_audit(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def execute(
    source_sha: str,
    attempt_id: str,
    audit_path: Path,
    *,
    drain_sleeper: Callable[[float], None] = time.sleep,
    drain_monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Install the host pair, update/default/read back, or roll both back."""

    if SOURCE_RE.fullmatch(source_sha) is None or source_sha != os.getenv("GITHUB_SHA"):
        raise RolloutError("source_sha_mismatch")
    worker = WORKER_PATH.read_bytes()
    shadow_raw = SHADOW_DOCUMENT_PATH.read_bytes()
    shadow_object, shadow_canonical = canonical_json(shadow_raw)
    rollout_source = ROLLOUT_DOCUMENT_PATH.read_bytes()
    rollout_semantic = expected_rollout_document(rollout_source)
    rollout = RolloutTuple(
        source_sha=source_sha,
        worker_sha256=sha256(worker),
        shadow_document_sha256=sha256(shadow_canonical),
        shadow_document_raw_sha256=sha256(shadow_raw),
        rollout_document_sha256=sha256(rollout_source),
        rollout_attempt_id=attempt_id,
    )
    rollout.validate()
    started = time.monotonic()
    aws = AwsCli(started + 960.0)
    audit: dict[str, object] = {
        "schema_version": 1, "outcome": "failed", "failure_category": None,
        "repository": REPOSITORY, "environment": "production-shadow",
        "run_id": os.getenv("GITHUB_RUN_ID", ""),
        "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT", ""),
        "source_sha": source_sha, "rollout_attempt_id": attempt_id,
        "region": REGION, "instance_id": INSTANCE_ID,
        "role_name": ROLLOUT_ROLE_NAME, "rollout_document": ROLLOUT_DOCUMENT,
        "shadow_document": SHADOW_DOCUMENT,
        "worker_sha256": rollout.worker_sha256,
        "shadow_document_raw_sha256": rollout.shadow_document_raw_sha256,
        "shadow_document_sha256": rollout.shadow_document_sha256,
        "rollout_document_sha256": rollout.rollout_document_sha256,
        "commands": aws.commands, "host_before": None, "host_new": None,
        "host_final": None, "phase": "prestate_capturing",
        "default_transitions": [], "rollback": False,
        "rollback_failure_category": None, "skew": False,
        "legacy_transition": None,
    }
    previous_default = ""
    pre_host: dict[str, object] | None = None
    install_started = False
    install_acceptance_uncertain = False
    try:
        audit["rollout_document_canonical_sha256"] = attest_rollout_document(
            aws, rollout_semantic
        )
        described = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])
        document = _document_description(described)
        default_value = document.get("DefaultVersion")
        if not isinstance(default_value, str):
            raise RolloutError("prestate_invalid")
        previous_default = default_value
        audit["previous_default_version"] = previous_default
        aws.call(["ssm", "list-document-versions", "--name", SHADOW_DOCUMENT])
        previous = aws.call([
            "ssm", "get-document", "--name", SHADOW_DOCUMENT,
            "--document-version", previous_default, "--document-format", "JSON",
        ])
        previous_object, _ = _document_content(previous)
        audit["previous_document_sha256"] = sha256(_canonical_bytes(previous_object))
        legacy_transition: dict[str, object] = {}
        audit["legacy_transition"] = legacy_transition
        if previous_object == shadow_object:
            checked = datetime.now(timezone.utc)
            legacy_transition.update({
                "mode": "steady",
                "checked_at": checked.isoformat().replace("+00:00", "Z"),
                "quiet_window_seconds": LEGACY_QUIET_WINDOW_SECONDS,
                "required_scan_count": len(LEGACY_SCAN_OFFSETS),
                "scan_count": 0,
                "settling_seconds": LEGACY_SETTLING_SECONDS,
                "observed_settling_seconds": 0,
                "first_checked_at": None,
                "last_checked_at": None,
                "scans": [],
                "result": "n-a",
            })
        else:
            attest_legacy_command_quiet(
                aws, evidence=legacy_transition, sleeper=drain_sleeper,
                monotonic=drain_monotonic,
            )
        pre_host = aws.send("readback", rollout)
        audit["host_before"] = pre_host
        audit["phase"] = "host_applying"
        install_started = True
        try:
            new_host = aws.send("install", rollout, expect_tuple=True)
        except RolloutError:
            install_record = next(
                (item for item in reversed(aws.commands) if item.get("action") == "install"),
                None,
            )
            install_acceptance_uncertain = (
                not isinstance(install_record, dict)
                or install_record.get("accepted") == "uncertain"
                or install_record.get("status") == "unknown"
            )
            raise
        audit["host_new"] = new_host
        audit["phase"] = "host_applied"
        audit["phase"] = "document_applying"
        try:
            update = aws.call([
                "ssm", "update-document", "--name", SHADOW_DOCUMENT,
                "--document-version", "$LATEST", "--document-format", "JSON",
                "--content", shadow_canonical.decode("utf-8"),
            ], write=True)
            description = update.get("DocumentDescription") if isinstance(update, dict) else None
            new_version = description.get("DocumentVersion") if isinstance(description, dict) else None
        except RolloutError:
            new_version = None
        if not isinstance(new_version, str) or not new_version.isdecimal():
            # Ambiguous writes are reconciled by exact latest content, never retried.
            reconciled_version: str | None = None
            for attempt in range(8):
                try:
                    latest_desc = _document_description(aws.call([
                        "ssm", "describe-document", "--name", SHADOW_DOCUMENT,
                    ]))
                    candidate = latest_desc.get("LatestVersion")
                    if isinstance(candidate, str):
                        latest = aws.call([
                            "ssm", "get-document", "--name", SHADOW_DOCUMENT,
                            "--document-version", candidate, "--document-format", "JSON",
                        ])
                        latest_object, _ = _document_content(latest)
                        if latest_object == shadow_object:
                            reconciled_version = candidate
                            break
                except RolloutError:
                    pass
                if attempt < 7:
                    time.sleep(1)
            new_version = reconciled_version
            if not isinstance(new_version, str):
                raise RolloutError("document_update_ambiguous")
        audit["new_document_version"] = new_version
        transition = set_default_reconciled(aws, new_version)
        transitions = audit["default_transitions"]
        assert isinstance(transitions, list)
        transitions.append({"to": new_version, "reconciliation": transition})
        final = aws.call(["ssm", "get-document", "--name", SHADOW_DOCUMENT,
                          "--document-version", new_version, "--document-format", "JSON"])
        final_object, final_raw = _document_content(final)
        final_desc = aws.call(["ssm", "describe-document", "--name", SHADOW_DOCUMENT])
        final_document = final_desc.get("Document") if isinstance(final_desc, dict) else None
        audit["semantic_readback"] = final_object == shadow_object
        audit["byte_readback"] = final_raw == shadow_canonical
        audit["final_default_version"] = (
            final_document.get("DefaultVersion") if isinstance(final_document, dict) else None
        )
        if not audit["semantic_readback"] or not audit["byte_readback"] or audit["final_default_version"] != new_version:
            raise RolloutError("document_readback_mismatch")
        aws.call(["ssm", "list-document-versions", "--name", SHADOW_DOCUMENT])
        final_host = aws.send("readback", rollout, expect_tuple=True)
        audit["host_final"] = final_host
        audit["phase"] = "applied"
        audit["outcome"] = "applied"
        return audit
    except RolloutError as error:
        audit["failure_category"] = error.category
        if install_started:
            audit["rollback"] = True
            audit["phase"] = "rollback_applying"
            try:
                if previous_default:
                    transition = set_default_reconciled(aws, previous_default)
                    transitions = audit["default_transitions"]
                    assert isinstance(transitions, list)
                    transitions.append({
                        "to": previous_default, "reconciliation": transition,
                        "rollback": True,
                    })
                reconciled = aws.send("readback", rollout)
                audit["host_reconciled_after_failure"] = reconciled
                if pre_host is None:
                    raise RolloutError("host_prestate_missing")
                if _host_identity(reconciled) != _host_identity(pre_host):
                    restored = aws.send("rollback", rollout)
                    audit["host_final"] = restored
                    if _host_identity(restored) != _host_identity(pre_host):
                        raise RolloutError("host_rollback_mismatch")
                else:
                    audit["host_final"] = reconciled
                if install_acceptance_uncertain:
                    raise RolloutError("install_acceptance_uncertain")
                audit["phase"] = "rolled_back"
            except RolloutError as rollback_error:
                audit["rollback_failure_category"] = rollback_error.category
                audit["skew"] = True
                audit["phase"] = "skew"
        raise
    finally:
        audit["duration_seconds"] = round(time.monotonic() - started, 3)
        _write_audit(audit_path, audit)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--rollout-attempt-id", required=True)
    parser.add_argument("--audit", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        execute(args.source_sha, args.rollout_attempt_id, args.audit)
    except (RolloutError, OSError) as error:
        print(f"shadow rollout failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
