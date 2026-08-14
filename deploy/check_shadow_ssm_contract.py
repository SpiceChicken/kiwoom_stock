#!/usr/bin/env python3
"""Verify the complete project-owned shadow SSM deployment graph."""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Mapping
import json
from pathlib import Path
import re
import shlex
import sys
from typing import Any, NoReturn, cast

import yaml


ACTIVATION_WORKFLOW = Path(".github/workflows/cd-shadow-worker-activation.yml")
ROLLOUT_WORKFLOW = Path(".github/workflows/cd-shadow-worker-rollout.yml")
ACTIVATION_DOCUMENT = Path("deploy/ssm/shadow-worker-document.yaml")
ROLLOUT_DOCUMENT = Path("deploy/ssm/shadow-worker-rollout-document.yaml")
WORKER = Path("deploy/ec2/shadow_worker_control.sh")
VALIDATOR = Path("deploy/ec2/shadow_runtime_evidence.py")
ROLLOUT_EXECUTOR = Path("src/kiwoom_stock/deployment/shadow_rollout.py")
ROLLOUT_MIGRATION = Path("deploy/migrate_shadow_rollout_document.py")
MIGRATION_BOOTSTRAP = Path("deploy/bootstrap_shadow_rollout_migration.py")
MIGRATION_WORKFLOW = Path(".github/workflows/cd-shadow-rollout-document-migration.yml")
MIGRATION_TRUST = Path("deploy/iam/github-shadow-migration-trust.json.example")
MIGRATION_POLICY = Path("deploy/iam/github-shadow-migration-policy.json.example")
ROLLOUT_POLICY = Path("deploy/iam/github-shadow-rollout-policy.json.example")
CI_WORKFLOW = Path(".github/workflows/ci.yml")

REGION = "ap-northeast-2"
INSTANCE_ID = "i-02cb0a404794bd43a"
ACTIVATION_DOCUMENT_NAME = "KiwoomStock-ShadowWorker"
ROLLOUT_DOCUMENT_NAME = "KiwoomStock-ShadowWorkerRollout"
TERMINAL_STATUSES = {"Success", "Failed", "Cancelled", "TimedOut"}

ACTIVATION_INPUT_ENV = {
    "SOURCE_SHA": "${{ inputs.source_sha }}",
    "IMAGE_DIGEST": "${{ inputs.image_digest }}",
    "BUILD_RUN_ID": "${{ inputs.build_run_id }}",
    "COMPOSE_SHADOW_SHA256": "${{ inputs.compose_shadow_sha256 }}",
    "ACTIVATION_ID": "${{ inputs.activation_id }}",
    "DESIRED_STATE": "${{ inputs.desired_state }}",
    "WORKER_SHA256": "${{ inputs.worker_sha256 }}",
    "VALIDATOR_SHA256": "${{ inputs.validator_sha256 }}",
    "SHADOW_DOCUMENT_SHA256": "${{ inputs.shadow_document_sha256 }}",
}
ACTIVATION_PARAMETER_ENV = {
    "DesiredState": "DESIRED_STATE",
    "ImageDigest": "IMAGE_DIGEST",
    "SourceSha": "SOURCE_SHA",
    "ActivationId": "ACTIVATION_ID",
    "ComposeShadowSha256": "ssm_compose_hash",
    "ExpectedWorkerSha256": "WORKER_SHA256",
    "ExpectedValidatorSha256": "VALIDATOR_SHA256",
    "ExpectedShadowDocumentSha256": "SHADOW_DOCUMENT_SHA256",
    "ExpectedInstanceId": "EC2_INSTANCE_ID",
    "Region": "AWS_REGION",
}
ROLLOUT_PARAMETER_NAMES = {
    "Action", "SourceSha", "WorkerSha256", "ValidatorSha256",
    "ShadowDocumentSha256", "RolloutAttemptId", "ExpectedInstanceId", "Region",
}
HASH_PATTERN = "^[0-9a-f]{64}$"
ACTIVATION_PARAMETER_SCHEMA = {
    "DesiredState": {
        "type": "String", "allowedValues": ["oneshot", "continuous", "stop"],
        "interpolationType": "ENV_VAR",
    },
    "ImageDigest": {
        "type": "String",
        "allowedPattern": "^ghcr\\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$",
        "interpolationType": "ENV_VAR",
    },
    "SourceSha": {
        "type": "String", "allowedPattern": "^[0-9a-f]{40}$",
        "interpolationType": "ENV_VAR",
    },
    "ActivationId": {
        "type": "String", "allowedPattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
        "interpolationType": "ENV_VAR",
    },
    **{
        name: {
            "type": "String", "allowedPattern": HASH_PATTERN,
            "interpolationType": "ENV_VAR",
        }
        for name in (
            "ComposeShadowSha256", "ExpectedWorkerSha256",
            "ExpectedValidatorSha256", "ExpectedShadowDocumentSha256",
        )
    },
    "ExpectedInstanceId": {
        "type": "String", "allowedPattern": f"^{INSTANCE_ID}$",
        "interpolationType": "ENV_VAR",
    },
    "Region": {
        "type": "String", "allowedPattern": f"^{REGION}$",
        "interpolationType": "ENV_VAR",
    },
}
ROLLOUT_PARAMETER_SCHEMA = {
    "Action": {
        "type": "String", "allowedValues": ["install", "readback", "rollback"],
        "interpolationType": "ENV_VAR",
    },
    "SourceSha": {
        "type": "String", "allowedPattern": "^[0-9a-f]{40}$",
        "interpolationType": "ENV_VAR",
    },
    "WorkerSha256": {
        "type": "String", "allowedPattern": HASH_PATTERN,
        "interpolationType": "ENV_VAR",
    },
    "ValidatorSha256": {
        "type": "String", "allowedPattern": HASH_PATTERN,
        "interpolationType": "ENV_VAR",
    },
    "ShadowDocumentSha256": {
        "type": "String", "allowedPattern": HASH_PATTERN,
        "interpolationType": "ENV_VAR",
    },
    "RolloutAttemptId": {
        "type": "String", "allowedPattern": "^[1-9][0-9]{0,19}$",
        "interpolationType": "ENV_VAR",
    },
    "ExpectedInstanceId": {
        "type": "String", "allowedPattern": f"^{INSTANCE_ID}$",
        "interpolationType": "ENV_VAR",
    },
    "Region": {
        "type": "String", "allowedPattern": f"^{REGION}$",
        "interpolationType": "ENV_VAR",
    },
}


class ContractMismatch(Exception):
    """A supported project contract is present but inconsistent."""


class SetupError(Exception):
    """The checker input cannot be parsed unambiguously."""


class _UniqueLoader(yaml.SafeLoader):
    pass


# GitHub Actions uses YAML 1.2 booleans. PyYAML otherwise turns the key `on` into True.
for first, resolvers in list(_UniqueLoader.yaml_implicit_resolvers.items()):
    _UniqueLoader.yaml_implicit_resolvers[first] = [
        item for item in resolvers if item[0] != "tag:yaml.org,2002:bool"
    ]
_UniqueLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def _construct_unique_mapping(
    loader: _UniqueLoader, node: yaml.MappingNode, deep: bool = False,
) -> dict[object, object]:
    loader.flatten_mapping(node)
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in result
        except TypeError as error:
            raise SetupError("yaml.unhashable_key") from error
        if duplicate:
            raise SetupError("yaml.duplicate_key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping,
)


def _fail(category: str, *, setup: bool = False) -> NoReturn:
    prefix = "SETUP" if setup else "FAIL"
    print(f"{prefix} category={category}", file=sys.stderr)
    raise SystemExit(2 if setup else 1)


