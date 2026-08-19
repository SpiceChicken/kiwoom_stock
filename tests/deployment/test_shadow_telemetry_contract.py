from pathlib import Path


def test_telemetry_page_contract_is_additive_and_bounded():
    document = Path("deploy/ssm/shadow-worker-document.yaml").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/cd-shadow-worker-activation.yml").read_text(encoding="utf-8")
    worker = Path("deploy/ec2/shadow_worker_control.sh").read_text(encoding="utf-8")
    assert "telemetry-export-page" in document
    assert "TelemetryOffset" in document and "TelemetryLength" in document
    assert "--compose-shadow-sha256" in document
    assert "shadow-telemetry.jsonl.gz" in workflow
    assert "shadow-telemetry.manifest.json" in workflow
    assert "--network none" in worker
    assert "length <= 12288" in worker
    assert "shadow-telemetry.db" in worker
    assert "TelemetrySessionDateKst=${telemetry_session_date}" in workflow
    assert "TelemetrySessionDateKst=${ACTIVATION_ID#shadow-session-}" not in workflow
    assert "page_sha256" in workflow
