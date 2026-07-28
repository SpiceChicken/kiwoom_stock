"""Behavioral contracts for the root-owned production-check command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import textwrap

from kiwoom_stock.infrastructure.kiwoom_credentials import (
    MAX_CREDENTIAL_BYTES,
)


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "ec2" / "deploy_runtime_check.sh"
SOURCE_A = "a" * 40
SOURCE_B = "b" * 40
DIGEST_A = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + ("1" * 64)
DIGEST_B = "ghcr.io/spicechicken/kiwoom_stock@sha256:" + ("2" * 64)
COMMON_A = "3" * 64
PROD_A = "4" * 64
COMMON_B = "5" * 64
PROD_B = "6" * 64


def _source(
    command: str,
    *,
    env: dict[str, str] | None = None,
    timeout: float = 10,
) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["bash", "-c", f"source {SCRIPT!s}; {command}"],
        cwd=ROOT,
        env=merged,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _fake_docker(directory: Path) -> Path:
    executable = directory / "docker"
    executable.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            set -eu
            printf '%s\\n' "$*" >>"${FAKE_DOCKER_LOG}"
            if [[ "$1" == "compose" && "$*" == *" config --quiet"* ]]; then
                [[ "${FAKE_DOCKER_MODE}" != "config-fail" ]]
                exit
            fi
            if [[ "$1" == "pull" ]]; then
                exit 0
            fi
            if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                if [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
                    printf '%s\\n' "${FAKE_SOURCE_SHA}"
                else
                    printf '%s\\n' "sha256:fake-image-id"
                fi
                exit 0
            fi
            if [[ "$1" == "compose" && "$*" == *" run "* ]]; then
                touch "${FAKE_CONTAINER_RUNNING}"
                if [[ "${FAKE_DOCKER_MODE}" == "term-ignore" ]]; then
                    trap '' TERM
                    while :; do sleep 1; done
                fi
                rm -f "${FAKE_CONTAINER_RUNNING}"
                exit 0
            fi
            if [[ "$1" == "rm" && "$2" == "-f" ]]; then
                rm -f "${FAKE_CONTAINER_RUNNING}"
                touch "${FAKE_CONTAINER_REMOVED}"
                exit 0
            fi
            if [[ "$1" == "container" && "$2" == "inspect" ]]; then
                [[ -e "${FAKE_CONTAINER_RUNNING}" ]]
                exit
            fi
            if [[ "$1" == "container" && "$2" == "prune" ]]; then
                exit 0
            fi
            if [[ "$1" == "image" && "$2" == "ls" ]]; then
                exit 0
            fi
            if [[ "$1" == "compose" && "$2" == "version" ]]; then
                exit 0
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _fake_environment(tmp_path: Path, mode: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_docker(bin_dir)
    return {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_DOCKER_LOG": str(tmp_path / "docker.log"),
        "FAKE_DOCKER_MODE": mode,
        "FAKE_SOURCE_SHA": SOURCE_A,
        "FAKE_CONTAINER_RUNNING": str(tmp_path / "running"),
        "FAKE_CONTAINER_REMOVED": str(tmp_path / "removed"),
        "KIWOOM_DEPLOY_RUNTIME_PARENT": str(tmp_path),
        "KIWOOM_DEPLOY_CHECK_TIMEOUT_SECONDS": "1",
        "KIWOOM_DEPLOY_KILL_AFTER_SECONDS": "1",
        "KIWOOM_DEPLOY_PULL_TIMEOUT_SECONDS": "2",
    }


def test_deploy_script_has_valid_shell_syntax_and_exact_cli_boundaries():
    completed = subprocess.run(
        ["bash", "-n", str(SCRIPT)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")

    assert completed.returncode == 0, completed.stderr
    assert "--rollback-check" in text
    assert "rollback-check does not accept release arguments" in text
    assert "--compose-sha256" in text
    assert "--compose-prod-sha256" in text
    assert "--repository" not in text


def test_release_validators_accept_only_exact_tuple_values():
    valid = _source(
        f"validate_release_values {DIGEST_A!r} {SOURCE_A} {COMMON_A} {PROD_A}; "
        "validate_instance_id i-02cb0a404794bd43a; "
        "validate_region ap-northeast-2; echo accepted"
    )
    mutable = _source(
        "validate_image ghcr.io/spicechicken/kiwoom_stock:latest"
    )
    short_sha = _source(f"validate_source_sha {'a' * 39}")

    assert valid.returncode == 0
    assert valid.stdout == "accepted\n"
    assert mutable.returncode == 1
    assert short_sha.returncode == 1


def test_release_state_transition_is_one_atomic_full_tuple(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"KIWOOM_DEPLOY_STATE_DIR": str(state_dir)}
    first = _source(
        f"record_success {DIGEST_A!r} {SOURCE_A} {COMMON_A} {PROD_A}",
        env=env,
    )
    second = _source(
        f"record_success {DIGEST_B!r} {SOURCE_B} {COMMON_B} {PROD_B}",
        env=env,
    )
    state = json.loads(
        (state_dir / "release-state.json").read_text(encoding="utf-8")
    )

    assert first.returncode == 0
    assert second.returncode == 0
    assert set(state) == {"schema", "current", "previous"}
    assert state["schema"] == 1
    assert state["current"] == {
        "source_sha": SOURCE_B,
        "image_digest": DIGEST_B,
        "image_revision": SOURCE_B,
        "compose_sha256": {
            "compose.yaml": COMMON_B,
            "compose.prod.yaml": PROD_B,
        },
    }
    assert state["previous"] == {
        "source_sha": SOURCE_A,
        "image_digest": DIGEST_A,
        "image_revision": SOURCE_A,
        "compose_sha256": {
            "compose.yaml": COMMON_A,
            "compose.prod.yaml": PROD_A,
        },
    }
    assert not list(state_dir.glob("*.tmp.*"))
    assert (state_dir / "release-state.json").stat().st_mode & 0o777 == 0o600


def test_invalid_existing_state_fails_without_partial_replacement(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_path = state_dir / "release-state.json"
    original = b'{"schema":999,"current":null,"previous":null}\n'
    state_path.write_bytes(original)
    completed = _source(
        f"record_success {DIGEST_A!r} {SOURCE_A} {COMMON_A} {PROD_A}",
        env={"KIWOOM_DEPLOY_STATE_DIR": str(state_dir)},
    )

    assert completed.returncode != 0
    assert state_path.read_bytes() == original
    assert not list(state_dir.glob("*.tmp.*"))


def test_rollback_loads_only_the_recorded_previous_full_tuple(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {"KIWOOM_DEPLOY_STATE_DIR": str(state_dir)}
    _source(
        f"record_success {DIGEST_A!r} {SOURCE_A} {COMMON_A} {PROD_A}; "
        f"record_success {DIGEST_B!r} {SOURCE_B} {COMMON_B} {PROD_B}",
        env=env,
    )
    loaded = _source("load_previous_release", env=env)

    assert loaded.returncode == 0
    assert loaded.stdout.splitlines() == [
        DIGEST_A,
        SOURCE_A,
        COMMON_A,
        PROD_A,
    ]


def test_lock_contention_fails_closed_with_fake_flock(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    flock = bin_dir / "flock"
    flock.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    flock.chmod(0o755)
    completed = _source(
        "acquire_deployment_lock",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "KIWOOM_DEPLOY_LOCK_FILE": str(tmp_path / "deploy.lock"),
        },
    )

    assert completed.returncode == 1
    assert "another production check holds" in completed.stderr


def test_compose_download_hash_mismatch_never_replaces_contract(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            output=""
            while [ "$#" -gt 0 ]; do
                if [ "$1" = "--output" ]; then
                    output="$2"
                    shift 2
                else
                    shift
                fi
            done
            printf '%s\\n' 'unexpected-contract' >"${output}"
            """
        ),
        encoding="utf-8",
    )
    curl.chmod(0o755)
    destination = tmp_path / "source"
    completed = _source(
        f"download_compose_contract {SOURCE_A} {COMMON_A} {PROD_A} "
        f"{str(destination)!r}",
        env={
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "KIWOOM_DEPLOY_DOWNLOAD_TIMEOUT_SECONDS": "2",
        },
    )

    assert completed.returncode == 1
    assert not (destination / "compose.yaml").exists()
    assert not (destination / "compose.prod.yaml").exists()