def _read(root: Path, path: Path, category: str) -> str:
    try:
        return (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise SetupError(category) from None


def _load_yaml(text: str, category: str) -> dict[str, Any]:
    try:
        value = yaml.load(text, Loader=_UniqueLoader)
    except SetupError as error:
        raise SetupError(f"{category}.{error}") from None
    except yaml.YAMLError:
        raise SetupError(f"{category}.yaml_malformed") from None
    if not isinstance(value, dict):
        raise SetupError(f"{category}.root_type")
    return value


def _mapping(value: object, category: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractMismatch(category)
    return value


def _steps(workflow: Mapping[str, Any], category: str) -> list[Mapping[str, Any]]:
    jobs = _mapping(workflow.get("jobs"), f"{category}.jobs")
    result: list[Mapping[str, Any]] = []
    job_ids: set[str] = set()
    step_ids: set[str] = set()
    for job_id, raw_job in jobs.items():
        if job_id in job_ids:
            raise ContractMismatch(f"{category}.job_duplicate")
        job_ids.add(job_id)
        job = _mapping(raw_job, f"{category}.job_shape")
        raw_steps = job.get("steps")
        if not isinstance(raw_steps, list):
            raise ContractMismatch(f"{category}.steps_shape")
        for raw_step in raw_steps:
            step = _mapping(raw_step, f"{category}.step_shape")
            step_id = step.get("id")
            if step_id is not None:
                if not isinstance(step_id, str) or step_id in step_ids:
                    raise ContractMismatch(f"{category}.step_id_duplicate")
                step_ids.add(step_id)
            if "run" in step and not isinstance(step["run"], str):
                raise ContractMismatch(f"{category}.run_type")
            result.append(step)
    return result


def _dispatch_inputs(workflow: Mapping[str, Any], category: str) -> Mapping[str, Any]:
    triggers = _mapping(workflow.get("on"), f"{category}.trigger")
    dispatch = _mapping(triggers.get("workflow_dispatch"), f"{category}.dispatch")
    return _mapping(dispatch.get("inputs"), f"{category}.dispatch_inputs")


def _run_steps_with(steps: list[Mapping[str, Any]], needle: str) -> list[Mapping[str, Any]]:
    return [
        step for step in steps
        if isinstance(step.get("run"), str) and needle in step["run"]
    ]


def _shell_unit(script: str, start: int, category: str) -> tuple[str, list[str]]:
    tail = script[start:]
    lines: list[str] = []
    for line in tail.splitlines():
        stripped = line.strip()
        lines.append(stripped[:-1].rstrip() if stripped.endswith("\\") else stripped)
        if not stripped.endswith("\\"):
            break
    raw = " ".join(lines)
    if raw.endswith(')"'):
        raw = raw[:-2]
    try:
        return raw, shlex.split(raw)
    except ValueError:
        raise ContractMismatch(category) from None


def _shell_command(script: str, executable: str, category: str) -> list[str]:
    start = script.find(executable)
    if start < 0:
        raise ContractMismatch(category)
    _raw, tokens = _shell_unit(script, start, category)
    for index, token in enumerate(tokens):
        if token.startswith(("<", ">")):
            return tokens[:index]
    return tokens


def _aws_cli_units(
    steps: list[Mapping[str, Any]], category: str,
) -> list[tuple[str, list[str]]]:
    result: list[tuple[str, list[str]]] = []
    for step in steps:
        script = step.get("run")
        if not isinstance(script, str):
            continue
        for match in re.finditer(r"(?<![A-Za-z0-9_-])aws(?=\s)", script):
            line_start = script.rfind("\n", 0, match.start()) + 1
            prefix = script[line_start:match.start()].strip()
            if prefix.startswith("#"):
                continue
            if prefix not in {"", 'command_id="$(', 'status="$('}:
                raise ContractMismatch(f"{category}.launcher")
            result.append(_shell_unit(script, match.start(), category))
    return result


def _verify_activation_launcher_surface(steps: list[Mapping[str, Any]]) -> None:
    """Reject shell indirection around the three literal activation AWS units."""
    for step in steps:
        script = step.get("run")
        if not isinstance(script, str):
            continue
        if re.search(
            r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*\s*=\s*"
            r"(?:['\"])?(?:aws|[^\s;'\"]*/aws)(?:['\"])?(?:\s|;|$)",
            script,
        ):
            raise ContractMismatch("activation.workflow.dynamic_aws_launcher")
        variable_launchers = re.finditer(
            r"(?m)^\s*(?:['\"])?\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
            r"(?P<plain>[A-Za-z_][A-Za-z0-9_]*))(?:['\"])?\s+",
            script,
        )
        if any(
            "AWS" in (match.group("braced") or match.group("plain")).upper()
            for match in variable_launchers
        ):
            raise ContractMismatch("activation.workflow.dynamic_aws_launcher")
        if re.search(
            r"(?m)^\s*(?:(?:command|env)(?:\s+[^\s;]+)*\s+)?"
            r"(?:/[^\s;]*/)?python(?:3(?:\.\d+)?)?\s+-m\s+awscli\b",
            script,
        ):
            raise ContractMismatch("activation.workflow.dynamic_aws_launcher")


def _shell_command_units(script: str) -> list[str]:
    """Split the supported shell subset at unquoted operators and comments."""
    units: list[str] = []
    normalized = re.sub(r"\\\n\s*", " ", script)
    for line in normalized.splitlines():
        current: list[str] = []
        quote = ""
        escaped = False
        for character in line:
            if escaped:
                current.append(character)
                escaped = False
                continue
            if character == "\\" and quote != "'":
                current.append(character)
                escaped = True
                continue
            if quote:
                current.append(character)
                if character == quote:
                    quote = ""
                continue
            if character in {"'", '"'}:
                quote = character
                current.append(character)
                continue
            if character == "#" and (
                not current or current[-1].isspace()
            ):
                break
            if character in ";&|":
                unit = "".join(current).strip()
                if unit:
                    units.append(unit)
                current = []
                continue
            current.append(character)
        unit = "".join(current).strip()
        if unit:
            units.append(unit)
    return units


def _shell_tokens(unit: str, category: str) -> list[str]:
    try:
        return shlex.split(unit, comments=False, posix=True)
    except ValueError:
        raise ContractMismatch(category) from None


def _protected_shell_writes(script: str, name: str) -> tuple[list[str], bool]:
    """Return direct assignments and command-unit protected writes."""
    assignments = re.findall(
        rf"(?:^|[;\n]\s*){re.escape(name)}=(?P<value>[^;\n]+)", script,
    )
    escaped = re.escape(name)
    name_token = re.compile(
        rf"^(?:\$\{{?)?{escaped}(?:\}})?(?:\[[^]]+\])?(?:=.*)?$",
    )
    write_commands = {
        "declare", "typeset", "local", "export", "readonly", "read",
        "mapfile", "readarray", "unset",
    }
    for unit in _shell_command_units(script):
        if not re.search(rf"\b{escaped}\b", unit):
            continue
        try:
            tokens = shlex.split(unit, comments=False, posix=True)
        except ValueError:
            return assignments, True
        if not tokens:
            continue
        command = tokens[0].rsplit("/", 1)[-1]
        if command == "printf" and "-v" in tokens:
            position = tokens.index("-v")
            if position + 1 < len(tokens) and name_token.fullmatch(
                tokens[position + 1],
            ):
                return assignments, True
        if command in write_commands and any(
            name_token.fullmatch(token) for token in tokens[1:]
        ):
            return assignments, True
        if command == "eval" or (
            command == "let" and re.search(rf"\b{escaped}\s*=", unit)
        ):
            return assignments, True
        if re.search(rf"\$\{{!{escaped}\}}|\(\(\s*{escaped}\s*=", unit):
            return assignments, True
    return assignments, False


def _command_flags(
    tokens: list[str], prefix: list[str], category: str,
) -> tuple[dict[str, str], list[str]]:
    command: list[str] = []
    suffix: list[str] = []
    for token in tokens:
        if token.startswith(("<", ">", "2>")) or token in {"||", "&&", ";"}:
            suffix = tokens[len(command):]
            break
        command.append(token)
    return _flags(command, prefix, category), suffix


def _flags(tokens: list[str], prefix: list[str], category: str) -> dict[str, str]:
    if tokens[:len(prefix)] != prefix:
        raise ContractMismatch(category)
    result: dict[str, str] = {}
    index = len(prefix)
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("--"):
            raise ContractMismatch(category)
        if "=" in token:
            flag, value = token.split("=", 1)
            index += 1
        else:
            if index + 1 >= len(tokens):
                raise ContractMismatch(category)
            flag, value = token, tokens[index + 1]
            index += 2
        if flag in result or value.startswith("--"):
            raise ContractMismatch(category)
        result[flag] = value
    return result


def _parameter_mapping(raw: str, category: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in raw.split(","):
        if item.count("=") != 1:
            raise ContractMismatch(category)
        key, value = item.split("=", 1)
        match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value)
        if not key or match is None or key in result:
            suffix = "parameter_duplicate" if key in result else "parameters_malformed"
            raise ContractMismatch(f"activation.workflow.{suffix}")
        result[key] = match.group(1)
    return result


def _verify_activation_workflow(workflow: Mapping[str, Any]) -> None:
    expected_inputs = {
        "desired_state", "source_sha", "image_digest", "build_run_id",
        "compose_shadow_sha256", "activation_id", "worker_sha256",
        "validator_sha256", "shadow_document_sha256", "status_notification",
    }
    dispatch_inputs = _dispatch_inputs(workflow, "activation.workflow")
    if set(dispatch_inputs) != expected_inputs:
        raise ContractMismatch("activation.workflow.dispatch_input_set")
    if dispatch_inputs.get("status_notification") != {
        "description": "Protected control-plane status notification",
        "required": True,
        "default": "slack",
        "type": "choice",
        "options": ["slack", "disabled"],
    }:
        raise ContractMismatch("activation.workflow.notification_input")
    env = _mapping(workflow.get("env"), "activation.workflow.global_env")
    if env != {
        "AWS_REGION": REGION,
        "EC2_INSTANCE_ID": INSTANCE_ID,
        "SHADOW_DOCUMENT_NAME": ACTIVATION_DOCUMENT_NAME,
        "EVIDENCE_FILENAME": "shadow-worker-evidence.json",
        "DIAGNOSTIC_FILENAME": "shadow-worker-diagnostic.json",
        "NOTIFICATION_RECEIPT_FILENAME": "shadow-status-notification.json",
    }:
        raise ContractMismatch("activation.workflow.global_env")
    steps = _steps(workflow, "activation.workflow")
    names = [step.get("name") for step in steps]
    expected_notification_steps = {
        "Preflight protected Slack status boundary",
        "Notify protected shadow status",
        "Clear OIDC credentials before evidence upload",
        "Upload bounded shadow evidence",
    }
    if not expected_notification_steps.issubset(set(names)):
        raise ContractMismatch("activation.workflow.notification_steps")
    preflight_index = names.index("Preflight protected Slack status boundary")
    oidc_index = names.index("Configure exact shadow activation role with OIDC")
    clear_index = names.index("Clear OIDC credentials before evidence upload")
    notify_index = names.index("Notify protected shadow status")
    upload_index = names.index("Upload bounded shadow evidence")
    if not (preflight_index < oidc_index < clear_index < notify_index < upload_index):
        raise ContractMismatch("activation.workflow.notification_order")
    notification_steps = [
        step for step in steps if step.get("name") in expected_notification_steps
    ]
    secret_reference = "${{ secrets.KIWOOM_SHADOW_SLACK_WEBHOOK_URL }}"
    for notification_name in (
        "Preflight protected Slack status boundary",
        "Notify protected shadow status",
    ):
        notification_step = next(
            step for step in notification_steps
            if step.get("name") == notification_name
        )
        if notification_step.get("env") != {
            "KIWOOM_SHADOW_SLACK_WEBHOOK_URL": secret_reference,
        }:
            raise ContractMismatch("activation.workflow.notification_secret")
    workflow_text = json.dumps(workflow, sort_keys=True)
    if (
        workflow_text.count("secrets.KIWOOM_SHADOW_SLACK_WEBHOOK_URL") != 2
        or "secrets.CONFIG_JSON" in workflow_text
        or "secrets.STRATEGY_CONFIG_JSON" in workflow_text
    ):
        raise ContractMismatch("activation.workflow.notification_secret_scope")
    aws_units = _aws_cli_units(steps, "activation.workflow.aws_command")
    _verify_activation_launcher_surface(steps)
    if len(aws_units) != 3:
        raise ContractMismatch("activation.workflow.aws_command_allowlist")
    candidates = _run_steps_with(steps, "aws ssm send-command")
    if len(candidates) != 1:
        raise ContractMismatch("activation.workflow.execute_unit")
    step = candidates[0]
    step_env = _mapping(step.get("env"), "activation.workflow.execute_env")
    expected_env = {
        "AWS_ACCESS_KEY_ID": "${{ steps.oidc.outputs.aws-access-key-id }}",
        "AWS_SECRET_ACCESS_KEY": "${{ steps.oidc.outputs.aws-secret-access-key }}",
        "AWS_SESSION_TOKEN": "${{ steps.oidc.outputs.aws-session-token }}",
        **ACTIVATION_INPUT_ENV,
        "PYTHONPATH": "${{ github.workspace }}/src",
    }
    if step_env != expected_env:
        raise ContractMismatch("activation.workflow.execute_env")
    script = step.get("run")
    if not isinstance(script, str):
        raise ContractMismatch("activation.workflow.execute_run")
    tokens = aws_units[0][1]
    flags = _flags(tokens, ["aws", "ssm", "send-command"], "activation.workflow.send_flags")
    expected_flags = {
        "--document-name": "${SHADOW_DOCUMENT_NAME}",
        "--document-version": "${document_version}",
        "--instance-ids": "${EC2_INSTANCE_ID}",
        "--parameters": flags.get("--parameters", ""),
        "--comment": "kiwoom shadow activation ${ACTIVATION_ID}",
        "--timeout-seconds": "1020", "--max-concurrency": "1", "--max-errors": "0",
        "--query": "Command.CommandId", "--output": "text",
    }
    if flags != expected_flags:
        raise ContractMismatch("activation.workflow.send_flags")
    poll_flags, poll_suffix = _command_flags(
        aws_units[1][1], ["aws", "ssm", "get-command-invocation"],
        "activation.workflow.poll_flags",
    )
    if poll_flags != {
        "--command-id": "${command_id}",
        "--instance-id": "${EC2_INSTANCE_ID}",
        "--query": "Status", "--output": "text",
    } or poll_suffix != ["2>/dev/null", "||", "true"]:
        raise ContractMismatch("activation.workflow.poll_flags")
    final_flags, final_suffix = _command_flags(
        aws_units[2][1], ["aws", "ssm", "get-command-invocation"],
        "activation.workflow.final_invocation_flags",
    )
    if final_flags != {
        "--command-id": "${command_id}",
        "--instance-id": "${EC2_INSTANCE_ID}", "--output": "json",
    } or final_suffix != [">${invocation_file}"]:
        raise ContractMismatch("activation.workflow.final_invocation_flags")
    parameters = _parameter_mapping(flags["--parameters"], "activation.workflow.parameters")
    if set(parameters) != set(ACTIVATION_PARAMETER_ENV):
        raise ContractMismatch("activation.workflow.parameter_set")
    if parameters != ACTIVATION_PARAMETER_ENV:
        raise ContractMismatch("activation.workflow.parameter_mapping")
    if not re.search(
        r'if\s+\[\[\s+"\$\{DESIRED_STATE\}"\s+==\s+stop\s+\]\];\s+then.*?'
        r'ssm_compose_hash="\$\(printf\s+\'0%\.0s\'\s+\{1\.\.64\}\)".*?else.*?'
        r'ssm_compose_hash="\$\(sha256sum\s+compose\.shadow\.yaml\s+\|\s+cut\s+-d\'\s\'\s+-f1\)".*?fi',
        script, re.DOTALL,
    ):
        raise ContractMismatch("activation.workflow.compose_hash_branches")
    attestation = re.findall(
        r'^\s*document_version="\$\(python3 -c \'import os; from '
        r'kiwoom_stock\.deployment\.shadow_rollout import '
        r'attest_activation_document; print\(attest_activation_document\('
        r'os\.environ\["SHADOW_DOCUMENT_SHA256"\]\)\)\'\)"\s*$',
        script, re.MULTILINE,
    )
    assignments, indirect_write = _protected_shell_writes(
        script, "document_version",
    )
    if len(attestation) != 1 or len(assignments) != 1 or indirect_write or len(re.findall(
        r'^\s*\[\[ "\$\{document_version\}" =~ \^\[1-9\]\[0-9\]\*\$ \]\]\s*$',
        script, re.MULTILINE,
    )) != 1:
        raise ContractMismatch("activation.workflow.document_attestation")
    send_position = script.find("aws ssm send-command")
    if script.find("document_version=") >= send_position:
        raise ContractMismatch("activation.workflow.document_attestation")
    terminal_matches = re.findall(r"Success\|Failed\|Cancelled\|TimedOut(?:\|Cancelling)?", script)
    if terminal_matches != ["Success|Failed|Cancelled|TimedOut"]:
        raise ContractMismatch("activation.workflow.terminal_statuses")
    validator_tokens = _shell_command(
        script, "python3 deploy/ec2/shadow_runtime_evidence.py",
        "activation.workflow.validator_command",
    )
    validator_flags = _flags(
        validator_tokens, ["python3", "deploy/ec2/shadow_runtime_evidence.py"],
        "activation.workflow.validator_flags",
    )
    if validator_flags != {
        "--mode": "${validator_mode}", "--event": "${validator_event}",
        "--source-sha": "${SOURCE_SHA}",
        "--image-digest": "${IMAGE_DIGEST}",
        "--activation-id": "${ACTIVATION_ID}",
        "--input-format": "ssm-invocation", "--output": "activation-summary",
    } or not re.search(
        r'--output\s+activation-summary\s+<"\$\{invocation_file\}"\)"',
        script,
    ):
        raise ContractMismatch("activation.workflow.validator_wiring")


def _document_step(document: Mapping[str, Any], category: str) -> tuple[Mapping[str, Any], str]:
    if document.get("schemaVersion") != "2.2":
        raise ContractMismatch(f"{category}.schema_version")
    steps = document.get("mainSteps")
    if not isinstance(steps, list) or len(steps) != 1:
        raise ContractMismatch(f"{category}.step_count")
    step = _mapping(steps[0], f"{category}.step_shape")
    if step.get("action") != "aws:runShellScript":
        raise ContractMismatch(f"{category}.action")
    if step.get("precondition") != {"StringEquals": ["platformType", "Linux"]}:
        raise ContractMismatch(f"{category}.precondition")
    inputs = _mapping(step.get("inputs"), f"{category}.inputs")
    commands = inputs.get("runCommand")
    if not isinstance(commands, list) or len(commands) != 1 or not isinstance(commands[0], str):
        raise ContractMismatch(f"{category}.run_command")
    return inputs, commands[0]


def _worker_usage_contract(worker: str) -> tuple[list[str], list[str], set[str]]:
    main = re.search(r"(?ms)^main\(\) \{(?P<body>.*?)^\}", worker)
    if main is None:
        raise ContractMismatch("activation.worker.main")
    parser_matches = re.findall(
        r'(?m)^\s+(--[a-z0-9-]+)\)\s+'
        r'([a-z_][a-z0-9_]*)="\$\{2:-\}";\s+shift 2\s+;;$',
        main.group("body"),
    )
    parser_mapping = dict(parser_matches)
    expected_parser_mapping = {
        "--image": "image", "--source-sha": "source_sha",
        "--activation-id": "activation_id",
        "--compose-shadow-sha256": "compose_hash",
        "--expected-worker-sha256": "expected_worker_hash",
        "--expected-validator-sha256": "expected_validator_hash",
        "--expected-shadow-document-sha256": "expected_document_hash",
        "--inherited-lock-fd": "inherited_lock_fd",
        "--expected-instance-id": "expected_instance", "--region": "region",
        "--desired-state": "desired_state",
    }
    if len(parser_matches) != len(parser_mapping) or (
        parser_mapping != expected_parser_mapping
    ):
        raise ContractMismatch("activation.worker.parser_mapping")
    stop_branch = re.search(
        r'(?ms)^\s*if \[\[ "\$\{desired_state\}" == stop \]\]; then\n'
        r'(?P<body>.*?)^\s*fi\n(?P<active>.*)$',
        main.group("body"),
    )
    if stop_branch is None:
        raise ContractMismatch("activation.worker.mode_guards")
    stop_body = stop_branch.group("body")
    if (
        main.group("body").count('validate_hash "${compose_hash}"') != 1
        or stop_body.count("${compose_hash}") != 1
        or '[[ -z "${compose_hash}" ]] || fail "stop does not accept a Compose hash"'
        not in stop_body
        or 'return 0' not in stop_body
        or stop_branch.group("active").count('validate_hash "${compose_hash}"') != 1
    ):
        raise ContractMismatch("activation.worker.mode_guards")
    usage = re.search(r"(?ms)^usage\(\) \{.*?<<'EOF'\n(?P<body>.*?)\nEOF", worker)
    if usage is None:
        raise ContractMismatch("activation.worker.usage")
    flattened = re.sub(r"\\\n\s*", " ", usage.group("body"))
    commands = re.findall(r"(?m)^\s*kiwoom-shadow-worker\s+.*$", flattened)
    if len(commands) != 2:
        raise ContractMismatch("activation.worker.usage")
    try:
        active, stop = (shlex.split(command.strip()) for command in commands)
    except ValueError:
        raise ContractMismatch("activation.worker.usage") from None
    return active, stop, set(parser_mapping)


def _branch_argv(command: str) -> tuple[list[str], list[str]]:
    match = re.fullmatch(
        r'\s*exec\s+9>/run/lock/kiwoom-stock-shadow\.lock;\s*'
        r'flock\s+-x\s+-w\s+240\s+9\s+\|\|\s+exit\s+75;\s*'
        r'if\s+\[\s+"\$SSM_DesiredState"\s+=\s+oneshot\s+\]\s+\|\|\s+'
        r'\[\s+"\$SSM_DesiredState"\s+=\s+continuous\s+\];\s*'
        r'then\s+exec\s+(?P<active>.*?);\s*elif\s+\[\s+"\$SSM_DesiredState"\s+=\s+stop\s+\];\s*'
        r'then\s+exec\s+(?P<stop>.*?);\s*else\s+exit\s+64;\s*fi\s*',
        command,
    )
    if match is None:
        raise ContractMismatch("activation.document.worker_branches")
    try:
        return shlex.split(match.group("active")), shlex.split(match.group("stop"))
    except ValueError:
        raise ContractMismatch("activation.document.worker_argv") from None


def _flag_names(tokens: list[str]) -> list[str]:
    return [token for token in tokens if token.startswith("--")]


def _verify_activation_document(document: Mapping[str, Any], worker: str) -> None:
    if document.get("description") != "Start or stop bounded market-only Kiwoom shadow execution":
        raise ContractMismatch("activation.document.description")
    if document.get("parameters") != ACTIVATION_PARAMETER_SCHEMA:
        raise ContractMismatch("activation.document.parameter_schema")
    inputs, command = _document_step(document, "activation.document")
    if inputs.get("timeoutSeconds") != "1020":
        raise ContractMismatch("activation.document.timeout")
    references = set(re.findall(r"\$SSM_([A-Za-z][A-Za-z0-9]*)\b", command))
    if references != set(ACTIVATION_PARAMETER_SCHEMA):
        raise ContractMismatch("activation.document.ssm_reference_set")
    active, stop = _branch_argv(command)
    usage_active, usage_stop, parser_flags = _worker_usage_contract(worker)
    document_flags = set(_flag_names(active)) | set(_flag_names(stop))
    if document_flags != parser_flags:
        raise ContractMismatch("activation.worker.parser_linkage")
    if _flag_names(active) != ["--inherited-lock-fd", *_flag_names(usage_active)] or (
        _flag_names(stop) != ["--inherited-lock-fd", *_flag_names(usage_stop)]
    ):
        raise ContractMismatch("activation.document.worker_argv")
    active_mapping = _flags(active, ["/usr/local/sbin/kiwoom-shadow-worker"], "activation.document.worker_argv")
    stop_mapping = _flags(stop, ["/usr/local/sbin/kiwoom-shadow-worker"], "activation.document.worker_argv")
    expected_common = {
        "--inherited-lock-fd": "9", "--image": "$SSM_ImageDigest",
        "--source-sha": "$SSM_SourceSha", "--activation-id": "$SSM_ActivationId",
        "--expected-worker-sha256": "$SSM_ExpectedWorkerSha256",
        "--expected-validator-sha256": "$SSM_ExpectedValidatorSha256",
        "--expected-shadow-document-sha256": "$SSM_ExpectedShadowDocumentSha256",
        "--expected-instance-id": "$SSM_ExpectedInstanceId", "--region": "$SSM_Region",
    }
    if active_mapping != {
        "--inherited-lock-fd": "9", "--desired-state": "$SSM_DesiredState",
        "--image": "$SSM_ImageDigest", "--source-sha": "$SSM_SourceSha",
        "--activation-id": "$SSM_ActivationId",
        "--compose-shadow-sha256": "$SSM_ComposeShadowSha256",
        **{key: value for key, value in expected_common.items() if key != "--inherited-lock-fd"},
    } or stop_mapping != {"--desired-state": "stop", **expected_common}:
        raise ContractMismatch("activation.document.worker_argv")


def _verify_validator(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise SetupError("activation.validator.python_malformed") from None
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    name_counts = Counter(node.name for node in functions)
    if any(count != 1 for count in name_counts.values()):
        raise ContractMismatch("activation.validator.duplicate_function")
    records_matches = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_records"
    ]
    authority_stores = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Store)
        and node.id == "_records"
    ]
    authority_arguments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.arg) and node.arg == "_records"
    ]
    authority_imports = [
        alias for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if (alias.asname or alias.name.rsplit(".", 1)[-1]) == "_records"
    ]
    authority_named_bindings = [
        node for node in ast.walk(tree)
        if (
            isinstance(node, ast.ClassDef) and node.name == "_records"
        ) or (
            isinstance(node, ast.ExceptHandler) and node.name == "_records"
        ) or (
            isinstance(node, (ast.MatchAs, ast.MatchStar))
            and node.name == "_records"
        )
    ]
    if (
        len(records_matches) != 1
        or records_matches[0] not in tree.body
        or authority_stores
        or authority_arguments
        or authority_imports
        or authority_named_bindings
    ):
        raise ContractMismatch("activation.validator.authority_binding")
    records = records_matches[0]
    body = records.body
    expected = [
        "status = invocation.get('Status')",
        "if type(status) is not str or status != 'Success':\n"
        "    raise EvidenceError('invocation_status_invalid')",
        "response_code = invocation.get('ResponseCode')",
        "if type(response_code) is not int or response_code != 0:\n"
        "    raise EvidenceError('invocation_response_code_invalid')",
    ]
    if len(body) != 11 or [ast.unparse(node) for node in body[3:7]] != expected:
        raise ContractMismatch("activation.validator.ssm_result")
    if ast.unparse(body[-1]) != (
        "return (_json_lines(stdout), {'ssm_status': status, "
        "'ssm_response_code': response_code})"
    ):
        raise ContractMismatch("activation.validator.ssm_result")


