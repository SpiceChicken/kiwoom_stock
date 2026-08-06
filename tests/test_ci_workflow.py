"""Static checks for the GitHub Actions CI workflow."""

from pathlib import Path
import os
import subprocess

import pytest
import yaml

from kiwoom_stock.deployment import promotion


WORKFLOW_PATH = Path(".github/workflows/ci.yml")
CD_WORKFLOW_PATH = Path(".github/workflows/cd-production-check.yml")
PROMOTION_WORKFLOW_PATH = Path(
    ".github/workflows/cd-production-promotion.yml"
)
SHADOW_ROLLOUT_WORKFLOW_PATH = Path(
    ".github/workflows/cd-shadow-worker-rollout.yml"
)
DEPLOYMENT_BOUNDARY_DOC = Path("docs/operations/deployment-boundary.md")
CONTAINER_DEPLOYMENT_DOC = Path(
    "docs/operations/github-ec2-container-deployment.md"
)
OIDC_BOOTSTRAP_DOC = Path("docs/operations/github-oidc-aws-bootstrap.md")
CHECKOUT_ACTION = (
    "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
)
SETUP_PYTHON_ACTION = (
    "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1"
)
UPLOAD_ARTIFACT_ACTION = (
    "actions/upload-artifact@b7c566a772e6b6bfb58ed0dc250532a479d7789f"
)
AWS_CREDENTIALS_ACTION = (
    "aws-actions/configure-aws-credentials@"
    "e6de054238d6b7531b4efff3b6587d9aade6a06c"
)
GITLEAKS_BINARY_SHA256 = (
    "88f91962aa2f93ac6ab281d553b9e125f5197bbbce38f9f2437f7299c32e5509"
)
GITLEAKS_ARCHIVE_SHA256 = (
    "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
)


def _workflow():
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _cd_workflow():
    if not CD_WORKFLOW_PATH.is_file():
        pytest.skip(
            "CD workflow metadata is intentionally absent from the Docker test stage"
        )
    return yaml.safe_load(CD_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _promotion_workflow():
    return yaml.safe_load(
        PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")
    )


def test_shadow_rollout_cd_has_exact_protected_source_only_wiring():
    workflow = yaml.safe_load(
        SHADOW_ROLLOUT_WORKFLOW_PATH.read_text(encoding="utf-8")
    )
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"source_sha"}
    assert workflow["concurrency"] == {
        "group": "kiwoom-stock-shadow-i-02cb0a404794bd43a",
        "cancel-in-progress": False,
    }
    text = SHADOW_ROLLOUT_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "vars.KIWOOM_AWS_SHADOW_ROLLOUT_ROLE_ARN" in text
    assert "ref: ${{ inputs.source_sha }}" in text
    assert "github.ref == 'refs/heads/main'" in text


def _single_python_heredoc(step):
    script = step["run"]
    marker = "<<'PY'\n"
    start = script.index(marker) + len(marker)
    end = script.index("\nPY", start)
    return script[start:end]


def test_ci_uses_minimum_permissions_and_cancels_stale_runs():
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert workflow["concurrency"]["group"] == "ci-${{ github.workflow }}-${{ github.ref }}"


def test_ci_does_not_materialize_runtime_secrets():
    text = WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "secrets." not in text
    assert "pull_request_target" not in text
    assert "Create Config Files" not in text
    assert "CONFIG_JSON" not in text
    assert "KIWOOM_APP_KEY:" not in text
    assert "KIWOOM_SECRET_KEY:" not in text


def test_ci_pins_every_action_to_an_approved_full_sha():
    workflow = _workflow()
    approved = {
        CHECKOUT_ACTION,
        SETUP_PYTHON_ACTION,
        UPLOAD_ARTIFACT_ACTION,
    }
    action_refs = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert action_refs
    assert set(action_refs) <= approved
    assert all(
        len(action_ref.rsplit("@", 1)[1]) == 40
        and all(char in "0123456789abcdef" for char in action_ref.rsplit("@", 1)[1])
        for action_ref in action_refs
    )


