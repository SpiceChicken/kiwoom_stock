"""Static contract checks for Docker and Compose files."""

import ast
import importlib.util
import os
from pathlib import Path

import yaml
import pytest

from kiwoom_stock.settings import SETTING_SPEC_BY_NAME


DOCKERFILE = Path("Dockerfile")
DOCKERIGNORE = Path(".dockerignore")
COMPOSE = Path("compose.yaml")
COMPOSE_DEV = Path("compose.dev.yaml")
COMPOSE_MOCK = Path("compose.mock.yaml")
COMPOSE_PROD = Path("compose.prod.yaml")
CONTAINER_TEST_STAGE_ENV = "CONTAINER_TEST_STAGE"


def _load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _stage_block(text: str, stage: str) -> str:
    marker = f"FROM base AS {stage}"
    _, block = text.split(marker, maxsplit=1)
    return block.split("\nFROM ", maxsplit=1)[0]


def _container_contract_mode() -> str:
    """Resolve host or self-describing test-image contract mode."""
    marker = os.environ.get(CONTAINER_TEST_STAGE_ENV)
    assert DOCKERFILE.is_file()
    assert DOCKERIGNORE.is_file()
    if marker is None:
        return "host"

    assert marker == "1"
    return "test-image"


def test_dockerfile_uses_multistage_non_root_runtime_without_secret_baking():
    _container_contract_mode()
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert "FROM python:${PYTHON_VERSION}-slim AS base" in text
    assert "FROM base AS builder" in text
    assert "FROM base AS test" in text
    assert "FROM base AS runtime" in text
    assert "USER 10001:10001" in text
    assert "HEALTHCHECK" in text
    assert (
        'CMD ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py", '
        '"--healthcheck"]' in text
    )
    assert "KIWOOM_APP_KEY=" not in text
    assert "KIWOOM_SECRET_KEY=" not in text


def test_docker_test_stage_has_complete_minimal_full_suite_manifest():
    _container_contract_mode()
    text = DOCKERFILE.read_text(encoding="utf-8")
    test_block = _stage_block(text, "test")

    copy_lines = [line for line in test_block.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY pyproject.toml README.MD ./",
        "COPY requirements/locks/dev-${PYTHON_LOCK}.txt /tmp/dev-lock.txt",
        "COPY requirements/locks ./requirements/locks",
        "COPY src ./src",
        "COPY tests ./tests",
        "COPY main.py ./",
        "COPY tools ./tools",
        "COPY docker ./docker",
        "COPY deploy ./deploy",
        "COPY prompt ./prompt",
        "COPY .env.example ./",
        "COPY Dockerfile .dockerignore .gitleaks.toml ./",
        "COPY docs/configuration.md ./docs/configuration.md",
        "COPY docs/operations ./docs/operations",
        "COPY compose.yaml compose.dev.yaml compose.mock.yaml compose.prod.yaml compose.shadow.yaml ./",
        (
            "COPY .github/workflows/ci.yml "
            ".github/workflows/cd-production-check.yml "
                ".github/workflows/cd-production-promotion.yml "
                ".github/workflows/cd-shadow-schedule-audit.yml "
                ".github/workflows/cd-shadow-worker-activation.yml "
                ".github/workflows/cd-shadow-worker-rollout.yml "
                ".github/workflows/cd-shadow-rollout-document-migration.yml "
                "./.github/workflows/"
        ),
    ]
    assert test_block.count("ENV CONTAINER_TEST_STAGE=1") == 1
    assert text.count("ENV CONTAINER_TEST_STAGE=1") == 1
    assert test_block.count("RUN mkdir /app/.git") == 1
    assert "python -m pip install --require-hashes -r /tmp/dev-lock.txt" in test_block
    assert "python -m pip install --no-deps --no-build-isolation -e ." in test_block
    assert (
        'CMD ["python", "-m", "pytest", "tests", "-q", '
        '"--basetemp=/tmp/pytest"]' in test_block
    )
    assert "COPY . ." not in test_block
    assert "compose*.yaml" not in test_block
    assert "apt-get" not in test_block
    assert "docker-ce" not in test_block


def test_docker_runtime_stage_only_copies_the_builder_wheel():
    _container_contract_mode()
    text = DOCKERFILE.read_text(encoding="utf-8")
    runtime_block = _stage_block(text, "runtime")

    copy_lines = [line for line in runtime_block.splitlines() if line.startswith("COPY ")]
    assert copy_lines == [
        "COPY --from=builder /app/dist/*.whl /tmp/",
        "COPY requirements/locks/runtime-${PYTHON_LOCK}.txt /tmp/runtime-lock.txt",
        "COPY docker/runtime_entrypoint.py /usr/local/bin/kiwoom-runtime-entrypoint.py",
    ]
    assert CONTAINER_TEST_STAGE_ENV not in runtime_block


