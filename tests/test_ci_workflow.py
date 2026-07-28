"""Static checks for the GitHub Actions CI workflow."""

from pathlib import Path

import pytest
import yaml


WORKFLOW_PATH = Path(".github/workflows/ci.yml")
CD_WORKFLOW_PATH = Path(".github/workflows/cd-production-check.yml")
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
    "e3dd6a429d7300a6a4c196c26e071d42e0343502"
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


def test_cd_is_manual_production_environment_and_serialized_without_cancel():
    workflow = _cd_workflow()
    triggers = workflow.get("on", workflow.get(True))
    build = workflow["jobs"]["build_publish"]
    deploy = workflow["jobs"]["deploy"]

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
    assert deploy["permissions"] == {"id-token": "write"}
    assert deploy["environment"] == "production"
    assert deploy["needs"] == "build_publish"
    assert build["if"] == "github.ref == 'refs/heads/main'"
    assert deploy["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["env"]["EC2_INSTANCE_ID"] == "i-02cb0a404794bd43a"
    assert workflow["env"]["AWS_REGION"] == "ap-northeast-2"


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
        AWS_CREDENTIALS_ACTION,
    }
    assert all(len(ref.rsplit("@", 1)[1]) == 40 for ref in action_refs)
    assert "secrets." not in text
    assert "KIWOOM_APP_KEY:" not in text
    assert "KIWOOM_SECRET_KEY:" not in text
    assert "ssm get-parameter" not in text.lower()
    assert "ssm get-parameters" not in text.lower()


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


def test_cd_protected_deploy_executes_no_candidate_checkout_or_code():
    workflow = _cd_workflow()
    deploy = workflow["jobs"]["deploy"]
    steps = deploy["steps"]
    uses = {step.get("uses") for step in steps if step.get("uses")}
    run_blocks = "\n".join(step.get("run", "") for step in steps)

    assert CHECKOUT_ACTION not in uses
    assert SETUP_PYTHON_ACTION not in uses
    assert uses == {AWS_CREDENTIALS_ACTION, UPLOAD_ARTIFACT_ACTION}
    assert "docker " not in run_blocks
    assert "git " not in run_blocks
    assert "pip " not in run_blocks
    assert "python -m" not in run_blocks
    assert "source_sha" in deploy["env"]["SOURCE_SHA"]
    assert "image_digest" in deploy["env"]["IMAGE_DIGEST"]
    assert "compose_sha256" in deploy["env"]["COMPOSE_SHA256"]
    assert "Revalidate protected release tuple" in {
        step["name"] for step in steps
    }


def test_cd_publishes_and_deploys_only_an_exact_digest_from_full_source_sha():
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
    assert "existing full-SHA tag differs; overwrite refused" in text
    assert 'test "${local_id}" = "${remote_id}"' in text
    assert "docker logout ghcr.io" in text
    assert "DOCKER_CONFIG=" in text
    assert "compose_sha256" in text
    assert "compose_prod_sha256" in text


def test_cd_ssm_command_is_allowlisted_bounded_and_check_only():
    workflow = _cd_workflow()
    text = CD_WORKFLOW_PATH.read_text(encoding="utf-8")
    deploy_runs = "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["deploy"]["steps"]
    )

    assert "AWS-RunShellScript" not in text
    assert "--document-name KiwoomStock-ProductionCheck" in text
    assert "--instance-ids \"${EC2_INSTANCE_ID}\"" in text
    assert "--timeout-seconds 750" in text
    assert '"ImageDigest": [os.environ["IMAGE_DIGEST"]]' in text
    assert '"ComposeSha256": [os.environ["COMPOSE_SHA256"]]' in text
    assert '"commands"' not in deploy_runs
    assert "raw.githubusercontent.com" not in deploy_runs
    assert "production-check-evidence.json" in text
    assert '"secrets_in_github": False' in text
    assert '"worker_activated": False' in text
    assert "docker compose up" not in text
    assert " up -d" not in text
    assert "--rollback-check" not in text