def test_ci_secret_scan_is_read_only_redacted_and_covers_full_history():
    secret_scan = _workflow()["jobs"]["secret-scan"]
    steps = secret_scan["steps"]
    checkout = next(step for step in steps if step.get("uses") == CHECKOUT_ACTION)
    install = next(
        step for step in steps if step["name"] == "Install checksum-verified Gitleaks"
    )
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    assert secret_scan["permissions"] == {"contents": "read"}
    assert checkout["with"] == {
        "fetch-depth": 0,
        "persist-credentials": False,
    }
    assert steps.index(install) == steps.index(checkout) + 1
    assert install["env"] == {
        "GITLEAKS_ARCHIVE_SHA256": GITLEAKS_ARCHIVE_SHA256,
        "GITLEAKS_BINARY_SHA256": GITLEAKS_BINARY_SHA256,
    }
    assert "gitleaks/gitleaks-action@" not in WORKFLOW_PATH.read_text(
        encoding="utf-8"
    )
    assert "GITHUB_TOKEN" not in WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "uname -m" in run_blocks
    assert '"x86_64"' in run_blocks
    assert "gitleaks_8.30.1_linux_x64.tar.gz" in run_blocks
    assert "curl --fail --silent --show-error --location" in run_blocks
    assert "--proto '=https' --tlsv1.2" in run_blocks
    assert '--log-opts="--all HEAD"' in run_blocks
    assert "Gitleaks repository history scan failed" in run_blocks
    assert "gitleaks dir --redact --no-banner" in run_blocks
    assert "--exit-code 97" in run_blocks
    assert 'base64.urlsafe_b64encode(os.urandom(48)).decode("ascii")' in run_blocks
    assert "python3 -" in run_blocks
    assert "positive-underscore" in run_blocks
    assert "positive-app-hyphen" in run_blocks
    assert "positive-secret-hyphen" in run_blocks
    assert "positive-app-compact" in run_blocks
    assert "positive-secret-compact" in run_blocks
    assert "app-key:" in run_blocks
    assert "secret-key =" in run_blocks
    assert '"appkey"' in run_blocks
    assert '"secretkey"' in run_blocks
    assert "KIWOOM_SECRET_KEY_FILE=" in run_blocks
    assert "KIWOOM_APP_KEY=${VARIABLE}" in run_blocks
    assert ">/dev/null 2>&1" in run_blocks


def test_ci_verifies_gitleaks_artifacts_before_extraction_and_execution():
    steps = _workflow()["jobs"]["secret-scan"]["steps"]
    install = next(
        step for step in steps if step["name"] == "Install checksum-verified Gitleaks"
    )
    install_script = install["run"]
    archive_check = (
        'if test "${archive_digest}" != "${GITLEAKS_ARCHIVE_SHA256}"; then'
    )
    extraction = 'tar -xzf "${archive}"'
    binary_check = (
        'if test "${binary_digest}" != "${GITLEAKS_BINARY_SHA256}"; then'
    )
    exact_version_execution = (
        'reported_version="$("${tool_dir}/gitleaks" version 2>/dev/null)"'
    )
    exact_version_check = 'if test "${reported_version}" != "8.30.1"; then'
    publish_path = 'echo "${tool_dir}" >> "${GITHUB_PATH}"'

    assert (
        install_script.index(archive_check)
        < install_script.index(extraction)
        < install_script.index(binary_check)
        < install_script.index(exact_version_execution)
        < install_script.index(exact_version_check)
        < install_script.index(publish_path)
    )

    scanner_steps = [
        step
        for step in steps
        if "gitleaks git" in step.get("run", "")
    ]
    assert scanner_steps
    first_scanner = scanner_steps[0]
    assert steps.index(install) < steps.index(first_scanner)
    assert "gitleaks git --redact --no-banner --config .gitleaks.toml" in (
        first_scanner["run"]
    )


def test_ci_quality_job_uses_supported_python_matrix_and_pip_cache():
    workflow = _workflow()
    quality = workflow["jobs"]["quality"]
    steps = quality["steps"]

    assert quality["needs"] == "secret-scan"
    assert quality["strategy"]["matrix"]["python-version"] == ["3.11", "3.14"]
    assert any(step.get("uses") == CHECKOUT_ACTION for step in steps)

    setup_steps = [step for step in steps if step.get("uses") == SETUP_PYTHON_ACTION]
    assert len(setup_steps) == 1
    assert setup_steps[0]["with"]["cache"] == "pip"
    assert setup_steps[0]["with"]["cache-dependency-path"] == "pyproject.toml"

    run_blocks = "\n".join(step.get("run", "") for step in steps)
    assert 'python -m pip install -e ".[dev]"' in run_blocks
    assert "python -m mypy src/kiwoom_stock" in run_blocks
    assert "python -m pytest tests --junitxml=reports/pytest-${{ matrix.python-version }}.xml" in run_blocks