def _literal_assignments(tree: ast.Module) -> dict[str, object]:
    result: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                result[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
    return result


def _expr_signature(node: ast.AST) -> str:
    return ast.dump(node, annotate_fields=False, include_attributes=False)


def _source_expr_signature(source: str) -> str:
    return _expr_signature(ast.parse(source, mode="eval").body)


def _verify_rollout_executor(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        raise SetupError("rollout.executor.python_malformed") from None
    assignments = _literal_assignments(tree)
    expected_assignments = {
        "REGION": REGION, "INSTANCE_ID": INSTANCE_ID,
        "ROLLOUT_DOCUMENT": ROLLOUT_DOCUMENT_NAME,
        "SHADOW_DOCUMENT": ACTIVATION_DOCUMENT_NAME,
    }
    if any(assignments.get(key) != value for key, value in expected_assignments.items()):
        raise ContractMismatch("rollout.executor.fixed_target")
    path_values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == "Path" and len(node.value.args) == 1:
            try:
                path_values[node.targets[0].id] = ast.literal_eval(node.value.args[0])
            except (ValueError, TypeError):
                pass
    if path_values.get("WORKER_PATH") != WORKER.as_posix() or (
        path_values.get("VALIDATOR_PATH") != VALIDATOR.as_posix()
    ) or path_values.get("SHADOW_DOCUMENT_PATH") != ACTIVATION_DOCUMENT.as_posix() or (
        path_values.get("ROLLOUT_DOCUMENT_PATH") != ROLLOUT_DOCUMENT.as_posix()
    ):
        raise ContractMismatch("rollout.executor.source_paths")
    terminal = next(
        (node.value for node in tree.body if isinstance(node, ast.Assign)
         and any(isinstance(target, ast.Name) and target.id == "TERMINAL_STATUSES" for target in node.targets)),
        None,
    )
    if not isinstance(terminal, ast.Call) or not terminal.args:
        raise ContractMismatch("rollout.executor.terminal_statuses")
    try:
        terminal_values = set(ast.literal_eval(terminal.args[0]))
    except (ValueError, TypeError):
        terminal_values = set()
    if terminal_values != TERMINAL_STATUSES:
        raise ContractMismatch("rollout.executor.terminal_statuses")
    function = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_rollout_parameters"),
        None,
    )
    returns = [node.value for node in ast.walk(function) if isinstance(node, ast.Return)] if function else []
    if len(returns) != 1 or not isinstance(returns[0], ast.Dict):
        raise ContractMismatch("rollout.executor.parameter_mapping")
    parameter_dict = cast(ast.Dict, returns[0])
    if any(key is None for key in parameter_dict.keys):
        raise ContractMismatch("rollout.executor.parameter_mapping")
    try:
        keys = [
            ast.literal_eval(cast(ast.expr, key))
            for key in parameter_dict.keys
        ]
    except (ValueError, TypeError):
        raise ContractMismatch("rollout.executor.parameter_key") from None
    if not all(isinstance(key, str) for key in keys):
        raise ContractMismatch("rollout.executor.parameter_key")
    if set(keys) != ROLLOUT_PARAMETER_NAMES or len(keys) != len(set(keys)):
        raise ContractMismatch("rollout.executor.parameter_mapping")
    expected_signatures = {
        "Action": "List([Name('action', Load())], Load())",
        "SourceSha": "List([Attribute(Name('rollout', Load()), 'source_sha', Load())], Load())",
        "WorkerSha256": "List([Attribute(Name('rollout', Load()), 'worker_sha256', Load())], Load())",
        "ValidatorSha256": "List([Attribute(Name('rollout', Load()), 'validator_sha256', Load())], Load())",
        "ShadowDocumentSha256": "List([Attribute(Name('rollout', Load()), 'shadow_document_sha256', Load())], Load())",
        "RolloutAttemptId": "List([Attribute(Name('rollout', Load()), 'rollout_attempt_id', Load())], Load())",
        "ExpectedInstanceId": "List([Name('INSTANCE_ID', Load())], Load())",
        "Region": "List([Name('REGION', Load())], Load())",
    }
    actual_signatures = {key: _expr_signature(value) for key, value in zip(keys, parameter_dict.values)}
    if actual_signatures != expected_signatures:
        raise ContractMismatch("rollout.executor.parameter_mapping")
    scoped_functions: dict[str, ast.FunctionDef] = {}
    for top_level in tree.body:
        if isinstance(top_level, ast.FunctionDef):
            scoped_functions[top_level.name] = top_level
        elif isinstance(top_level, ast.ClassDef):
            for member in top_level.body:
                if isinstance(member, ast.FunctionDef):
                    scoped_functions[f"{top_level.name}.{member.name}"] = member
    actual_calls: Counter[tuple[str, str, str, str]] = Counter()
    for scope, scoped_function in scoped_functions.items():
        for call in ast.walk(scoped_function):
            if (
                not isinstance(call, ast.Call)
                or not isinstance(call.func, ast.Attribute)
                or call.func.attr != "call"
                or not isinstance(call.func.value, ast.Name)
                or call.func.value.id not in {"aws", "self"}
            ):
                continue
            if len(call.args) != 1 or any(
                keyword.arg != "write" for keyword in call.keywords
            ) or len(call.keywords) > 1:
                raise ContractMismatch("rollout.executor.call_allowlist")
            write = "read" if not call.keywords else ast.unparse(call.keywords[0].value)
            actual_calls[(
                scope, ast.unparse(call.func), ast.unparse(call.args[0]), write,
            )] += 1
    expected_calls = Counter({
        (
            "AwsCli.send", "self.call",
            "['ssm', 'send-command', '--document-name', ROLLOUT_DOCUMENT, "
            "'--document-version', rollout.rollout_document_version, "
            "'--instance-ids', INSTANCE_ID, "
            "'--comment', comment, '--parameters', json.dumps(parameters, "
            "separators=(',', ':')), '--timeout-seconds', '300', "
            "'--max-concurrency', '1', '--max-errors', '0']", "True",
        ): 1,
        ("AwsCli._acceptance_commands", "self.call", "args", "read"): 1,
        (
            "AwsCli._acceptance_invocation_exists", "self.call",
            "['ssm', 'list-command-invocations', '--command-id', command_id, "
            "'--instance-id', INSTANCE_ID, '--details', '--max-results', "
            "str(LEGACY_HISTORY_PAGE_SIZE), '--no-paginate']", "read",
        ): 1,
        (
            "AwsCli._attest_rollout_version_unchanged", "self.call",
            "['ssm', 'describe-document', '--name', ROLLOUT_DOCUMENT]", "read",
        ): 1,
        (
            "AwsCli.poll", "self.call",
            "['ssm', 'get-command-invocation', '--command-id', command_id, "
            "'--instance-id', INSTANCE_ID]", "read",
        ): 1,
        (
            "attest_activation_document", "aws.call",
            "['ssm', 'describe-document', '--name', SHADOW_DOCUMENT]", "read",
        ): 1,
        (
            "attest_activation_document", "aws.call",
            "['ssm', 'get-document', '--name', SHADOW_DOCUMENT, "
            "'--document-version', default, '--document-format', 'JSON']", "read",
        ): 1,
        (
            "attest_rollout_document", "aws.call",
            "['ssm', 'describe-document', '--name', ROLLOUT_DOCUMENT]", "read",
        ): 1,
        (
            "attest_rollout_document", "aws.call",
            "['ssm', 'get-document', '--name', ROLLOUT_DOCUMENT, "
            "'--document-version', default, '--document-format', 'JSON']", "read",
        ): 1,
        ("_scan_legacy_commands", "aws.call", "args", "read"): 1,
        ("_scan_legacy_invocations", "aws.call", "args", "read"): 1,
        (
            "set_default_reconciled", "aws.call",
            "['ssm', 'update-document-default-version', '--name', "
            "SHADOW_DOCUMENT, '--document-version', version]", "True",
        ): 1,
        (
            "set_default_reconciled", "aws.call",
            "['ssm', 'describe-document', '--name', SHADOW_DOCUMENT]", "read",
        ): 1,
        (
            "execute", "aws.call",
            "['ssm', 'describe-document', '--name', SHADOW_DOCUMENT]", "read",
        ): 3,
        (
            "execute", "aws.call",
            "['ssm', 'list-document-versions', '--name', SHADOW_DOCUMENT]", "read",
        ): 2,
        (
            "execute", "aws.call",
            "['ssm', 'get-document', '--name', SHADOW_DOCUMENT, "
            "'--document-version', previous_default, '--document-format', 'JSON']",
            "read",
        ): 1,
        (
            "execute", "aws.call",
            "['ssm', 'update-document', '--name', SHADOW_DOCUMENT, "
            "'--document-version', '$LATEST', '--document-format', 'JSON', "
            "'--content', shadow_canonical.decode('utf-8')]", "True",
        ): 1,
        (
            "execute", "aws.call",
            "['ssm', 'get-document', '--name', SHADOW_DOCUMENT, "
            "'--document-version', candidate, '--document-format', 'JSON']", "read",
        ): 1,
        (
            "execute", "aws.call",
            "['ssm', 'get-document', '--name', SHADOW_DOCUMENT, "
            "'--document-version', new_version, '--document-format', 'JSON']", "read",
        ): 1,
    })
    if actual_calls != expected_calls:
        raise ContractMismatch("rollout.executor.call_allowlist")

    dynamic_args = {
        "AwsCli._acceptance_commands": (
            "['ssm', 'list-commands', '--instance-id', INSTANCE_ID, '--filters', "
            "f'key=DocumentName,value={ROLLOUT_DOCUMENT}', '--max-results', "
            "str(ACCEPTANCE_HISTORY_PAGE_SIZE), '--no-paginate']"
        ),
        "_scan_legacy_commands": (
            "['ssm', 'list-commands', '--instance-id', INSTANCE_ID, '--filters', "
            "f'key=DocumentName,value={SHADOW_DOCUMENT}', '--max-results', "
            "str(LEGACY_HISTORY_PAGE_SIZE), '--no-paginate']"
        ),
        "_scan_legacy_invocations": (
            "['ssm', 'list-command-invocations', '--instance-id', INSTANCE_ID, "
            "'--filters', f'key=DocumentName,value={SHADOW_DOCUMENT}', "
            "'--no-details', '--max-results', str(LEGACY_HISTORY_PAGE_SIZE), "
            "'--no-paginate']"
        ),
    }
    for scope, expected_args in dynamic_args.items():
        scoped_function = scoped_functions[scope]
        definitions = [
            node for node in ast.walk(scoped_function)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "args"
        ]
        extensions = [
            node for node in ast.walk(scoped_function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "args"
            and node.func.attr == "extend"
        ]
        if (
            len(definitions) != 1
            or ast.unparse(definitions[0].value) != expected_args
            or len(extensions) != 1
            or len(extensions[0].args) != 1
            or ast.unparse(extensions[0].args[0])
            != "['--next-token', next_token]"
        ):
            raise ContractMismatch("rollout.executor.dynamic_read_allowlist")

    call_method = scoped_functions.get("AwsCli.call")
    if call_method is None or [ast.unparse(node) for node in call_method.body[:2]] != [
        "classified_write = _classify_aws_command(args)",
        "if type(write) is not bool or write is not classified_write:\n"
        "    raise RolloutError('aws_command_write_mismatch')",
    ]:
        raise ContractMismatch("rollout.executor.runtime_classifier")
    subprocess_imports = [
        alias for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names if alias.name == "subprocess"
    ]
    os_imports = [
        alias for node in tree.body if isinstance(node, ast.Import)
        for alias in node.names if alias.name == "os"
    ]
    subprocess_from_imports = [
        node for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess"
    ]
    imported_process_callables = [
        alias for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module in {"os", "asyncio", "pty"}
        for alias in node.names
        if (
            node.module == "os"
            and re.fullmatch(
                r"system|popen|exec.*|spawn.*|posix_spawn.*", alias.name,
            )
        ) or (
            node.module == "asyncio"
            and alias.name in {"create_subprocess_exec", "create_subprocess_shell"}
        ) or (node.module == "pty" and alias.name == "spawn")
    ]
    protected_module_names = {"os", "subprocess"}
    protected_module_stores = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, (ast.Store, ast.Del))
        and node.id in protected_module_names
    ]
    protected_module_arguments = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.arg) and node.arg in protected_module_names
    ]
    protected_module_named_bindings = [
        node for node in ast.walk(tree)
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name in protected_module_names
        ) or (
            isinstance(node, ast.ExceptHandler)
            and node.name in protected_module_names
        ) or (
            isinstance(node, (ast.MatchAs, ast.MatchStar))
            and node.name in protected_module_names
        )
    ]
    protected_module_import_rebindings = [
        alias for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
        if (alias.asname or alias.name.rsplit(".", 1)[-1])
        in protected_module_names
        and not (
            isinstance(node, ast.Import)
            and alias.asname is None
            and alias.name in protected_module_names
        )
    ]
    subprocess_attributes = Counter(
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.value, ast.Name)
        and node.value.id == "subprocess"
    )
    subprocess_loads = sum(
        1 for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and node.id == "subprocess"
    )
    forbidden_os_process = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
        and re.fullmatch(r"system|popen|exec.*|spawn.*|posix_spawn.*", node.attr)
    ]
    forbidden_other_process = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (
            node.value.id == "asyncio"
            and node.attr in {"create_subprocess_exec", "create_subprocess_shell"}
            or node.value.id == "pty" and node.attr == "spawn"
        )
    ]
    dynamic_process = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and (
            node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in {"subprocess", "os", "asyncio", "pty"}
            or node.func.id == "__import__"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in {"subprocess", "os", "asyncio", "pty"}
        )
    ]
    process_calls = [
        call for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "subprocess"
        and call.func.attr == "run"
    ]
    process_call = process_calls[0] if len(process_calls) == 1 else None
    call_keywords = {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in process_call.keywords
        if keyword.arg is not None
    } if process_call is not None else {}
    if (
        len(subprocess_imports) != 1
        or subprocess_imports[0].asname is not None
        or len(os_imports) != 1
        or os_imports[0].asname is not None
        or subprocess_from_imports
        or imported_process_callables
        or protected_module_stores
        or protected_module_arguments
        or protected_module_named_bindings
        or protected_module_import_rebindings
        or subprocess_attributes != Counter({"run": 1, "TimeoutExpired": 1})
        or subprocess_loads != 2
        or forbidden_os_process
        or forbidden_other_process
        or dynamic_process
        or process_call is None
        or not process_call.args
        or ast.unparse(process_call.args[0]) != "['aws', *args, '--output', 'json']"
        or call_keywords != {
            "env": "env", "check": "False", "capture_output": "True",
            "text": "True", "timeout": "min(60.0, remaining)",
        }
        or not any(
            process_call in ast.walk(node)
            for node in [scoped_functions.get("AwsCli.call")]
            if node is not None
        )
    ):
        raise ContractMismatch("rollout.executor.process_authority")
    main = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"), None,
    )
    parser_flags = {
        ast.literal_eval(call.args[0])
        for call in ast.walk(main) if isinstance(call, ast.Call) and call.args
        and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"
        and isinstance(call.args[0], ast.Constant)
        and any(keyword.arg == "required" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in call.keywords)
    } if main else set()
    if parser_flags != {"--source-sha", "--rollout-attempt-id", "--audit"}:
        raise ContractMismatch("rollout.executor.cli")