def test_dockerignore_only_reincludes_tested_workflows_from_github():
    _container_contract_mode()
    active_lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    github_lines = [line for line in active_lines if ".github" in line]
    negation_lines = [line for line in active_lines if line.startswith("!")]

    assert github_lines == [
        ".github/**",
        "!.github/workflows/",
        "!.github/workflows/ci.yml",
        "!.github/workflows/cd-production-check.yml",
        "!.github/workflows/cd-production-promotion.yml",
        "!.github/workflows/cd-shadow-schedule-audit.yml",
        "!.github/workflows/cd-shadow-worker-activation.yml",
        "!.github/workflows/cd-shadow-rollout-document-migration.yml",
    ]
    assert negation_lines == [
        "!.github/workflows/",
        "!.github/workflows/ci.yml",
        "!.github/workflows/cd-production-check.yml",
        "!.github/workflows/cd-production-promotion.yml",
        "!.github/workflows/cd-shadow-schedule-audit.yml",
        "!.github/workflows/cd-shadow-worker-activation.yml",
        "!.github/workflows/cd-shadow-rollout-document-migration.yml",
        "!.env.example",
        "!.env.*.example",
    ]
    assert ".github" not in active_lines
    assert "!.github/**" not in active_lines
    assert "!.github/workflows/**" not in active_lines
    assert "!.github/workflows/*" not in active_lines
    assert ".git" in active_lines


def test_dockerignore_denies_credentials_everywhere_without_secret_reinclude():
    _container_contract_mode()
    active_lines = [
        line.strip()
        for line in DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    credential_deny_patterns = [
        "**/.credential/",
        "**/.credentials/",
        "**/credential/",
        "**/credentials/",
        "**/.secret/",
        "**/.secrets/",
        "**/secret/",
        "**/secrets/",
        "**/app-key",
        "**/secret-key",
        "**/KIWOOM_APP_KEY",
        "**/KIWOOM_SECRET_KEY",
    ]
    first_pattern = active_lines.index(credential_deny_patterns[0])
    last_pattern = first_pattern + len(credential_deny_patterns)

    assert (
        active_lines[first_pattern:last_pattern] == credential_deny_patterns
    )
    assert len(active_lines) == len(set(active_lines))

    sensitive_names = (
        "credential",
        "secret",
        "app-key",
        "kiwoom_app_key",
    )
    sensitive_reincludes = [
        line
        for line in active_lines
        if line.startswith("!")
        and any(name in line.lower() for name in sensitive_names)
    ]
    assert sensitive_reincludes == []
    assert "!.env.example" in active_lines
    assert "!.env.*.example" in active_lines


def test_test_image_manifest_covers_repository_assets_and_host_only_compose():
    _container_contract_mode()
    required_paths = [
        Path("README.MD"),
        Path("tests"),
        Path("main.py"),
        Path("tools"),
        Path("docker"),
        Path("deploy"),
        Path("prompt"),
        Path(".env.example"),
        Path(".gitleaks.toml"),
        Path("docs/configuration.md"),
        Path("docs/operations/github-oidc-aws-bootstrap.md"),
        COMPOSE,
        COMPOSE_DEV,
        COMPOSE_MOCK,
        COMPOSE_PROD,
        Path("compose.shadow.yaml"),
        Path(".github/workflows/ci.yml"),
        Path(".github/workflows/cd-production-check.yml"),
        Path(".github/workflows/cd-production-promotion.yml"),
        Path(".github/workflows/cd-shadow-schedule-audit.yml"),
        Path(".github/workflows/cd-shadow-worker-activation.yml"),
        Path(".github/workflows/cd-shadow-rollout-document-migration.yml"),
    ]
    assert all(path.exists() for path in required_paths)

    compose_test = Path("tests/test_compose_preflight.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(compose_test)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    expected_function = (
        "test_effective_prod_compose_has_no_network_or_production_named_volume"
    )

    def call_path(node: ast.AST) -> tuple[str, ...]:
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        return tuple(reversed(parts))

    skip_calls = []
    docker_which_calls = []
    for function_name, function in functions.items():
        for node in ast.walk(function):
            if not isinstance(node, ast.Call):
                continue
            path = call_path(node.func)
            if path in {
                ("pytest", "skip"),
                ("pytest", "mark", "skip"),
                ("pytest", "mark", "skipif"),
            }:
                skip_calls.append((function_name, path, node))
            if (
                path == ("shutil", "which")
                and len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "docker"
            ):
                docker_which_calls.append((function_name, node))

    assert len(skip_calls) == 1
    skip_function, skip_path, skip_call = skip_calls[0]
    assert skip_function == expected_function
    assert skip_path == ("pytest", "skip")
    assert len(skip_call.args) == 1
    assert isinstance(skip_call.args[0], ast.Constant)
    assert skip_call.args[0].value == "Docker Compose is unavailable"

    assert len(docker_which_calls) == 1
    assert docker_which_calls[0][0] == expected_function

    guarded_skips = [
        node
        for node in ast.walk(functions[expected_function])
        if isinstance(node, ast.If)
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Expr)
        and node.body[0].value is skip_call
    ]
    assert len(guarded_skips) == 1