def test_term_ignoring_check_is_force_removed_and_placeholders_are_deleted(
    tmp_path,
):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    env = _fake_environment(tmp_path, "term-ignore")
    completed = _source(
        f"compose_check {DIGEST_A!r} {SOURCE_A} {str(compose_dir)!r}",
        env=env,
        timeout=8,
    )
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    expected_name = "kiwoom-check-aaaaaaaaaaaa-111111111111"

    assert completed.returncode == 1
    assert (tmp_path / "removed").is_file()
    assert not (tmp_path / "running").exists()
    assert f"--name {expected_name}" in log
    assert f"rm -f {expected_name}" in log
    assert not list(tmp_path.glob("kiwoom-check-secrets.*"))
    assert not list(tmp_path.glob("kiwoom-check-data.*"))


def test_config_failure_also_cleans_exact_boundaries(tmp_path):
    compose_dir = tmp_path / "compose"
    compose_dir.mkdir()
    env = _fake_environment(tmp_path, "config-fail")
    completed = _source(
        f"compose_check {DIGEST_A!r} {SOURCE_A} {str(compose_dir)!r}",
        env=env,
    )

    assert completed.returncode == 1
    assert not list(tmp_path.glob("kiwoom-check-secrets.*"))
    assert not list(tmp_path.glob("kiwoom-check-data.*"))


def test_secret_metadata_contract_matches_8kib_ssot_and_rejects_symlink(
    tmp_path,
):
    text = SCRIPT.read_text(encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    completed = _source(f"reject_symlink_components {str(link)!r}")

    assert f"MAX_CREDENTIAL_BYTES={MAX_CREDENTIAL_BYTES}" in text
    assert completed.returncode == 1
    assert "must not contain symbolic links" in completed.stderr
    assert "stat -c '%s'" in text


def test_script_never_mounts_actual_secret_files_or_prunes_volumes():
    text = SCRIPT.read_text(encoding="utf-8")

    assert 'KIWOOM_PROD_APP_KEY_FILE="${PLACEHOLDER_DIR}/app-key"' in text
    assert (
        'KIWOOM_PROD_SECRET_KEY_FILE="${PLACEHOLDER_DIR}/secret-key"'
        in text
    )
    assert "CHECK_ONLY_NON_SECRET_APP_KEY" in text
    assert "CHECK_ONLY_NON_SECRET_SECRET_KEY" in text
    assert "docker volume" not in text
    assert "docker compose up" not in text
    assert "rm -rf" not in text
    assert "trap 'cleanup_runtime' EXIT" in text
    assert "trap 'cleanup_runtime' ERR" in text
    assert "trap 'cleanup_runtime; exit 143' TERM" in text