def _verify_rollout_migration(source: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ContractMismatch("rollout.migration.syntax") from error
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    classes = {
        node.name: node for node in tree.body if isinstance(node, ast.ClassDef)
    }
    required_functions = {
        "_classify_admin_command", "approved_sources", "_put_lock",
        "_release_lock", "_release_failed_safe", "_settle_candidate",
        "_verify_candidate_binding",
        "_valid_audit", "_validate_journal", "_verify_cutover_state",
        "_update_response_version", "execute", "main",
    }
    if (
        not required_functions <= set(functions)
        or not {"Deadline", "AdminAwsCli", "RemoteJournal"} <= set(classes)
    ):
        raise ContractMismatch("rollout.migration.positive_contract")
    required_literals = (
        'LOCK_PARAMETER = "/kiwoom-stock/shadow-rollout-document-migration/lock"',
        'JOURNAL_PREFIX = "/kiwoom-stock/shadow-rollout-document-migration/attempts/"',
        '"--version-name", version_name, "--content", source_text',
        '"--version-name", approved_version_name, "--content", approved_content',
        '"update_submitting",',
        '"cutover_submitting",',
        '"attempt_created",',
        '"cutover_uncertain_no_cas"',
        'status="MANUAL_HOLD"',
        '"status", "--porcelain", "--untracked-files=all"',
        'command == ("sts", "get-caller-identity")',
        'TERMINAL_RESERVE_SECONDS = 120.0',
        'TERMINAL_PHASES = frozenset({"complete", "failed_safe", "manual_hold"})',
    )
    if any(value not in source for value in required_literals):
        raise ContractMismatch("rollout.migration.positive_contract")
    forbidden = (
        "create-document", "delete-document", "send-command",
        "ec2", "--profile", "shell=True", "Overwrite=true",
    )
    if (
        any(value in source for value in forbidden)
        or "rollback" in source
        or source.count("file://") < 2
    ):
        raise ContractMismatch("rollout.migration.forbidden_authority")

    failed_safe_release = functions["_release_failed_safe"]
    release_calls = sorted(
        (node for node in ast.walk(failed_safe_release) if isinstance(node, ast.Call)),
        key=lambda node: node.lineno,
    )
    absence = [
        call for call in release_calls
        if isinstance(call.func, ast.Name)
        and call.func.id == "_require_candidate_absent"
    ]
    updates = [
        call for call in release_calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "journal"
        and call.func.attr == "update"
    ]
    releases = [
        call for call in release_calls
        if isinstance(call.func, ast.Name) and call.func.id == "_release_lock"
    ]
    if (
        len(absence) != 1
        or len(updates) != 1
        or len(releases) != 1
        or not (absence[0].lineno < updates[0].lineno < releases[0].lineno)
        or ast.unparse(updates[0].args[0]) != "'failed_safe'"
        or {
            keyword.arg: ast.unparse(keyword.value)
            for keyword in updates[0].keywords
        } != {
            "operation": "'terminal'",
            "status": "'FAIL'",
            "actor_last": "actor",
        }
    ):
        raise ContractMismatch("rollout.migration.failed_safe_actor_audit")

    contract = functions["_contract"]
    contract_args = [argument.arg for argument in contract.args.args]
    if contract_args != [
        "account", "role_arn", "source_sha", "attempt", "prior_version",
        "prior_hash", "target_hash", "provenance",
    ] or any(
        isinstance(node, ast.Constant) and node.value == "session"
        for node in ast.walk(contract)
    ):
        raise ContractMismatch("rollout.migration.stable_contract")

    admin = classes["AdminAwsCli"]
    admin_methods = {
        node.name: node for node in admin.body if isinstance(node, ast.FunctionDef)
    }
    constructor = admin_methods.get("__init__")
    if constructor is None or [argument.arg for argument in constructor.args.args] != [
        "self", "deadline", "approved_content", "approved_version_name",
        "prior_version",
    ]:
        raise ContractMismatch("rollout.migration.adapter_contract")
    classify = functions["_classify_admin_command"]
    classify_names = [argument.arg for argument in classify.args.kwonlyargs]
    if classify_names != [
        "approved_content", "approved_version_name", "candidate_version",
    ]:
        raise ContractMismatch("rollout.migration.adapter_contract")
    classify_text = ast.unparse(classify)
    if (
        "approved_content" not in classify_text
        or "approved_version_name" not in classify_text
        or "frozenset({'primary'})" not in classify_text
        or "frozenset({'terminal'})" not in classify_text
    ):
        raise ContractMismatch("rollout.migration.adapter_contract")

    execute = functions["execute"]
    journal_phase_operations = {
        "failed_safe": "terminal", "complete": "terminal",
        "lease_acquired": "primary", "prestate_verified": "primary",
        "update_submitting": "primary", "candidate_verified": "primary",
        "cutover_submitting": "primary", "cutover_reconciled": "terminal",
    }
    for call in ast.walk(execute):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id in {"aws", "journal"}
            and call.func.attr in {"call", "update"}
        ):
            operations = [
                keyword.value for keyword in call.keywords
                if keyword.arg == "operation"
            ]
            if (
                len(operations) != 1
                or not isinstance(operations[0], ast.Constant)
                or operations[0].value not in {"primary", "terminal"}
            ):
                raise ContractMismatch("rollout.migration.operation_class")
            operation = cast(str, operations[0].value)
            if call.func.value.id == "aws" and call.func.attr == "call":
                command_text = ast.unparse(call.args[0]) if call.args else ""
                if "'update-document'" in command_text and operation != "primary":
                    raise ContractMismatch("rollout.migration.operation_class")
            if (
                call.func.value.id == "journal"
                and call.func.attr == "update"
                and call.args
                and isinstance(call.args[0], ast.Constant)
                and (
                    call.args[0].value not in journal_phase_operations
                    or operation != journal_phase_operations[
                        cast(str, call.args[0].value)
                    ]
                )
            ):
                raise ContractMismatch("rollout.migration.operation_class")
    default_calls = [
        call for call in ast.walk(execute)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "_default_submit"
    ]
    default_contract = {
        (ast.unparse(call.args[1]), next(
            ast.literal_eval(keyword.value) for keyword in call.keywords
            if keyword.arg == "operation"
        ))
        for call in default_calls if len(call.args) == 2
        and any(keyword.arg == "operation" for keyword in call.keywords)
    }
    if default_contract != {("candidate", "primary")}:
        raise ContractMismatch("rollout.migration.operation_class")

    execute_calls = [
        call for call in ast.walk(execute) if isinstance(call, ast.Call)
    ]
    create_lines = [
        call.lineno for call in execute_calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "RemoteJournal"
        and call.func.attr == "create"
    ]
    open_lines = [
        call.lineno for call in execute_calls
        if isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "RemoteJournal"
        and call.func.attr == "open"
    ]
    lock_lines = [
        call.lineno for call in execute_calls
        if isinstance(call.func, ast.Name) and call.func.id == "_put_lock"
    ]
    if (
        len(create_lines) != 1
        or len(open_lines) != 2
        or len(lock_lines) != 1
        or create_lines[0] >= lock_lines[0]
        or min(open_lines) >= lock_lines[0]
        or max(open_lines) <= lock_lines[0]
    ):
        raise ContractMismatch("rollout.migration.journal_first")

    main = functions.get("main")
    if (
        main is None
        or not main.body
        or not isinstance(main.body[0], ast.Assign)
        or ast.unparse(main.body[0].value) != "Deadline.start()"
    ):
        raise ContractMismatch("rollout.migration.global_deadline")
    parser_flags = {
        ast.literal_eval(call.args[0])
        for call in ast.walk(main) if isinstance(call, ast.Call) and call.args
        and isinstance(call.func, ast.Attribute) and call.func.attr == "add_argument"
        and isinstance(call.args[0], ast.Constant)
        and any(keyword.arg == "required" and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True for keyword in call.keywords)
    } if main else set()
    if parser_flags != {
        "--mode", "--account-id", "--expected-role-arn",
        "--expected-session-name", "--source-sha", "--migration-attempt-id",
        "--expected-current-version", "--expected-current-canonical-sha256",
        "--audit-path",
    }:
        raise ContractMismatch("rollout.migration.cli")