def test_ci_package_job_builds_and_smokes_installed_wheel():
    workflow = _workflow()
    package = workflow["jobs"]["package"]
    steps = package["steps"]

    assert package["needs"] == "quality"
    assert any(step.get("uses") == CHECKOUT_ACTION for step in steps)
    assert any(step.get("uses") == SETUP_PYTHON_ACTION for step in steps)
    assert any(step.get("uses") == UPLOAD_ARTIFACT_ACTION for step in steps)

    run_blocks = "\n".join(step.get("run", "") for step in steps)
    assert "python -m build" in run_blocks
    assert "python -m venv .wheel-smoke" in run_blocks
    assert ".wheel-smoke/bin/python -m pip install dist/*.whl" in run_blocks
    assert '"${wheel_smoke_python}" -m kiwoom_stock --check-config' in run_blocks
    assert "grep -q KIWOOM_PROCESS_NAME /tmp/check-config.err" in run_blocks
    assert 'cd "${RUNNER_TEMP}"' in run_blocks
    assert "pkgutil.walk_packages" in run_blocks
    assert '"kiwoom_stock.__main__"' in run_blocks
    assert 'importlib.import_module("kiwoom_stock.application.lifecycle")' in run_blocks
    assert 'importlib.import_module("kiwoom_stock.monitoring.reporter")' in run_blocks
    assert 'resources.files("kiwoom_stock.resources.prompts")' in run_blocks


def test_cd_candidate_is_manual_unprivileged_and_serialized_without_cancel():
    workflow = _cd_workflow()
    triggers = workflow.get("on", workflow.get(True))
    build = workflow["jobs"]["build_publish"]
    seal = workflow["jobs"]["seal_release_manifest"]

    assert set(triggers) == {"workflow_dispatch"}
    source_input = triggers["workflow_dispatch"]["inputs"]["source_sha"]
    assert source_input["required"] is True
    assert "default" not in source_input
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "kiwoom-stock-production-check-i-02cb0a404794bd43a",
        "cancel-in-progress": False,
    }
    assert build["permissions"] == {
        "contents": "read",
        "packages": "write",
    }
    assert "environment" not in build
    assert seal["permissions"] == {"actions": "read"}
    assert "environment" not in seal
    assert seal["needs"] == "build_publish"
    assert build["if"] == "github.ref == 'refs/heads/main'"
    assert seal["if"] == "github.ref == 'refs/heads/main'"
    assert set(workflow["jobs"]) == {
        "build_publish",
        "seal_release_manifest",
    }