def test_common_compose_service_is_hardened_and_side_effect_safe():
    compose = _load_yaml(COMPOSE)
    app = compose["services"]["app"]

    assert app["init"] is True
    assert app["user"] == "10001:10001"
    assert app["read_only"] is True
    assert app["cap_drop"] == ["ALL"]
    assert app["security_opt"] == ["no-new-privileges:true"]
    assert app["command"] == ["python", "-m", "kiwoom_stock", "--check-config"]
    assert app["healthcheck"]["test"] == [
        "CMD",
        "python",
        "/usr/local/bin/kiwoom-runtime-entrypoint.py",
        "--healthcheck",
    ]
    assert app["stop_grace_period"] == "30s"
    assert app["tmpfs"] == ["/tmp"]
    assert app["environment"]["KIWOOM_API_MODE"] == "disabled"
    assert "KIWOOM_CREDENTIALS_DIR" not in app["environment"]
    assert "secrets" not in app


def test_common_compose_pins_one_named_volume_sqlite_owner():
    compose = _load_yaml(COMPOSE)
    app = compose["services"]["app"]

    assert set(compose["services"]) == {"app"}
    assert app["environment"]["KIWOOM_DB_PATH"] == "/var/lib/kiwoom/trades.db"
    assert app["volumes"] == ["kiwoom-data:/var/lib/kiwoom"]
    assert compose["volumes"] == {"kiwoom-data": None}
    assert app["read_only"] is True
    assert app["tmpfs"] == ["/tmp"]


def test_raw_common_and_prod_compose_do_not_request_replica_expansion():
    common = _load_yaml(COMPOSE)
    prod = _load_yaml(COMPOSE_PROD)

    for document in (common, prod):
        assert set(document["services"]) == {"app"}
        app = document["services"]["app"]
        assert "scale" not in app
        assert "replicas" not in app.get("deploy", {})

    assert "KIWOOM_DB_PATH" not in prod["services"]["app"]["environment"]
    assert prod["services"]["app"]["network_mode"] == "none"
    assert prod["services"]["app"]["volumes"][0]["target"] == "/var/lib/kiwoom"
    assert prod["services"]["app"]["volumes"][0]["read_only"] is True


def test_runtime_secret_entrypoint_is_the_only_enabled_mode_copy_boundary():
    entrypoint = Path("docker/runtime_entrypoint.py").read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert 'source_dir != Path("/run/secrets")' in entrypoint
    assert "os.chown(destination, RUNTIME_UID, RUNTIME_GID)" in entrypoint
    assert "os.setgroups([])" in entrypoint
    assert "os.setgid(RUNTIME_GID)" in entrypoint
    assert "os.setuid(RUNTIME_UID)" in entrypoint
    assert "--healthcheck" in entrypoint
    assert 'ENTRYPOINT ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py"]' in dockerfile

    for path in (COMPOSE_PROD, COMPOSE_MOCK):
        app = _load_yaml(path)["services"]["app"]
        assert app["user"] == "0:0"
        assert "/run/secrets:mode=0700" in app["tmpfs"]
        assert "/run/kiwoom-secrets:mode=0700" in app["tmpfs"]
        assert app["cap_add"] == ["CHOWN", "SETGID", "SETUID"]