def _verify_migration_boundary(
    workflow: Mapping[str, Any], trust_source: str, policy_source: str,
    rollout_policy_source: str, bootstrap_source: str,
) -> None:
    inputs = set(_dispatch_inputs(workflow, "migration.workflow"))
    if inputs != {
        "mode", "source_sha", "migration_attempt_id", "expected_current_version",
        "expected_current_canonical_sha256",
    }:
        raise ContractMismatch("migration.workflow.inputs")
    if workflow.get("concurrency") != {
        "group": "kiwoom-stock-shadow-i-02cb0a404794bd43a",
        "cancel-in-progress": False,
    }:
        raise ContractMismatch("migration.workflow.concurrency")
    text = yaml.safe_dump(workflow)
    run_text = "\n".join(
        cast(str, step.get("run", ""))
        for step in _steps(workflow, "migration.workflow")
        if isinstance(step.get("run", ""), str)
    )
    required = (
        "production-shadow", "KIWOOM_AWS_SHADOW_MIGRATION_ROLE_ARN",
        "refs/heads/main",
        "if: always()", "retention-days: 14", "--mode",
        "steps.audit.outputs.path",
    )
    if any(value in text for value in (
        "secrets.", "SendCommand", "KIWOOM_APP_KEY", "runner.temp",
    )):
        raise ContractMismatch("migration.workflow.forbidden")
    if (
        any(value not in text for value in required)
        or "git status --porcelain --untracked-files=all" not in run_text
        or '${RUNNER_TEMP}/shadow-rollout-document-migration.json' not in run_text
        or '>>"${GITHUB_OUTPUT}"' not in run_text
    ):
        raise ContractMismatch("migration.workflow.contract")

    try:
        trust = json.loads(trust_source)
        policy = json.loads(policy_source)
        rollout_policy = json.loads(rollout_policy_source)
    except json.JSONDecodeError as error:
        raise ContractMismatch("migration.iam.json") from error
    if "production-shadow" not in _canonical_text(trust):
        raise ContractMismatch("migration.iam.trust")
    policy_text = _canonical_text(policy)
    for allowed in (
        "ssm:DescribeDocument", "ssm:GetDocument", "ssm:ListDocumentVersions",
        "ssm:UpdateDocument", "ssm:UpdateDocumentDefaultVersion",
        "ssm:GetParameter", "ssm:PutParameter", "ssm:DeleteParameter",
        '"ssm:Overwrite":"false"',
    ):
        if allowed not in policy_text:
            raise ContractMismatch("migration.iam.minimum")
    for forbidden_action in (
        "ssm:CreateDocument", "ssm:DeleteDocument", "ssm:SendCommand", "ec2:",
    ):
        if forbidden_action in policy_text:
            raise ContractMismatch("migration.iam.forbidden")
    rollout_text = _canonical_text(rollout_policy)
    rollout_resource = (
        "arn:aws:ssm:<AWS_REGION>:<AWS_ACCOUNT_ID>:document/"
        "KiwoomStock-ShadowWorkerRollout"
    )
    for statement in rollout_policy.get("Statement", []):
        if isinstance(statement, dict) and statement.get("Resource") == rollout_resource:
            actions = statement.get("Action", [])
            values = {actions} if isinstance(actions, str) else set(actions)
            if values - {"ssm:GetDocument", "ssm:DescribeDocument", "ssm:SendCommand"}:
                raise ContractMismatch("migration.iam.routine_authority")
    if not rollout_text:
        raise ContractMismatch("migration.iam.routine_authority")
    if any(value not in bootstrap_source for value in (
        "create-role", "put-role-policy", "refusing overwrite", "get-role-policy",
    )) or any(value in bootstrap_source for value in ("update-assume-role-policy", "delete-role")):
        raise ContractMismatch("migration.bootstrap.create_only")


