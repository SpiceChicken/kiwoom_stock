"""Static contract tests for disabled C* SSM boundaries."""

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "deploy/check_shadow_cstar_ssm_contract.py"


def run_checker(root: Path = ROOT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_cstar_ssm_documents_pass_static_contract():
    result = run_checker()
    assert result.returncode == 0
    assert result.stdout == "PASS documents=2 activation_parameters=17 evidence_parameters=7\n"
    assert result.stderr == ""


def test_activation_document_does_not_expose_legacy_telemetry_or_worker_directly():
    text = (ROOT / "deploy/ssm/shadow-cstar-activation-document.yaml").read_text()
    assert "telemetry-export-page" not in text
    assert "/usr/local/sbin/kiwoom-shadow-worker" not in text
    assert "/usr/local/libexec/kiwoom-shadow-schedule-fence.py activate" in text


def test_activation_document_uses_posix_shell_options_for_ssm_run_shell_script():
    text = (ROOT / "deploy/ssm/shadow-cstar-activation-document.yaml").read_text()
    assert '"set -eu"' in text
    assert "pipefail" not in text
    assert '"set -E' not in text


def test_evidence_document_has_no_activation_or_fence_mutation_boundary():
    text = (ROOT / "deploy/ssm/shadow-evidence-export-document.yaml").read_text()
    assert "shadow-schedule-fence.py" not in text
    assert "kiwoom-shadow-worker" not in text
    assert "--desired-state" not in text