def test_runtime_entrypoint_enforces_image_tuple_for_both_shadow_modes(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "kiwoom_runtime_entrypoint",
        "docker/runtime_entrypoint.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "a" * 64

    for mode, process in (
        ("shadow-once", "kiwoom-shadow-once"),
        ("shadow-continuous", "kiwoom-shadow-worker"),
    ):
        monkeypatch.setenv("KIWOOM_EXECUTION_MODE", mode)
        monkeypatch.setenv("KIWOOM_PROCESS_NAME", process)
        monkeypatch.setenv("KIWOOM_IMAGE_REF", digest)
        monkeypatch.setenv("KIWOOM_IMAGE_DIGEST", digest)
        module._validate_shadow_image_tuple()

        monkeypatch.setenv(
            "KIWOOM_IMAGE_DIGEST",
            "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "b" * 64,
        )
        with pytest.raises(RuntimeError, match="must match"):
            module._validate_shadow_image_tuple()


def test_runtime_entrypoint_rejects_crossed_shadow_mode_process_pair(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "kiwoom_runtime_entrypoint_cross_pair",
        "docker/runtime_entrypoint.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    digest = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + "a" * 64
    monkeypatch.setenv("KIWOOM_EXECUTION_MODE", "shadow-continuous")
    monkeypatch.setenv("KIWOOM_PROCESS_NAME", "kiwoom-shadow-once")
    monkeypatch.setenv("KIWOOM_IMAGE_REF", digest)
    monkeypatch.setenv("KIWOOM_IMAGE_DIGEST", digest)

    with pytest.raises(RuntimeError, match="mode and process name"):
        module._validate_shadow_image_tuple()


def test_documented_database_consumer_matches_typed_registry():
    documentation = Path("docs/configuration.md").read_text(encoding="utf-8")
    row = next(
        line
        for line in documentation.splitlines()
        if line.startswith("| `KIWOOM_DB_PATH` |")
    )
    cells = [cell.strip() for cell in row.strip("|").split("|")]

    assert cells[4] == SETTING_SPEC_BY_NAME["KIWOOM_DB_PATH"].consumer


def test_dev_compose_is_disabled_without_credentials_or_runtime_network():
    dev = _load_yaml(COMPOSE_DEV)
    app = dev["services"]["app"]

    assert app["build"]["target"] == "test"
    assert app["build"]["args"]["PYTHON_VERSION"] == "3.14"
    assert app["build"]["args"]["PYTHON_LOCK"] == "py314"
    assert app["network_mode"] == "none"
    assert app["read_only"] is False
    assert app["healthcheck"] == {"disable": True}
    assert app["environment"]["KIWOOM_API_MODE"] == "disabled"
    assert "KIWOOM_APP_KEY" not in app["environment"]
    assert "KIWOOM_SECRET_KEY" not in app["environment"]
    assert "KIWOOM_BASE_URL" not in app["environment"]
    assert "secrets" not in app
    assert ".:/app" in app["volumes"]
    assert app["command"] == ["python", "-m", "pytest", "tests", "-q"]


def test_prod_compose_uses_secrets_without_source_bind_or_build_context():
    prod = _load_yaml(COMPOSE_PROD)
    app = prod["services"]["app"]

    assert "build" not in app
    assert app["network_mode"] == "none"
    assert app["privileged"] is False
    assert app["cpus"] == 0.75
    assert app["mem_limit"] == "512m"
    assert app["pids_limit"] == 128
    assert app["volumes"] == [
        {
            "type": "bind",
            "source": (
                "${KIWOOM_CHECK_DATA_DIR:?"
                "set an absolute ephemeral check data directory}"
            ),
            "target": "/var/lib/kiwoom",
            "read_only": True,
        }
    ]
    assert "kiwoom-data" not in COMPOSE_PROD.read_text(encoding="utf-8")
    assert app["environment"]["KIWOOM_API_MODE"] == "prod"
    assert app["environment"]["KIWOOM_CREDENTIALS_DIR"] == "/run/secrets"
    assert app["secrets"] == [
        {"source": "kiwoom_prod_app_key", "target": "KIWOOM_APP_KEY"},
        {"source": "kiwoom_prod_secret_key", "target": "KIWOOM_SECRET_KEY"},
    ]
    assert app["environment"]["KIWOOM_APP_ENV"] == "prod"
    assert "./.secrets" not in COMPOSE_PROD.read_text(encoding="utf-8")
    assert prod["secrets"]["kiwoom_prod_app_key"]["file"].startswith(
        "${KIWOOM_PROD_APP_KEY_FILE:?"
    )
    assert prod["secrets"]["kiwoom_prod_secret_key"]["file"].startswith(
        "${KIWOOM_PROD_SECRET_KEY_FILE:?"
    )


def test_mock_and_prod_use_distinct_external_file_contracts_without_remap_claims():
    mock = _load_yaml(COMPOSE_MOCK)
    prod = _load_yaml(COMPOSE_PROD)
    mock_app = mock["services"]["app"]

    assert mock_app["environment"]["KIWOOM_API_MODE"] == "mock"
    assert mock_app["environment"]["KIWOOM_CREDENTIALS_DIR"] == "/run/secrets"
    assert mock_app["secrets"] == [
        {"source": "kiwoom_mock_app_key", "target": "KIWOOM_APP_KEY"},
        {"source": "kiwoom_mock_secret_key", "target": "KIWOOM_SECRET_KEY"},
    ]
    assert set(mock["secrets"]).isdisjoint(prod["secrets"])
    assert "KIWOOM_MOCK_APP_KEY_FILE" in COMPOSE_MOCK.read_text(encoding="utf-8")
    assert "KIWOOM_PROD_APP_KEY_FILE" in COMPOSE_PROD.read_text(encoding="utf-8")
    for document in (mock, prod):
        for secret in document["secrets"].values():
            assert set(secret) == {"file"}