def _canonical_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _verify_rollout_workflow(workflow: Mapping[str, Any]) -> None:
    if set(_dispatch_inputs(workflow, "rollout.workflow")) != {"source_sha"}:
        raise ContractMismatch("rollout.workflow.dispatch_input_set")
    if workflow.get("env") != {
        "AWS_REGION": REGION, "EC2_INSTANCE_ID": INSTANCE_ID,
        "ROLLOUT_DOCUMENT_NAME": ROLLOUT_DOCUMENT_NAME,
        "SHADOW_DOCUMENT_NAME": ACTIVATION_DOCUMENT_NAME,
        "EVIDENCE_FILENAME": "shadow-rollout-evidence.json",
    }:
        raise ContractMismatch("rollout.workflow.global_env")
    steps = _steps(workflow, "rollout.workflow")
    candidates = _run_steps_with(steps, "-m kiwoom_stock.deployment.shadow_rollout")
    if len(candidates) != 1:
        raise ContractMismatch("rollout.workflow.execute_unit")
    step = candidates[0]
    if step.get("env") != {
        "AWS_ACCESS_KEY_ID": "${{ steps.oidc.outputs.aws-access-key-id }}",
        "AWS_SECRET_ACCESS_KEY": "${{ steps.oidc.outputs.aws-secret-access-key }}",
        "AWS_SESSION_TOKEN": "${{ steps.oidc.outputs.aws-session-token }}",
        "SOURCE_SHA": "${{ inputs.source_sha }}",
        "ROLLOUT_ATTEMPT_ID": "${{ github.run_id }}",
    }:
        raise ContractMismatch("rollout.workflow.execute_env")
    script = step.get("run")
    if not isinstance(script, str):
        raise ContractMismatch("rollout.workflow.execute_run")
    tokens = _shell_command(
        script, "python3 -m kiwoom_stock.deployment.shadow_rollout",
        "rollout.workflow.executor_command",
    )
    flags = _flags(
        tokens, ["python3", "-m", "kiwoom_stock.deployment.shadow_rollout"],
        "rollout.workflow.executor_flags",
    )
    if flags != {
        "--source-sha": "${SOURCE_SHA}",
        "--rollout-attempt-id": "${ROLLOUT_ATTEMPT_ID}",
        "--audit": "${EVIDENCE_FILENAME}",
    }:
        raise ContractMismatch("rollout.workflow.executor_flags")


