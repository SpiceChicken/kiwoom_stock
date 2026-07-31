"""Static checks for the GitHub Actions CI workflow."""

import base64
import hashlib
import json
from pathlib import Path
import os
import subprocess
import sys
import zipfile

import pytest
import yaml


WORKFLOW_PATH = Path(".github/workflows/ci.yml")
CD_WORKFLOW_PATH = Path(".github/workflows/cd-production-check.yml")
PROMOTION_WORKFLOW_PATH = Path(
    ".github/workflows/cd-production-promotion.yml"
)
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


def _promotion_workflow():
    return yaml.safe_load(
        PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")
    )


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
    inputs = triggers["workflow_dispatch"]["inputs"]

    assert set(triggers) == {"workflow_dispatch"}
    assert set(inputs) == {"source_sha", "image_digest", "build_run_id"}
    assert all(value["required"] is True for value in inputs.values())
    assert all("default" not in value for value in inputs.values())
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
    assert promote["env"]["APPROVED_SOURCE_SHA"] == (
        "${{ vars.KIWOOM_APPROVED_SOURCE_SHA }}"
    )
    assert promote["env"]["APPROVED_IMAGE_DIGEST"] == (
        "${{ vars.KIWOOM_APPROVED_IMAGE_DIGEST }}"
    )
    assert promote["env"]["APPROVED_BUILD_RUN_ID"] == (
        "${{ vars.KIWOOM_APPROVED_BUILD_RUN_ID }}"
    )


