#!/usr/bin/env python3
"""Static, side-effect-free checker for the disabled C* CloudFormation example."""

from __future__ import annotations

import json
from pathlib import Path
import sys


TEMPLATE = Path("deploy/aws/shadow-cstar-scheduler.yaml.example")


class IacError(ValueError):
    pass


def check(root: Path) -> str:
    try:
        value = json.loads((root / TEMPLATE).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise IacError(f"template: {error}") from None
    parameters = value.get("Parameters", {})
    defaults = {
        "EnableActivationSchedules": "false",
        "ActivationScheduleState": "DISABLED",
        "EnableObserverRule": "false",
        "EnableReconciliationSchedules": "false",
        "SubmitterReservedConcurrency": 0,
        "ObserverReservedConcurrency": 0,
        "AlertMode": "metrics-only",
    }
    for name, expected in defaults.items():
        if parameters.get(name, {}).get("Default") != expected:
            raise IacError(f"default:{name}")
    resources = value.get("Resources", {})
    for name, condition in {
        "StartSchedule": "ActivationEnabled",
        "StopSchedule": "ActivationEnabled",
        "ObserverRule": "ObserverEnabled",
        "ReconcileSchedule": "ReconciliationEnabled",
        "ObserverInvokePermission": "ObserverEnabled",
    }.items():
        if resources.get(name, {}).get("Condition") != condition:
            raise IacError(f"condition:{name}")
    observer_alert_alarm = resources.get("ObserverAlertAlarm", {}).get("Properties", {})
    if (
        observer_alert_alarm.get("Namespace") != "Kiwoom/ShadowCStar"
        or observer_alert_alarm.get("MetricName") != "cstar_observer_alerted"
        or observer_alert_alarm.get("Threshold") != 1
        or observer_alert_alarm.get("TreatMissingData") != "notBreaching"
        or "AlarmActions" in observer_alert_alarm
    ):
        raise IacError("observer-alert-alarm")
    bucket = resources.get("EvidenceBucket", {})
    props = bucket.get("Properties", {})
    retention = props.get("ObjectLockConfiguration", {}).get("Rule", {}).get("DefaultRetention")
    if (
        bucket.get("DeletionPolicy") != "Retain"
        or props.get("ObjectLockEnabled") is not True
        or props.get("VersioningConfiguration", {}).get("Status") != "Enabled"
        or retention != {"Mode": "GOVERNANCE", "Days": 400}
    ):
        raise IacError("evidence-retention")
    observer_text = json.dumps(resources.get("ObserverRole", {}), sort_keys=True)
    if (
        "KiwoomStock-ShadowEvidenceExport" not in observer_text
        or "KiwoomStock-ShadowCStarActivation" in observer_text
        or "iam:PassRole" in observer_text
    ):
        raise IacError("observer-iam")
    submitter_policies = resources.get("SubmitterRole", {}).get(
        "Properties", {}
    ).get("Policies", [])
    submitter_statements = [
        statement
        for policy in submitter_policies
        for statement in policy.get("PolicyDocument", {}).get("Statement", [])
    ]
    submitter_dynamodb_actions = {
        action
        for statement in submitter_statements
        if statement.get("Effect") == "Allow"
        and statement.get("Resource") == {"Fn::GetAtt": ["CStarTable", "Arn"]}
        for action in (
            statement.get("Action", [])
            if isinstance(statement.get("Action"), list)
            else [statement.get("Action")]
        )
    }
    if not {
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:TransactWriteItems",
        "dynamodb:UpdateItem",
    }.issubset(submitter_dynamodb_actions):
        raise IacError("submitter-iam")
    return "PASS cstar_iac=disabled schedules=2 observer=exact-evidence"


def main(argv: list[str] | None = None) -> int:
    root = Path(argv[0]) if argv else Path(".")
    try:
        print(check(root))
    except IacError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