def _verify_rollout_document(document: Mapping[str, Any]) -> None:
    if document.get("description") != "Install, read back, or roll back the exact shadow artifact set":
        raise ContractMismatch("rollout.document.description")
    if document.get("parameters") != ROLLOUT_PARAMETER_SCHEMA:
        raise ContractMismatch("rollout.document.parameter_schema")
    inputs, command = _document_step(document, "rollout.document")
    if inputs.get("timeoutSeconds") != "300":
        raise ContractMismatch("rollout.document.timeout")
    references = set(re.findall(r"\$SSM_([A-Za-z][A-Za-z0-9]*)\b", command))
    if references != ROLLOUT_PARAMETER_NAMES:
        raise ContractMismatch("rollout.document.ssm_reference_set")
    header = re.sub(r"\\\n\s*", " ", command.split("<<'KIWOOM_ROLLOUT'", 1)[0])
    try:
        header_tokens = shlex.split(header)
    except ValueError:
        raise ContractMismatch("rollout.document.executor_argv") from None
    if header_tokens != [
        "exec", "/bin/bash", "-s", "--", "$SSM_Action", "$SSM_SourceSha",
        "$SSM_WorkerSha256", "$SSM_ValidatorSha256", "$SSM_ShadowDocumentSha256",
        "$SSM_RolloutAttemptId", "$SSM_ExpectedInstanceId", "$SSM_Region",
    ]:
        raise ContractMismatch("rollout.document.executor_argv")
    trusted_bindings = {
        "source_sha": "$2", "worker_sha": "$3", "validator_sha": "$4",
        "document_sha": "$5", "attempt": "$6",
    }
    for name, source in trusted_bindings.items():
        assignments, indirect_write = _protected_shell_writes(command, name)
        if assignments != [f'"{source}"'] or indirect_write:
            raise ContractMismatch("rollout.document.trusted_input_assignment")
    publish_match = re.search(
        r'(?ms)^publish\(\) \{\n(?P<body>.*?)^\s*\}\n', command,
    )
    if publish_match is None:
        raise ContractMismatch("rollout.document.publish_function")
    publish_body = publish_match.group("body")
    publish_guards = [
        'install -o root -g root -m "$mode" "$source" "$temporary"',
        '[[ "$(stat -c \'%u:%g:%a:%h:%F\' "$temporary")" == '
        '"0:0:${mode}:1:regular file" ]]',
        '[[ "$(sha256sum "$temporary" | cut -d\' \' -f1)" == "$expected" ]]',
        'case "$syntax" in shell) bash -n "$temporary" ;; python) python3 -c',
        'fsync_file "$temporary"; mv -fT "$temporary" "$destination"; '
        'fsync_parent "$destination"',
    ]
    positions = [publish_body.find(fragment) for fragment in publish_guards]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        raise ContractMismatch("rollout.document.publish_hash_guard")
    required_fragments = {
        "worker_target=/usr/local/sbin/kiwoom-shadow-worker",
        "validator_target=/usr/local/libexec/kiwoom-shadow-runtime-evidence.py",
        "binding=/var/lib/kiwoom-stock/shadow-rollout-current.json",
        "shadow_worker_control.sh\" -o \"$downloaded\"",
        "shadow_runtime_evidence.py\" -o \"$validator_downloaded\"",
        "publish \"$validator_downloaded\" \"$validator_target\" 750 \"$validator_sha\" python",
        "publish \"$downloaded\" \"$worker_target\" 750 \"$worker_sha\" shell",
        "publish \"$marker\" \"$binding\" 600 \"$marker_sha\" no",
    }
    if any(command.count(fragment) != 1 for fragment in required_fragments):
        raise ContractMismatch("rollout.document.artifact_wiring")
    ordered_pipeline = [
        'shadow_worker_control.sh" -o "$downloaded"',
        'shadow_runtime_evidence.py" -o "$validator_downloaded"',
        '[[ "$(sha256sum "$downloaded" | cut -d\' \' -f1)" == "$worker_sha" ]]',
        'bash -n "$downloaded"',
        '[[ "$(sha256sum "$validator_downloaded" | cut -d\' \' -f1)" == '
        '"$validator_sha" ]]',
        "python3 -c 'import sys; compile(open(sys.argv[1]",
        'publish "$validator_downloaded" "$validator_target" 750 '
        '"$validator_sha" python',
        'publish "$downloaded" "$worker_target" 750 "$worker_sha" shell',
        'publish "$marker" "$binding" 600 "$marker_sha" no',
    ]
    pipeline_start = command.find("timeout 45 curl")
    pipeline = command[pipeline_start:] if pipeline_start >= 0 else ""
    pipeline_positions = [pipeline.find(fragment) for fragment in ordered_pipeline]
    if any(position < 0 for position in pipeline_positions) or (
        pipeline_positions != sorted(pipeline_positions)
    ):
        raise ContractMismatch("rollout.document.publish_pipeline")


