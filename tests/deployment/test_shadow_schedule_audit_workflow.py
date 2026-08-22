"""Static security contracts for the workflow_run Shadow audit."""

from __future__ import annotations

from pathlib import Path
import re

import pytest
import yaml


WORKFLOW = Path(".github/workflows/cd-shadow-schedule-audit.yml")
CHECKOUT = (
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found duplicate key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _workflow() -> dict[str, object]:
    loaded = yaml.load(
        WORKFLOW.read_text(encoding="utf-8"), Loader=UniqueKeyLoader
    )
    assert isinstance(loaded, dict)
    return loaded


def test_workflow_has_exact_completed_main_trigger_and_read_permissions():
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {
        "workflow_run": {
            "workflows": ["Shadow worker activation"],
            "types": ["completed"],
            "branches": ["main"],
        }
    }
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": (
            "kiwoom-shadow-schedule-audit-"
            "${{ github.event.workflow_run.id }}"
        ),
        "cancel-in-progress": False,
    }
    assert set(workflow["jobs"]) == {"audit"}
    job = workflow["jobs"]["audit"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 5
    assert job["permissions"] == {"actions": "read", "contents": "read"}
    assert "environment" not in job
    guard = " ".join(job["if"].split())
    assert guard == (
        "github.event.workflow_run.event == 'schedule' && "
        "github.event.workflow_run.head_branch == 'main' && "
        "github.event.workflow_run.head_repository.full_name == "
        "github.repository"
    )
    assert "conclusion" not in guard
    assert "status" not in guard


def test_workflow_checks_out_only_current_default_sha_and_treats_upstream_as_data(
):
    workflow = _workflow()
    steps = workflow["jobs"]["audit"]["steps"]
    assert len(steps) == 2
    checkout = steps[0]
    assert checkout["uses"] == CHECKOUT
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }
    run = steps[1]["run"]
    assert "workflow_run.head_sha" not in checkout["with"]["ref"]
    assert "actions/download-artifact" not in WORKFLOW.read_text(
        encoding="utf-8"
    )
    assert "unzip " not in run
    assert "tar " not in run
    assert '"${audit_dir}/artifact.zip"' in run
    assert "deploy/audit_shadow_schedule_bundle.py" in run
    assert "--auto-schedule" in run


def test_workflow_uses_exact_get_projections_original_zip_and_bounded_temp():
    workflow = _workflow()
    step = workflow["jobs"]["audit"]["steps"][1]
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "TRIGGER_RUN_ID": "${{ github.event.workflow_run.id }}",
        "EXPECTED_SOURCE_SHA": (
            "${{ vars.KIWOOM_SHADOW_SCHEDULE_SOURCE_SHA }}"
        ),
        "EXPECTED_IMAGE_DIGEST": (
            "${{ vars.KIWOOM_SHADOW_SCHEDULE_IMAGE_DIGEST }}"
        ),
    }
    run = step["run"]
    assert run.count("curl --fail --silent --show-error") == 4
    assert run.count("X-GitHub-Api-Version: 2022-11-28") == 4
    assert "--request" not in run
    assert "-X " not in run
    assert (
        "/actions/runs/${TRIGGER_RUN_ID}/artifacts?per_page=100" in run
    )
    assert "/actions/artifacts/${artifact_id}" in run
    assert "/actions/artifacts/${artifact_id}/zip" in run
    assert "--location" in run
    assert "--proto '=https'" in run
    assert "--proto-redir '=https'" in run
    for forwarding_option in (
        "--location-trusted", "--user", "--oauth2-bearer", "--netrc",
        "--proxy-header",
    ):
        assert forwarding_option not in run
    assert "--max-filesize 8388608" in run
    assert "|| audit_fail artifact_download_invalid" in run
    assert (
        'mktemp -d "${RUNNER_TEMP}/shadow-schedule-audit.XXXXXX"' in run
    )
    assert 'chmod 700 "${audit_dir}"' in run
    assert "trap cleanup EXIT" in run
    assert 'rm -rf -- "${audit_dir}"' in run
    assert run.index("trap cleanup EXIT") < run.index(
        'chmod 700 "${audit_dir}"'
    )
    assert "total_count" in run
    assert "workflow_run:{id:.workflow_run.id,head_sha:" in run
    assert run.count("deploy/prepare_shadow_schedule_audit.py") == 2
    assert "--artifact-json" in run


def test_workflow_has_no_privileged_or_write_capability_and_summary_is_fixed():
    text = WORKFLOW.read_text(encoding="utf-8")
    workflow = _workflow()
    serialized = str(workflow).lower()
    forbidden = (
        "secrets.",
        "id-token",
        "aws-actions/",
        "aws ",
        "ssm",
        "ec2",
        "slack",
        "actions/cache",
        "actions/upload-artifact",
        "contents: write",
        "actions: write",
        "checks: write",
        "issues: write",
        "pull-requests: write",
    )
    for value in forbidden:
        assert value not in text.lower()
    assert "environment" not in serialized
    assert "workflow_dispatch" not in text
    assert "GITHUB_STEP_SUMMARY" in text
    assert "expected_keys = {" in text
    assert 'value.get("status") != "PASS"' in text
    assert "len(raw) <= 4096" in text
    assert "upload" not in " ".join(
        step["name"].lower()
        for step in workflow["jobs"]["audit"]["steps"]
    )
    assert re.search(r"\b(post|put|patch|delete)\b", text.lower()) is None


def test_unique_key_loader_rejects_duplicate_workflow_mapping():
    text = WORKFLOW.read_text(encoding="utf-8")
    duplicated = text.replace(
        "permissions: {}\n", "permissions: {}\npermissions: {}\n", 1
    )
    with pytest.raises(yaml.constructor.ConstructorError):
        yaml.load(duplicated, Loader=UniqueKeyLoader)