@pytest.mark.parametrize(
    ("requested_sha", "trigger_sha", "expected_status"),
    [
        ("a" * 40, "a" * 40, 0),
        ("a" * 40, "b" * 40, 1),
    ],
)
def test_cd_dispatch_source_must_equal_the_exact_main_trigger_sha(
    tmp_path,
    requested_sha,
    trigger_sha,
    expected_status,
):
    step = next(
        item
        for item in _cd_workflow()["jobs"]["build_publish"]["steps"]
        if item["name"] == "Validate immutable source input"
    )
    output = tmp_path / "github-output"
    environment = dict(os.environ)
    environment.update(
        {
            "REQUESTED_SOURCE_SHA": requested_sha,
            "GITHUB_TRIGGER_SHA": trigger_sha,
            "GITHUB_OUTPUT": str(output),
        }
    )

    completed = subprocess.run(
        ["bash", "-c", step["run"]],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == expected_status
    if expected_status == 0:
        assert output.read_text(encoding="utf-8") == f"sha={requested_sha}\n"
    else:
        assert not output.exists()


def test_cd_pins_actions_and_never_receives_kiwoom_credentials():
    workflow = _cd_workflow()
    text = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    action_refs = [
        step["uses"]
        for job in workflow["jobs"].values()
        for step in job["steps"]
        if "uses" in step
    ]

    assert set(action_refs) == {
        CHECKOUT_ACTION,
        SETUP_PYTHON_ACTION,
        UPLOAD_ARTIFACT_ACTION,
    }
    assert all(len(ref.rsplit("@", 1)[1]) == 40 for ref in action_refs)
    assert "secrets." not in text
    assert "KIWOOM_APP_KEY:" not in text
    assert "KIWOOM_SECRET_KEY:" not in text
    assert "ssm get-parameter" not in text.lower()
    assert "ssm get-parameters" not in text.lower()
    assert "aws-actions/" not in text
    assert "aws ssm" not in text
    assert "environment: production" not in text
    assert "id-token: write" not in text


def test_cd_candidate_execution_has_no_oidc_or_production_environment():
    workflow = _cd_workflow()
    build = workflow["jobs"]["build_publish"]
    steps = build["steps"]
    names = [step["name"] for step in steps]
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    required = {
        "Scan complete source history for credentials",
        "Lint, type-check, and run full tests",
        "Build package and smoke installed wheel",
        "Build and execute container test stage",
        "Build and inspect immutable runtime candidate",
        "Publish or reuse exact full-SHA GHCR tag",
        "Prove anonymous public digest pull",
    }
    assert required <= set(names)
    assert "id-token" not in build["permissions"]
    assert "environment" not in build
    assert AWS_CREDENTIALS_ACTION not in {
        step.get("uses") for step in steps
    }
    assert "python -m flake8" in run_blocks
    assert "python -m mypy src/kiwoom_stock" in run_blocks
    assert "python -m pytest tests" in run_blocks
    assert "python -m build" in run_blocks
    assert "--target test" in run_blocks
    assert "--target runtime" in run_blocks
    assert 'docker pull "${IMAGE_DIGEST}"' in run_blocks
    assert "MAX_RUNTIME_IMAGE_MIB" in run_blocks


def test_cd_reuses_existing_tag_without_overwrite_or_local_rebuild_equality():
    text = CD_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "git rev-parse HEAD" in text
    assert "REQUESTED_SOURCE_SHA" in text
    assert "^\\[0-9a-f\\]{40}$" not in text
    assert "^[0-9a-f]{40}$" in text
    assert ":sha-${SOURCE_SHA}" in text
    assert "org.opencontainers.image.revision" in text
    assert "@sha256:[0-9a-f]{64}" in text
    assert ":latest" not in text
    assert "docker push" in text
    assert 'if [[ "${tag_exists}" == "true" ]]; then' in text
    assert 'docker pull "${{ steps.image.outputs.remote_tag }}"' in text
    assert "existing full-SHA tag differs" not in text
    assert "local_id=" not in text
    assert "remote_id=" not in text
    assert "docker logout ghcr.io" in text
    assert "DOCKER_CONFIG=" in text
    assert "compose_sha256" in text
    assert "compose_prod_sha256" in text


def test_cd_seals_strict_bounded_unique_release_manifest():
    workflow = _cd_workflow()
    text = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    seal = workflow["jobs"]["seal_release_manifest"]
    runs = "\n".join(
        step.get("run", "") for step in seal["steps"]
    )

    assert "release-manifest-${{ needs.build_publish.outputs.source_sha }}-${{ github.run_id }}" in text
    assert "release-manifest.json" in text
    assert "candidate-${{ steps.release.outputs.source_sha }}" not in text
    assert "pytest-production-check.xml" not in text
    assert "runtime-image-inspect.json" not in text
    uploads = [
        step for job in workflow["jobs"].values() for step in job["steps"]
        if step.get("uses") == UPLOAD_ARTIFACT_ACTION
    ]
    assert len(uploads) == 1
    assert uploads[0] in seal["steps"]
    assert uploads[0]["with"]["if-no-files-found"] == "error"
    assert "16 * 1024" in runs
    assert "total_count" in runs
    assert "> 100" in runs
    assert "validate and publish immutable candidate" in runs
    assert 'job.get("status") != "completed"' in runs
    assert 'job.get("conclusion") != "success"' in runs
    for key in (
        "schema_version",
        "source_sha",
        "image_digest",
        "image_size_mib",
        "compose_sha256",
        "compose_prod_sha256",
        "build_run_id",
        "build_job_id",
    ):
        assert f'"{key}"' in runs




def test_promotion_has_exact_inputs_permissions_and_protected_tuple():
    workflow = _promotion_workflow()
    triggers = workflow.get("on", workflow.get(True))
    promote = workflow["jobs"]["promote"]

    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {
        "source_sha", "image_digest", "build_run_id"
    }
    assert all(
        value["required"] is True and "default" not in value
        for value in triggers["workflow_dispatch"]["inputs"].values()
    )
    assert workflow["permissions"] == {}
    assert workflow["concurrency"] == {
        "group": "kiwoom-stock-production-check-i-02cb0a404794bd43a",
        "cancel-in-progress": False,
    }
    assert promote["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert promote["environment"] == "production"
    assert promote["if"] == "github.ref == 'refs/heads/main'"
    assert "GH_TOKEN" not in promote["env"]
    assert all(not key.startswith("AWS_ACCESS_KEY") for key in promote["env"])


def test_promotion_is_thin_pinned_and_orders_the_trust_boundary():
    workflow = _promotion_workflow()
    steps = workflow["jobs"]["promote"]["steps"]
    names = [step["name"] for step in steps]
    action_refs = {step["uses"] for step in steps if "uses" in step}

    assert action_refs == {
        CHECKOUT_ACTION,
        AWS_CREDENTIALS_ACTION,
        UPLOAD_ARTIFACT_ACTION,
    }
    assert all(len(ref.rsplit("@", 1)[1]) == 40 for ref in action_refs)
    checkout = steps[0]
    assert checkout["name"] == "Checkout trusted promotion executor"
    assert checkout["with"] == {
        "ref": "${{ github.sha }}",
        "fetch-depth": 1,
        "persist-credentials": False,
    }
    assert names == [
        "Checkout trusted promotion executor",
        "Initialize bounded redacted promotion evidence",
        "Validate immutable protected boundary preflight",
        "Configure exact AWS deploy role with OIDC",
        "Execute authoritative production-check boundary",
        "Clear OIDC credentials before artifact handling",
        "Upload protected promotion evidence",
    ]
    oidc_index = names.index("Configure exact AWS deploy role with OIDC")
    assert names[oidc_index + 1] == (
        "Execute authoritative production-check boundary"
    )
    assert names[oidc_index + 2] == (
        "Clear OIDC credentials before artifact handling"
    )
    preflight = steps[names.index("Validate immutable protected boundary preflight")]
    execute = steps[names.index("Execute authoritative production-check boundary")]
    assert "promotion preflight" in preflight["run"]
    assert "promotion execute" in execute["run"]
    assert names.index("Checkout trusted promotion executor") < names.index(
        "Validate immutable protected boundary preflight"
    ) < oidc_index < names.index(
        "Execute authoritative production-check boundary"
    ) < names.index(
        "Clear OIDC credentials before artifact handling"
    ) < names.index("Upload protected promotion evidence")


def test_promotion_operations_docs_match_trust_order_and_terminal_cleanup():
    documents = [
        " ".join(path.read_text(encoding="utf-8").split())
        for path in (
            DEPLOYMENT_BOUNDARY_DOC,
            CONTAINER_DEPLOYMENT_DOC,
            OIDC_BOOTSTRAP_DOC,
        )
    ]
    required = (
        "checkout",
        "Node 24 OIDC outputs",
        "authoritative",
        "credential clear",
        "evidence upload",
        "terminal success/failure/cancel",
        "role-only",
        "secrets `0`",
        "pending deployments `0`",
    )
    for document in documents:
        assert all(phrase in document for phrase in required)

    combined = "\n".join(documents)
    for stale in (
        "checkout 없는 promotion job",
        "pinned system `curl`",
        "제거하거나 다음 승인 tuple로 교체",
        "이 모든 검증 뒤에만 OIDC role",
        "검증 전 OIDC를 얻지 않는다",
    ):
        assert stale not in combined


def test_promotion_cli_commands_have_no_pre_oidc_derived_state_transport():
    workflow = _promotion_workflow()
    steps = workflow["jobs"]["promote"]["steps"]
    text = PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")
    preflight = next(
        step for step in steps
        if step["name"] == "Validate immutable protected boundary preflight"
    )
    execute = next(
        step for step in steps
        if step["name"] == "Execute authoritative production-check boundary"
    )

    assert "id" not in preflight
    assert "GITHUB_OUTPUT" not in preflight["run"]
    assert "GITHUB_ENV" not in preflight["run"]
    assert "steps." not in preflight["run"]
    assert "outputs:" not in text
    assert "needs." not in text
    assert "promotion-ssm-parameters" not in text
    assert "COMPOSE_SHA256" not in text
    assert "COMPOSE_PROD_SHA256" not in text
    assert "IMAGE_SIZE_MIB" not in text
    assert "PYTHONPATH" in execute["env"]
    assert "python -m kiwoom_stock.deployment.promotion execute" in execute["run"]
    assert "aws ssm" not in text
    assert "docker pull" not in text
    assert "curl " not in text
    assert "<<" not in text


def test_promotion_scopes_github_and_oidc_credentials_and_clears_before_upload():
    steps = _promotion_workflow()["jobs"]["promote"]["steps"]
    oidc = next(step for step in steps if step.get("id") == "oidc")
    execute = next(
        step for step in steps
        if step["name"] == "Execute authoritative production-check boundary"
    )
    clear = next(
        step for step in steps
        if step["name"] == "Clear OIDC credentials before artifact handling"
    )
    upload = next(step for step in steps if step.get("uses") == UPLOAD_ARTIFACT_ACTION)

    assert oidc["uses"] == AWS_CREDENTIALS_ACTION
    assert oidc["with"] == {
        "role-to-assume": "${{ env.AWS_DEPLOY_ROLE_ARN }}",
        "aws-region": "${{ env.AWS_REGION }}",
        "role-session-name": "kiwoom-production-check",
        "output-credentials": True,
        "output-env-credentials": False,
        "unset-current-credentials": True,
    }
    assert execute["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert execute["env"]["AWS_ACCESS_KEY_ID"] == (
        "${{ steps.oidc.outputs.aws-access-key-id }}"
    )
    assert execute["env"]["AWS_SECRET_ACCESS_KEY"] == (
        "${{ steps.oidc.outputs.aws-secret-access-key }}"
    )
    assert execute["env"]["AWS_SESSION_TOKEN"] == (
        "${{ steps.oidc.outputs.aws-session-token }}"
    )
    assert clear["if"] == "always()"
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_REGION", "AWS_DEFAULT_REGION",
    ):
        assert f"echo '{name}='" in clear["run"]
    assert "env" not in upload
    assert upload["if"] == "always()"
    assert steps.index(clear) + 1 == steps.index(upload)


def test_promotion_preserves_fixed_allowlists_and_no_business_credentials():
    text = PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "e3dd6a429d7300a6a4c196c26e071d42e0343502" not in text
    assert "aws-actions/configure-aws-credentials@v6" not in text
    assert "ACTIONS_ALLOW_USE_UNSECURE_NODE_VERSION" not in text
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" not in text
    assert "secrets." not in text
    assert "KIWOOM_APP_KEY" not in text
    assert "KIWOOM_SECRET_KEY" not in text
    assert "arn:aws:iam::380648615401" not in text
    assert "i-02cb0a404794bd43a" in text
    assert "ap-northeast-2" in text
    assert "AWS-RunShellScript" not in text
    assert ":latest" not in text
    assert "docker compose up" not in text
    assert "production-check-evidence.json" in text
    assert "if-no-files-found: error" in text


def test_promotion_attempt_id_and_execution_budget_leave_cleanup_margin():
    promote = _promotion_workflow()["jobs"]["promote"]
    steps = promote["steps"]
    run_blocks = "\n".join(
        step.get("run", "") for step in promote["steps"]
    )

    assert promote["timeout-minutes"] == 25
    assert promote["env"]["PROMOTION_ATTEMPT_ID"] == "${{ github.run_id }}"
    assert run_blocks.count(
        '--promotion-attempt-id "${PROMOTION_ATTEMPT_ID}"'
    ) == 3
    assert promotion.EXECUTION_BUDGET_SECONDS < 25 * 60
    assert all("timeout-minutes" in step for step in steps)
    declared = [step["timeout-minutes"] for step in steps]
    assert declared == [1, 1, 1, 1, 18, 1, 1]
    assert sum(declared) <= promote["timeout-minutes"] - 1
    execute_step = next(
        step for step in steps
        if step["name"] == "Execute authoritative production-check boundary"
    )
    assert execute_step["timeout-minutes"] * 60 - (
        promotion.EXECUTION_BUDGET_SECONDS
    ) >= 120
    cleanup_minutes = sum(
        step["timeout-minutes"] for step in steps[-2:]
    )
    assert cleanup_minutes == 2