def _ci_shell_commands(script: str) -> list[list[str]]:
    return [
        tokens for unit in _shell_command_units(script)
        if (tokens := _shell_tokens(unit, "ci.command_parse"))
    ]


def _normalized_ci_command(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", result[0]):
        result.pop(0)
    if result and result[0].rsplit("/", 1)[-1] == "command":
        result.pop(0)
    if result and result[0].rsplit("/", 1)[-1] == "env":
        result.pop(0)
        while result and (
            result[0].startswith("-")
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", result[0])
        ):
            result.pop(0)
        if result and result[0].rsplit("/", 1)[-1] == "command":
            result.pop(0)
    return result


def _is_ci_build_command(tokens: list[str]) -> bool:
    command = _normalized_ci_command(tokens)
    if not command:
        return False
    executable = command[0].rsplit("/", 1)[-1]
    arguments = command[1:]
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable):
        return arguments[:2] == ["-m", "build"] or (
            arguments[:3] == ["-m", "pip", "wheel"]
        )
    if re.fullmatch(r"pip(?:3(?:\.\d+)?)?", executable):
        return arguments[:1] == ["wheel"]
    if executable == "poetry":
        return arguments[:1] == ["build"]
    if executable == "npm":
        return arguments[:2] == ["run", "build"] or arguments[:1] == ["pack"]
    if executable == "docker":
        return arguments[:1] == ["build"] or (
            arguments[:1] == ["compose"] and "build" in arguments[1:]
        )
    return False


def _is_ci_checker_command(tokens: list[str]) -> bool:
    command = _normalized_ci_command(tokens)
    if not command:
        return False
    executable = command[0].rsplit("/", 1)[-1]
    return bool(
        re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable)
        and command[1:] == ["deploy/check_shadow_ssm_contract.py"]
    )


def _ci_gate_name(tokens: list[str]) -> str | None:
    command = _normalized_ci_command(tokens)
    if not command:
        return None
    executable = command[0].rsplit("/", 1)[-1]
    if re.fullmatch(r"python(?:3(?:\.\d+)?)?", executable) and (
        len(command) >= 3 and command[1] == "-m"
        and command[2] in {"flake8", "mypy", "pytest"}
    ):
        return command[2]
    if executable in {"flake8", "mypy", "pytest"}:
        return executable
    return None


def _verify_ci(workflow: Mapping[str, Any]) -> None:
    jobs = _mapping(workflow.get("jobs"), "ci.jobs")
    quality = _mapping(jobs.get("quality"), "ci.quality")
    package = _mapping(jobs.get("package"), "ci.package")
    steps = quality.get("steps")
    if not isinstance(steps, list):
        raise ContractMismatch("ci.quality_steps")
    run_scripts: list[str] = []
    for raw_step in steps:
        step = _mapping(raw_step, "ci.step")
        run = step.get("run", "")
        if not isinstance(run, str):
            raise ContractMismatch("ci.run_type")
        run_scripts.append(run)
    quality_commands = [_ci_shell_commands(script) for script in run_scripts]
    checker_locations = [
        (index, command_index)
        for index, commands in enumerate(quality_commands)
        for command_index, command in enumerate(commands)
        if _is_ci_checker_command(command)
    ]
    if len(checker_locations) != 1:
        raise ContractMismatch("ci.checker_count")
    checker_index, checker_command_index = checker_locations[0]
    if quality_commands[checker_index] != [
        quality_commands[checker_index][checker_command_index]
    ]:
        raise ContractMismatch("ci.checker_count")
    gates = [
        index for index, commands in enumerate(quality_commands)
        if any(_ci_gate_name(command) is not None for command in commands)
    ]
    if not gates or checker_index >= min(gates):
        raise ContractMismatch("ci.checker_order")
    needs = package.get("needs")
    if needs != "quality" and needs != ["quality"]:
        raise ContractMismatch("ci.package_needs_quality")
    package_steps = package.get("steps")
    if not isinstance(package_steps, list):
        raise ContractMismatch("ci.package_steps")
    package_commands = [
        command for step in package_steps
        for command in _ci_shell_commands(_ci_run(step, "ci.package_step"))
    ]
    if not any(_is_ci_build_command(command) for command in package_commands):
        raise ContractMismatch("ci.package_build")
    normalized_quality = [
        _normalized_ci_command(command)
        for commands in quality_commands for command in commands
    ]
    flake_scoped = any(
        _ci_gate_name(command) == "flake8"
        and {"src", "tests", "deploy/check_shadow_ssm_contract.py"}
        <= set(_normalized_ci_command(command))
        for commands in quality_commands for command in commands
    )
    mypy_scoped = any(
        _ci_gate_name(command) == "mypy"
        and {"src/kiwoom_stock", "deploy/check_shadow_ssm_contract.py"}
        <= set(_normalized_ci_command(command))
        for commands in quality_commands for command in commands
    )
    if not flake_scoped or not mypy_scoped or not normalized_quality:
        raise ContractMismatch("ci.checker_static_scope")
    job_needs: dict[str, set[str]] = {}
    build_steps: list[tuple[str, int]] = []
    for job_id, raw_job in jobs.items():
        job = _mapping(raw_job, "ci.job")
        needs_value = job.get("needs", [])
        if isinstance(needs_value, str):
            needs = {needs_value}
        elif isinstance(needs_value, list) and all(
            isinstance(item, str) for item in needs_value
        ):
            needs = set(needs_value)
        else:
            raise ContractMismatch("ci.job_needs")
        if not needs <= set(jobs):
            raise ContractMismatch("ci.job_needs")
        job_needs[job_id] = needs
        job_steps = job.get("steps")
        if not isinstance(job_steps, list):
            raise ContractMismatch("ci.job_steps")
        for index, raw_step in enumerate(job_steps):
            run = _ci_run(raw_step, "ci.job_step")
            if any(_is_ci_build_command(unit) for unit in _ci_shell_commands(run)):
                build_steps.append((job_id, index))

    def depends_on_quality(job_id: str, visiting: set[str]) -> bool:
        if job_id == "quality":
            return True
        if job_id in visiting:
            raise ContractMismatch("ci.job_cycle")
        return any(
            depends_on_quality(parent, visiting | {job_id})
            for parent in job_needs[job_id]
        )

    for job_id, index in build_steps:
        if job_id == "quality":
            if index <= checker_index:
                raise ContractMismatch("ci.build_before_checker")
        elif not depends_on_quality(job_id, set()):
            raise ContractMismatch("ci.build_without_quality_dependency")


def _ci_run(value: object, category: str) -> str:
    step = _mapping(value, category)
    run = step.get("run", "")
    if not isinstance(run, str):
        raise ContractMismatch(f"{category}.run_type")
    return run


def check(root: Path) -> tuple[int, int, int]:
    activation_workflow = _load_yaml(
        _read(root, ACTIVATION_WORKFLOW, "activation.workflow.unreadable"),
        "activation.workflow",
    )
    rollout_workflow = _load_yaml(
        _read(root, ROLLOUT_WORKFLOW, "rollout.workflow.unreadable"),
        "rollout.workflow",
    )
    migration_workflow = _load_yaml(
        _read(root, MIGRATION_WORKFLOW, "migration.workflow.unreadable"),
        "migration.workflow",
    )
    activation_document = _load_yaml(
        _read(root, ACTIVATION_DOCUMENT, "activation.document.unreadable"),
        "activation.document",
    )
    rollout_document = _load_yaml(
        _read(root, ROLLOUT_DOCUMENT, "rollout.document.unreadable"),
        "rollout.document",
    )
    worker = _read(root, WORKER, "activation.worker.unreadable")
    validator = _read(root, VALIDATOR, "activation.validator.unreadable")
    executor = _read(root, ROLLOUT_EXECUTOR, "rollout.executor.unreadable")
    migration = _read(root, ROLLOUT_MIGRATION, "rollout.migration.unreadable")
    migration_bootstrap = _read(
        root, MIGRATION_BOOTSTRAP, "migration.bootstrap.unreadable"
    )
    migration_trust = _read(root, MIGRATION_TRUST, "migration.trust.unreadable")
    migration_policy = _read(root, MIGRATION_POLICY, "migration.policy.unreadable")
    rollout_policy = _read(root, ROLLOUT_POLICY, "rollout.policy.unreadable")
    ci = _load_yaml(_read(root, CI_WORKFLOW, "ci.unreadable"), "ci")

    _verify_activation_workflow(activation_workflow)
    _verify_activation_document(activation_document, worker)
    _verify_validator(validator)
    _verify_rollout_workflow(rollout_workflow)
    _verify_rollout_executor(executor)
    _verify_rollout_migration(migration)
    _verify_migration_boundary(
        migration_workflow, migration_trust, migration_policy,
        rollout_policy, migration_bootstrap,
    )
    _verify_rollout_document(rollout_document)
    _verify_ci(ci)
    return 2, len(ACTIVATION_PARAMETER_SCHEMA), len(ROLLOUT_PARAMETER_SCHEMA)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        _fail("usage", setup=True)
    root = Path(args[0]) if args else Path.cwd()
    if not root.is_dir():
        _fail("root.unavailable", setup=True)
    try:
        units, activation_parameters, rollout_parameters = check(root)
    except SetupError as error:
        _fail(str(error), setup=True)
    except ContractMismatch as error:
        _fail(str(error))
    print(
        f"PASS units={units} activation_parameters={activation_parameters} "
        f"rollout_parameters={rollout_parameters}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
