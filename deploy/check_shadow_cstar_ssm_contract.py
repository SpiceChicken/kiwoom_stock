#!/usr/bin/env python3
"""Fail-closed static checker for the two C* SSM documents."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


ACTIVATION = Path("deploy/ssm/shadow-cstar-activation-document.yaml")
EVIDENCE = Path("deploy/ssm/shadow-evidence-export-document.yaml")
ACTIVATION_PARAMETERS = {
    "Phase", "ScheduleGeneration", "ScheduleArn", "ScheduledTime",
    "OccurrenceId", "SessionDateKst", "ReleaseId", "DesiredState",
    "ImageDigest", "SourceSha", "ActivationId", "ComposeShadowSha256",
    "ExpectedWorkerSha256", "ExpectedValidatorSha256",
    "ExpectedShadowDocumentSha256", "ExpectedInstanceId", "Region",
}
EVIDENCE_PARAMETERS = {
    "SessionDateKst", "OccurrenceId", "ReleaseId", "EvidenceOffset",
    "EvidenceLength", "ExpectedInstanceId", "Region",
}
FORBIDDEN_ACTIVATION = ("telemetry-export-page", "aws ", "github")
FORBIDDEN_EVIDENCE = ("--desired-state", "kiwoom-shadow-worker", "shadow-schedule-fence.py")


class ContractError(ValueError):
    pass


def _load(root: Path, path: Path) -> dict[str, Any]:
    try:
        raw = (root / path).read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ContractError(f"{path}: invalid json: {error}") from None
    if not isinstance(value, dict):
        raise ContractError(f"{path}: root must be object")
    return value


def _parameters(value: dict[str, Any], expected: set[str], path: Path) -> dict[str, Any]:
    parameters = value.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != expected:
        raise ContractError(f"{path}: parameter set mismatch")
    for name, definition in parameters.items():
        if not isinstance(definition, dict):
            raise ContractError(f"{path}: parameter {name} shape")
        if definition.get("type") != "String" or definition.get("interpolationType") != "ENV_VAR":
            raise ContractError(f"{path}: parameter {name} interpolation")
    return parameters


def _script(value: dict[str, Any], path: Path) -> str:
    steps = value.get("mainSteps")
    if not isinstance(steps, list) or len(steps) != 1 or not isinstance(steps[0], dict):
        raise ContractError(f"{path}: mainSteps")
    inputs = steps[0].get("inputs")
    if not isinstance(inputs, dict) or inputs.get("timeoutSeconds") not in {"120", "1020"}:
        raise ContractError(f"{path}: timeout")
    commands = inputs.get("runCommand")
    if not isinstance(commands, list) or not commands or not all(isinstance(item, str) for item in commands):
        raise ContractError(f"{path}: runCommand")
    return "\n".join(commands)


def check(root: Path) -> tuple[int, str]:
    activation = _load(root, ACTIVATION)
    evidence = _load(root, EVIDENCE)
    if activation.get("schemaVersion") != "2.2" or evidence.get("schemaVersion") != "2.2":
        raise ContractError("schema version")
    activation_params = _parameters(activation, ACTIVATION_PARAMETERS, ACTIVATION)
    _parameters(evidence, EVIDENCE_PARAMETERS, EVIDENCE)
    if activation_params["DesiredState"].get("allowedValues") != ["continuous", "stop"]:
        raise ContractError("activation desired state")
    activation_script = _script(activation, ACTIVATION)
    evidence_script = _script(evidence, EVIDENCE)
    if not activation_script.startswith("set -eu\n"):
        raise ContractError("activation shell must be POSIX-compatible")
    if "pipefail" in activation_script or "set -E" in activation_script:
        raise ContractError("activation shell must not require bash")
    if "/usr/local/libexec/kiwoom-shadow-schedule-fence.py activate" not in activation_script:
        raise ContractError("activation fence boundary")
    if "/usr/local/sbin/kiwoom-shadow-worker" in activation_script:
        raise ContractError("activation must delegate only through fence")
    if any(value in activation_script for value in FORBIDDEN_ACTIVATION):
        raise ContractError("activation forbidden capability")
    if "/usr/local/libexec/kiwoom-shadow-evidence-export.py" not in evidence_script:
        raise ContractError("evidence exporter boundary")
    if any(value in evidence_script for value in FORBIDDEN_EVIDENCE):
        raise ContractError("evidence forbidden capability")
    if "exec 9>/run/lock/kiwoom-stock-shadow.lock" not in evidence_script:
        raise ContractError("evidence incumbent lock")
    return 0, "PASS documents=2 activation_parameters=17 evidence_parameters=7"


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(".")
    try:
        code, message = check(root)
    except ContractError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    print(message)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