@pytest.mark.parametrize(
    ("source", "approved_source", "expected_status"),
    [
        ("a" * 40, "a" * 40, 0),
        ("a" * 40, "b" * 40, 1),
    ],
)
def test_promotion_environment_tuple_must_match_before_provenance(
    source,
    approved_source,
    expected_status,
):
    steps = _promotion_workflow()["jobs"]["promote"]["steps"]
    step = next(
        step
        for step in steps
        if step["name"] == "Match protected Environment release tuple"
    )
    digest = (
        "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "c" * 64
    )
    environment = dict(os.environ)
    environment.update(
        {
            "SOURCE_SHA": source,
            "IMAGE_DIGEST": digest,
            "BUILD_RUN_ID": "123",
            "APPROVED_SOURCE_SHA": approved_source,
            "APPROVED_IMAGE_DIGEST": digest,
            "APPROVED_BUILD_RUN_ID": "123",
            "AWS_DEPLOY_ROLE_ARN": (
                "arn:aws:iam::380648615401:"
                "role/kiwoom-stock-github-production-check"
            ),
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "AWS_REGION": "ap-northeast-2",
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


def test_promotion_provenance_and_legacy_exception_are_exact_and_fail_closed():
    workflow = _promotion_workflow()
    steps = workflow["jobs"]["promote"]["steps"]
    text = PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")
    provenance = next(
        step for step in steps
        if step["name"] == "Verify candidate run job and unique artifact provenance"
    )["run"]

    assert "allow_legacy" not in text.lower()
    assert "30544114256" in provenance
    assert "90875823290" in provenance
    assert "90b0f00f32e8db0b327d90aa3d053f520d2d3f1b" in provenance
    assert (
        "faa437771719203165c2de57bfd8f122"
        "99ddfcc1c5d014772f1af86b3c71093d"
    ) in provenance
    assert '".github/workflows/cd-production-check.yml"' in provenance
    assert '"workflow_dispatch"' in provenance
    assert '"head_branch": "main"' in provenance
    assert '"status": "completed"' in provenance
    assert '"cancelled" if legacy else "success"' in provenance
    assert "exactly one candidate build job is required" in provenance
    assert "exactly one expected release artifact is required" in provenance
    assert 'artifact.get("expired") is not False' in provenance
    assert 'artifact_run.get("id") != run_id' in provenance
    assert 'artifact.get("digest")' in provenance
    assert 're.fullmatch(r"sha256:[0-9a-f]{64}"' in provenance


def test_promotion_artifact_manifest_zip_and_contents_checks_are_strict():
    workflow = _promotion_workflow()
    steps = workflow["jobs"]["promote"]["steps"]
    artifact = next(
        step for step in steps
        if step["name"] == "Validate bounded release artifact and strict manifest"
    )["run"]
    compose = next(
        step for step in steps
        if step["name"]
        == "Verify Compose and publish bounded public preparation outputs"
    )["run"]

    assert "--max-filesize" in artifact
    assert '"${ARTIFACT_SIZE}"' in artifact
    assert '"${ARTIFACT_DIGEST}"' in artifact
    assert "sha256sum" in artifact
    assert "PurePosixPath" in artifact
    assert "path.is_absolute()" in artifact
    assert '".." in path.parts' in artifact
    assert "stat.S_ISLNK" in artifact
    assert "set(manifest) != expected_manifest_keys" in artifact
    assert "type(manifest[key]) is not str" in artifact
    assert "type(manifest[key]) is not int" in artifact
    assert "release manifest tuple mismatch" in artifact
    assert "release manifest exceeds 16 KiB" in artifact
    assert "/contents/${compose_path}?ref=${SOURCE_SHA}" in compose
    assert 'response.get("encoding") != "base64"' in compose
    assert "hashlib.sha256(payload).hexdigest()" in compose
    assert "manifest and source Compose hashes differ" in compose


def _run_manifest_validator(tmp_path, manifest, *, mode="manifest", unsafe=False):
    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Validate bounded release artifact and strict manifest"
    )
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        if mode == "manifest":
            info = zipfile.ZipInfo("release-manifest.json")
            if unsafe:
                info.external_attr = 0o120777 << 16
            bundle.writestr(info, json.dumps(manifest))
        else:
            bundle.writestr(
                "reports/pytest-production-check.xml",
                "<testsuite/>",
            )
            bundle.writestr(
                "runtime-image-inspect.json",
                json.dumps(
                    [
                        {
                            "Config": {
                                "Entrypoint": [
                                    "python",
                                    (
                                        "/usr/local/bin/"
                                        "kiwoom-runtime-entrypoint.py"
                                    ),
                                ],
                                "User": "10001:10001",
                                "Labels": {
                                    "org.opencontainers.image.revision": (
                                        "a" * 40
                                    )
                                },
                            }
                        }
                    ]
                ),
            )
    output = tmp_path / "github-output"
    environment = dict(os.environ)
    environment.update(
        {
            "RELEASE_MODE": mode,
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "BUILD_JOB_ID": "456",
            "MAX_RUNTIME_IMAGE_MIB": "850",
            "GITHUB_OUTPUT": str(output),
        }
    )
    return subprocess.run(
        [sys.executable, "-", str(archive)],
        input=_single_python_heredoc(step),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _valid_release_manifest():
    return {
        "schema_version": 1,
        "source_sha": "a" * 40,
        "image_digest": (
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
        ),
        "image_size_mib": 400,
        "compose_sha256": "c" * 64,
        "compose_prod_sha256": "d" * 64,
        "build_run_id": 123,
        "build_job_id": 456,
    }


def test_promotion_manifest_validator_accepts_exact_modern_and_legacy_contract(
    tmp_path,
):
    modern = _run_manifest_validator(tmp_path, _valid_release_manifest())
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    legacy = _run_manifest_validator(
        legacy_dir,
        {},
        mode="legacy",
    )

    assert modern.returncode == 0, modern.stderr
    assert legacy.returncode == 0, legacy.stderr


@pytest.mark.parametrize("mutation", ["extra", "tuple", "bool", "size", "symlink"])
def test_promotion_manifest_validator_rejects_malformed_contract(
    tmp_path,
    mutation,
):
    manifest = _valid_release_manifest()
    unsafe = False
    if mutation == "extra":
        manifest["unexpected"] = "value"
    elif mutation == "tuple":
        manifest["source_sha"] = "e" * 40
    elif mutation == "bool":
        manifest["build_job_id"] = True
    elif mutation == "size":
        manifest["image_size_mib"] = 851
    elif mutation == "symlink":
        unsafe = True

    completed = _run_manifest_validator(
        tmp_path,
        manifest,
        unsafe=unsafe,
    )

    assert completed.returncode != 0


def _run_initial_promotion_evidence(tmp_path):
    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Initialize bounded redacted promotion evidence"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "EVIDENCE_FILENAME": "production-check-evidence.json",
        }
    )
    return subprocess.run(
        [sys.executable, "-"],
        input=_single_python_heredoc(step),
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )


def _run_compose_preparation(tmp_path, mutation=None):
    initialized = _run_initial_promotion_evidence(tmp_path)
    assert initialized.returncode == 0, initialized.stderr

    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"]
        == "Verify Compose and publish bounded public preparation outputs"
    )
    payloads = {
        "compose.yaml": b"services:\n  app: {}\n",
        "compose.prod.yaml": b"services:\n  app:\n    read_only: true\n",
    }
    paths = []
    for name, payload in payloads.items():
        response = tmp_path / f"{name}.json"
        encoded = base64.b64encode(payload).decode("ascii")
        if mutation == "empty" and name == "compose.prod.yaml":
            encoded = ""
        elif mutation == "malformed" and name == "compose.prod.yaml":
            encoded = "!!!!"
        response.write_text(
            json.dumps(
                {
                    "type": "file",
                    "path": name,
                    "encoding": "base64",
                    "content": f"{encoded[:8]}\n{encoded[8:]}",
                }
            ),
            encoding="utf-8",
        )
        paths.append(str(response))
    compose_hash = hashlib.sha256(payloads["compose.yaml"]).hexdigest()
    compose_prod_hash = hashlib.sha256(
        payloads["compose.prod.yaml"]
    ).hexdigest()
    expected_prod_hash = (
        "0" * 64 if mutation == "mismatch" else compose_prod_hash
    )
    output_path = tmp_path / "github-preparation-output"
    evidence_path = tmp_path / "production-check-evidence.json"
    environment = dict(os.environ)
    environment.update(
        {
            "RELEASE_MODE": "manifest",
            "MANIFEST_COMPOSE_SHA256": compose_hash,
            "MANIFEST_COMPOSE_PROD_SHA256": expected_prod_hash,
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "IMAGE_SIZE_MIB": "400",
            "MAX_RUNTIME_IMAGE_MIB": "850",
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "AWS_REGION": "ap-northeast-2",
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_OUTPUT": str(output_path),
            "EVIDENCE_FILENAME": evidence_path.name,
        }
    )
    completed = subprocess.run(
        [sys.executable, "-", *paths],
        input=_single_python_heredoc(step),
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    return (
        completed,
        output_path,
        evidence_path,
        compose_hash,
        compose_prod_hash,
    )


def test_promotion_tuple_failure_keeps_bounded_redacted_initial_evidence(
    tmp_path,
):
    initialized = _run_initial_promotion_evidence(tmp_path)
    assert initialized.returncode == 0, initialized.stderr

    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Match protected Environment release tuple"
    )
    digest = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
    environment = dict(os.environ)
    environment.update(
        {
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": digest,
            "BUILD_RUN_ID": "123",
            "APPROVED_SOURCE_SHA": "c" * 40,
            "APPROVED_IMAGE_DIGEST": digest,
            "APPROVED_BUILD_RUN_ID": "123",
            "AWS_DEPLOY_ROLE_ARN": (
                "arn:aws:iam::380648615401:"
                "role/kiwoom-stock-github-production-check"
            ),
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "AWS_REGION": "ap-northeast-2",
        }
    )
    failed = subprocess.run(
        ["bash", "-c", step["run"]],
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert failed.returncode != 0
    evidence_path = tmp_path / "production-check-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["command_id"] is None
    assert evidence["compose_sha256"] is None
    assert evidence["compose_prod_sha256"] is None
    assert evidence["last_observed_status"] == "SendCommandNotAttempted"
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert evidence_path.stat().st_size <= 8192


def test_promotion_pre_job_env_uses_static_filenames_without_runner_context():
    workflow = _promotion_workflow()
    environments = (
        workflow["env"],
        workflow["jobs"]["promote"]["env"],
    )

    for environment in environments:
        assert all(
            "${{ runner." not in value
            for value in environment.values()
            if isinstance(value, str)
        )
    assert "COMPOSE_HANDOFF_FILENAME" not in workflow["env"]
    assert workflow["env"]["EVIDENCE_FILENAME"] == (
        "production-check-evidence.json"
    )
    assert all(
        "/" not in workflow["env"][name]
        for name in ("EVIDENCE_FILENAME",)
    )


def test_promotion_compose_validator_accepts_exact_bytes_and_rejects_mismatch(
    tmp_path,
):
    accepted, _, _, _, _ = _run_compose_preparation(tmp_path)
    rejected_dir = tmp_path / "rejected"
    rejected_dir.mkdir()
    rejected, _, _, _, _ = _run_compose_preparation(
        rejected_dir,
        mutation="mismatch",
    )

    assert accepted.returncode == 0, accepted.stderr
    assert rejected.returncode != 0


def test_promotion_compose_preparation_publishes_only_strict_public_outputs(
    tmp_path,
):
    completed, output_path, evidence_path, compose_hash, prod_hash = (
        _run_compose_preparation(tmp_path)
    )

    assert completed.returncode == 0, completed.stderr
    outputs = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert outputs == {
        "compose_sha256": compose_hash,
        "compose_prod_sha256": prod_hash,
        "image_size_mib": "400",
    }
    assert evidence["compose_sha256"] is None
    assert evidence["compose_prod_sha256"] is None
    assert evidence["image_size_mib"] is None
    assert evidence["command_id"] is None
    assert evidence["last_observed_status"] == "SendCommandNotAttempted"
    assert evidence_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("mutation", ["empty", "malformed", "mismatch"])
def test_promotion_compose_failure_preserves_initial_redacted_evidence(
    tmp_path,
    mutation,
):
    completed, output_path, evidence_path, _, _ = (
        _run_compose_preparation(tmp_path, mutation=mutation)
    )

    assert completed.returncode != 0
    assert not output_path.exists()
    assert evidence_path.is_file()
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["compose_sha256"] is None
    assert evidence["compose_prod_sha256"] is None
    assert evidence["command_id"] is None
    assert evidence["last_observed_status"] == "SendCommandNotAttempted"
    assert evidence["contract_expected_no_github_secrets"] is True
    assert evidence["contract_expected_worker_inactive"] is True
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert evidence_path.stat().st_size <= 8192


def test_promotion_send_failure_preserves_redacted_evidence_without_command_id(
    tmp_path,
):
    completed, output_path, evidence_path, compose_hash, prod_hash = (
        _run_compose_preparation(tmp_path)
    )
    assert completed.returncode == 0, completed.stderr
    preparation_outputs = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    evidence_path.unlink()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        (
            "#!/usr/bin/env python3\n"
            "import json, os, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "value = args[args.index('--parameters') + 1]\n"
            "payload = json.loads(pathlib.Path(value.removeprefix('file://'))"
            ".read_text())\n"
            "pathlib.Path(os.environ['CAPTURE_PARAMETERS']).write_text("
            "json.dumps(payload))\n"
            "raise SystemExit(42)\n"
        ),
        encoding="utf-8",
    )
    fake_aws.chmod(0o700)
    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Send allowlisted production-check document"
    )
    output = tmp_path / "github-output"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWS_REGION": "ap-northeast-2",
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "APPROVED_SOURCE_SHA": "a" * 40,
            "APPROVED_IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "APPROVED_BUILD_RUN_ID": "123",
            "COMPOSE_SHA256": preparation_outputs["compose_sha256"],
            "COMPOSE_PROD_SHA256": preparation_outputs[
                "compose_prod_sha256"
            ],
            "IMAGE_SIZE_MIB": preparation_outputs["image_size_mib"],
            "MAX_RUNTIME_IMAGE_MIB": "850",
            "AWS_MAX_ATTEMPTS": "3",
            "AWS_DEPLOY_ROLE_ARN": (
                "arn:aws:iam::380648615401:"
                "role/kiwoom-stock-github-production-check"
            ),
            "RUNNER_TEMP": str(tmp_path),
            "EVIDENCE_FILENAME": evidence_path.name,
            "GITHUB_OUTPUT": str(output),
            "CAPTURE_PARAMETERS": str(tmp_path / "captured-parameters.json"),
        }
    )
    sent = subprocess.run(
        ["bash", "-c", step["run"]],
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert sent.returncode == 42
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["command_id"] is None
    assert (
        evidence["last_observed_status"]
        == "SendCommandFailedBeforeCommandId"
    )
    assert evidence["send_command_exit_code"] == 42
    assert evidence["compose_sha256"] == compose_hash
    assert evidence["compose_prod_sha256"] == prod_hash
    assert evidence["image_size_mib"] == 400
    assert evidence_path.stat().st_mode & 0o777 == 0o600
    assert evidence_path.stat().st_size <= 8192
    parameters = json.loads(
        (tmp_path / "captured-parameters.json").read_text(encoding="utf-8")
    )
    assert parameters["ComposeSha256"] == [compose_hash]
    assert parameters["ComposeProdSha256"] == [prod_hash]
    assert not list(tmp_path.glob("promotion-ssm-parameters.*.json"))
    assert not output.exists()


def test_promotion_send_success_records_command_id_before_polling(tmp_path):
    completed, output_path, evidence_path, compose_hash, prod_hash = (
        _run_compose_preparation(tmp_path)
    )
    assert completed.returncode == 0, completed.stderr
    preparation_outputs = dict(
        line.split("=", 1)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    )
    evidence_path.unlink()

    command_id = "12345678-1234-1234-1234-123456789abc"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        f"#!/bin/sh\nprintf '%s\\n' '{command_id}'\n",
        encoding="utf-8",
    )
    fake_aws.chmod(0o700)
    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Send allowlisted production-check document"
    )
    output = tmp_path / "github-output"
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWS_REGION": "ap-northeast-2",
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "APPROVED_SOURCE_SHA": "a" * 40,
            "APPROVED_IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "APPROVED_BUILD_RUN_ID": "123",
            "COMPOSE_SHA256": preparation_outputs["compose_sha256"],
            "COMPOSE_PROD_SHA256": preparation_outputs[
                "compose_prod_sha256"
            ],
            "IMAGE_SIZE_MIB": preparation_outputs["image_size_mib"],
            "MAX_RUNTIME_IMAGE_MIB": "850",
            "AWS_MAX_ATTEMPTS": "3",
            "AWS_DEPLOY_ROLE_ARN": (
                "arn:aws:iam::380648615401:"
                "role/kiwoom-stock-github-production-check"
            ),
            "RUNNER_TEMP": str(tmp_path),
            "EVIDENCE_FILENAME": evidence_path.name,
            "GITHUB_OUTPUT": str(output),
        }
    )
    sent = subprocess.run(
        ["bash", "-c", step["run"]],
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert sent.returncode == 0, sent.stderr
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["command_id"] == command_id
    assert (
        evidence["last_observed_status"]
        == "CommandCreatedAwaitingInvocation"
    )
    assert evidence["compose_sha256"] == compose_hash
    assert evidence["compose_prod_sha256"] == prod_hash
    assert evidence["image_size_mib"] == 400
    assert output.read_text(encoding="utf-8") == f"command_id={command_id}\n"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMPOSE_SHA256", ""),
        ("COMPOSE_PROD_SHA256", "A" * 64),
        ("IMAGE_SIZE_MIB", "0400"),
        ("IMAGE_SIZE_MIB", "851"),
    ],
)
def test_promotion_malformed_or_missing_preparation_output_fails_before_aws(
    tmp_path,
    name,
    value,
):
    initialized = _run_initial_promotion_evidence(tmp_path)
    assert initialized.returncode == 0, initialized.stderr
    evidence_path = tmp_path / "production-check-evidence.json"
    initial = evidence_path.read_bytes()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_aws = fake_bin / "aws"
    fake_aws.write_text(
        "#!/bin/sh\ntouch \"${AWS_CALLED_MARKER}\"\nexit 0\n",
        encoding="utf-8",
    )
    fake_aws.chmod(0o700)
    step = next(
        step
        for step in _promotion_workflow()["jobs"]["promote"]["steps"]
        if step["name"] == "Send allowlisted production-check document"
    )
    environment = dict(os.environ)
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "AWS_REGION": "ap-northeast-2",
            "EC2_INSTANCE_ID": "i-02cb0a404794bd43a",
            "SOURCE_SHA": "a" * 40,
            "IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "BUILD_RUN_ID": "123",
            "APPROVED_SOURCE_SHA": "a" * 40,
            "APPROVED_IMAGE_DIGEST": (
                "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64
            ),
            "APPROVED_BUILD_RUN_ID": "123",
            "COMPOSE_SHA256": "c" * 64,
            "COMPOSE_PROD_SHA256": "d" * 64,
            "IMAGE_SIZE_MIB": "400",
            "MAX_RUNTIME_IMAGE_MIB": "850",
            "AWS_MAX_ATTEMPTS": "3",
            "AWS_DEPLOY_ROLE_ARN": (
                "arn:aws:iam::380648615401:"
                "role/kiwoom-stock-github-production-check"
            ),
            "RUNNER_TEMP": str(tmp_path),
            "EVIDENCE_FILENAME": evidence_path.name,
            "GITHUB_OUTPUT": str(tmp_path / "github-output"),
            "AWS_CALLED_MARKER": str(tmp_path / "aws-called"),
            name: value,
        }
    )

    sent = subprocess.run(
        ["bash", "-c", step["run"]],
        env=environment,
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert sent.returncode != 0
    assert not (tmp_path / "aws-called").exists()
    assert evidence_path.read_bytes() == initial


def test_promotion_has_no_candidate_build_and_gates_oidc_after_all_validation():
    workflow = _promotion_workflow()
    steps = workflow["jobs"]["promote"]["steps"]
    names = [step["name"] for step in steps]
    uses = {step.get("uses") for step in steps if step.get("uses")}
    runs_before_oidc = "\n".join(
        step.get("run", "")
        for step in steps[: names.index("Configure exact AWS deploy role with OIDC")]
    )
    all_runs = "\n".join(step.get("run", "") for step in steps)

    assert uses == {AWS_CREDENTIALS_ACTION, UPLOAD_ARTIFACT_ACTION}
    assert CHECKOUT_ACTION not in uses
    assert SETUP_PYTHON_ACTION not in uses
    assert "docker build" not in all_runs
    assert "docker push" not in all_runs
    assert "git " not in all_runs
    assert "pip " not in all_runs
    assert names.index("Initialize bounded redacted promotion evidence") == 0
    assert names.index("Match protected Environment release tuple") == 1
    assert names.index("Verify candidate run job and unique artifact provenance") < (
        names.index("Validate bounded release artifact and strict manifest")
    )
    assert names.index("Validate bounded release artifact and strict manifest") < (
        names.index("Prove anonymous exact digest image contract")
    )
    assert names.index("Prove anonymous exact digest image contract") < (
        names.index(
            "Verify Compose and publish bounded public preparation outputs"
        )
    )
    assert names.index(
        "Verify Compose and publish bounded public preparation outputs"
    ) < (
        names.index("Configure exact AWS deploy role with OIDC")
    )
    initializer = steps[0]["run"]
    assert "curl " not in initializer
    assert "docker " not in initializer
    assert "aws " not in initializer
    preparation = next(
        step["run"]
        for step in steps
        if step["name"]
        == "Verify Compose and publish bounded public preparation outputs"
    )
    assert "/contents/${compose_path}?ref=${SOURCE_SHA}" in preparation
    assert "compose_sha256={hashes['compose.yaml']}" in preparation
    assert "compose_prod_sha256={hashes['compose.prod.yaml']}" in preparation
    assert "image_size_mib={image_size_mib}" in preparation
    assert "GITHUB_OUTPUT" in preparation
    assert "write_private_json(" not in preparation
    assert "EVIDENCE_FILENAME" not in preparation
    assert "SSM_PARAMETERS_FILENAME" not in preparation
    assert all(
        "/contents/${compose_path}?ref=${SOURCE_SHA}" not in step.get("run", "")
        for step in steps
        if step["name"]
        != "Verify Compose and publish bounded public preparation outputs"
    )
    assert "aws ssm" not in runs_before_oidc


def test_promotion_oidc_boundary_carries_only_strict_public_step_outputs():
    steps = _promotion_workflow()["jobs"]["promote"]["steps"]
    names = [step["name"] for step in steps]
    preparation = next(
        step
        for step in steps
        if step["name"]
        == "Verify Compose and publish bounded public preparation outputs"
    )
    send = next(
        step
        for step in steps
        if step["name"] == "Send allowlisted production-check document"
    )
    oidc_index = names.index("Configure exact AWS deploy role with OIDC")
    send_index = names.index("Send allowlisted production-check document")

    assert send_index == oidc_index + 1
    assert preparation["id"] == "preparation"
    assert set(send["env"]) == {
        "COMPOSE_SHA256",
        "COMPOSE_PROD_SHA256",
        "IMAGE_SIZE_MIB",
    }
    assert send["env"] == {
        "COMPOSE_SHA256": (
            "${{ steps.preparation.outputs.compose_sha256 }}"
        ),
        "COMPOSE_PROD_SHA256": (
            "${{ steps.preparation.outputs.compose_prod_sha256 }}"
        ),
        "IMAGE_SIZE_MIB": (
            "${{ steps.preparation.outputs.image_size_mib }}"
        ),
    }
    assert "EVIDENCE_FILENAME" not in preparation["run"]
    assert "promotion-ssm-parameters" not in preparation["run"]
    assert "compose.yaml.contents.json" not in send["run"]
    assert "compose.prod.yaml.contents.json" not in send["run"]
    assert 'python3 - "${parameters_path}"' in send["run"]
    assert '"file://${parameters_path}"' in send["run"]
    assert send["run"].index('python3 - "${parameters_path}"') < (
        send["run"].index("aws ssm send-command")
    )
    assert "steps.image.outputs.size_mib" not in "\n".join(
        str(step)
        for step in steps[oidc_index:]
    )


def test_promotion_ssm_command_is_allowlisted_digest_only_and_check_only():
    workflow = _promotion_workflow()
    text = PROMOTION_WORKFLOW_PATH.read_text(encoding="utf-8")
    runs = "\n".join(
        step.get("run", "")
        for step in workflow["jobs"]["promote"]["steps"]
    )

    assert "secrets." not in text
    assert "AWS-RunShellScript" not in text
    assert "--document-name KiwoomStock-ProductionCheck" in text
    assert "--instance-ids \"${EC2_INSTANCE_ID}\"" in text
    assert "--timeout-seconds 750" in text
    assert '"ImageDigest": [digest]' in text
    assert '"SourceSha": [source]' in text
    assert '"ComposeSha256": [compose_hash]' in text
    assert '"ComposeProdSha256": [compose_prod_hash]' in text
    assert "COMPOSE_HANDOFF" not in text
    assert "steps.compose.outputs" not in text
    assert 're.fullmatch(r"[0-9a-f]{64}", value)' in text
    assert "SendCommandFailedBeforeCommandId" in text
    assert '"commands"' not in runs
    assert "raw.githubusercontent.com" not in runs
    assert ":latest" not in text
    assert "production-check-evidence.json" in text
    assert "if-no-files-found: error" in text
    assert '"contract_expected_no_github_secrets": True' in text
    assert '"contract_expected_worker_inactive": True' in text
    assert '"instance_verified": instance_verified' in text
    assert '"response_code_verified": response_code_verified' in text
    assert '"pass_marker_verified": pass_marker_verified' in text
    assert (
        '"operator_follow_up_required": terminal_status != "Success"'
        in text
    )
    assert "Success|Cancelled|TimedOut|Failed|Cancelling" not in text
    assert "Success|Cancelled|TimedOut|Failed)" in text
    assert "seq 1 90" in text
    assert "docker compose up" not in text
    assert " up -d" not in text
    assert "--rollback-check" not in text
