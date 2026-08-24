"""Disabled-by-default C* IaC contract tests."""

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "deploy/aws/shadow-cstar-scheduler.yaml.example"
CHECKER = ROOT / "deploy/check_shadow_cstar_iac.py"


def load():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


def test_iac_checker_reports_disabled_boundary():
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == "PASS cstar_iac=disabled schedules=2 observer=exact-evidence\n"
    assert result.stderr == ""


def test_all_activation_paths_are_disabled_by_default():
    template = load()
    parameters = template["Parameters"]
    assert parameters["EnableActivationSchedules"]["Default"] == "false"
    assert parameters["ActivationScheduleState"]["Default"] == "DISABLED"
    assert parameters["EnableObserverRule"]["Default"] == "false"
    assert parameters["EnableReconciliationSchedules"]["Default"] == "false"
    assert parameters["SubmitterReservedConcurrency"]["Default"] == 0
    assert parameters["ObserverReservedConcurrency"]["Default"] == 0
    assert parameters["AlertMode"]["Default"] == "metrics-only"
    assert template["Resources"]["StartSchedule"]["Condition"] == "ActivationEnabled"
    assert template["Resources"]["StopSchedule"]["Condition"] == "ActivationEnabled"
    assert template["Resources"]["StartSchedule"]["Properties"]["State"] == {"Ref": "ActivationScheduleState"}
    assert template["Resources"]["StopSchedule"]["Properties"]["State"] == {"Ref": "ActivationScheduleState"}
    assert template["Resources"]["ObserverRule"]["Condition"] == "ObserverEnabled"
    assert template["Resources"]["ReconcileSchedule"]["Condition"] == "ReconciliationEnabled"


def test_evidence_bucket_is_versioned_and_governance_locked_for_400_days():
    bucket = load()["Resources"]["EvidenceBucket"]
    props = bucket["Properties"]
    assert bucket["DeletionPolicy"] == "Retain"
    assert props["ObjectLockEnabled"] is True
    assert props["VersioningConfiguration"]["Status"] == "Enabled"
    retention = props["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]
    assert retention == {"Mode": "GOVERNANCE", "Days": 400}


def test_observer_iam_mentions_only_exact_evidence_document_for_send_command():
    policies = load()["Resources"]["ObserverRole"]["Properties"]["Policies"]
    text = json.dumps(policies, sort_keys=True)
    assert "KiwoomStock-ShadowEvidenceExport" in text
    assert "KiwoomStock-ShadowCStarActivation" not in text
    assert "iam:PassRole" not in text
    assert "scheduler:UpdateSchedule" not in text


def test_schedule_retry_windows_match_approved_cutoffs():
    resources = load()["Resources"]
    start = resources["StartSchedule"]["Properties"]["Target"]["RetryPolicy"]
    stop = resources["StopSchedule"]["Properties"]["Target"]["RetryPolicy"]
    assert start == {"MaximumEventAgeInSeconds": 480, "MaximumRetryAttempts": 2}
    assert stop == {"MaximumEventAgeInSeconds": 900, "MaximumRetryAttempts": 2}
