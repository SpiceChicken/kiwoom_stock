"""Behavioral contracts for the root-owned production-check command."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
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
ATTEMPT_ID = "987654321"


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
            if [[ "$1" == "pull" ]]; then
                exit 0
            fi
            if [[ "$1" == "image" && "$2" == "inspect" ]]; then
                if [[ "$*" == *"org.opencontainers.image.revision"* ]]; then
                    printf '%s\\n' "${FAKE_SOURCE_SHA}"
                elif [[ "$*" == *"Config.Entrypoint"* ]]; then
                    if [[ "${FAKE_DOCKER_MODE}" == "bad-entrypoint" ]]; then
                        printf '%s\\n' '["/bin/sh","-c"]'
                    else
                        printf '%s\\n' \
                            '["python","/usr/local/bin/kiwoom-runtime-entrypoint.py"]'
                    fi
                elif [[ "$*" == *".Config.User"* ]]; then
                    printf '%s\\n' "10001:10001"
                else
                    printf '%s\\n' "sha256:fake-image-id"
                fi
                exit 0
            fi
            if [[ "$1" == "run" ]]; then
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
    chown = bin_dir / "chown"
    chown.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    chown.chmod(0o755)
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


def test_promotion_attempt_success_marker_is_private_atomic_and_reconcilable(
    tmp_path,
):
    state_dir = tmp_path / "state"
    attempt_dir = state_dir / "promotion-attempts"
    attempt_dir.mkdir(parents=True)
    env = {"KIWOOM_DEPLOY_STATE_DIR": str(state_dir)}
    recorded = _source(
        f"record_promotion_attempt_success {ATTEMPT_ID} {DIGEST_A!r} "
        f"{SOURCE_A} {COMMON_A} {PROD_A}",
        env=env,
    )
    reconciled = _source(
        f"reconcile_promotion_attempt {ATTEMPT_ID} {DIGEST_A!r} "
        f"{SOURCE_A} {COMMON_A} {PROD_A}",
        env=env,
    )
    marker = attempt_dir / f"{ATTEMPT_ID}.json"

    assert recorded.returncode == 0, recorded.stderr
    assert reconciled.returncode == 0, reconciled.stderr
    assert reconciled.stdout == (
        "production check passed: "
        f"source_sha={SOURCE_A} image={DIGEST_A} rollback=false\n"
    )
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not list(attempt_dir.glob(".*.tmp.*"))
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["promotion_attempt_id"] == ATTEMPT_ID
    assert payload["source_sha"] == SOURCE_A
    assert payload["image_digest"] == DIGEST_A


def test_existing_attempt_tuple_mismatch_fails_before_any_runtime_call(tmp_path):
    state_dir = tmp_path / "state"
    attempt_dir = state_dir / "promotion-attempts"
    attempt_dir.mkdir(parents=True)
    docker_log = tmp_path / "docker.log"
    env = {
        "KIWOOM_DEPLOY_STATE_DIR": str(state_dir),
        "FAKE_DOCKER_LOG": str(docker_log),
    }
    first = _source(
        f"record_promotion_attempt_success {ATTEMPT_ID} {DIGEST_A!r} "
        f"{SOURCE_A} {COMMON_A} {PROD_A}",
        env=env,
    )
    mismatch = _source(
        f"reconcile_promotion_attempt {ATTEMPT_ID} {DIGEST_B!r} "
        f"{SOURCE_B} {COMMON_B} {PROD_B}",
        env=env,
    )

    assert first.returncode == 0
    assert mismatch.returncode != 0
    assert "marker tuple mismatch" in mismatch.stderr
    assert not docker_log.exists()
    main_block = SCRIPT.read_text(encoding="utf-8").split(
        "main() {", maxsplit=1
    )[1]
    reconciliation = main_block.index(
        'if [[ -e "${ATTEMPT_DIR}/${promotion_attempt_id}.json" ]]'
    )
    assert '|| fail "promotion attempt reconciliation failed"' in main_block
    assert reconciliation < main_block.index("command -v docker")
    assert reconciliation < main_block.index("validate_instance_identity")


def test_failed_attempt_has_no_marker_to_convert_retry_to_success(tmp_path):
    state_dir = tmp_path / "state"
    attempt_dir = state_dir / "promotion-attempts"
    attempt_dir.mkdir(parents=True)
    absent = _source(
        f"reconcile_promotion_attempt {ATTEMPT_ID} {DIGEST_A!r} "
        f"{SOURCE_A} {COMMON_A} {PROD_A}",
        env={"KIWOOM_DEPLOY_STATE_DIR": str(state_dir)},
    )

    assert absent.returncode == 1
    assert not (attempt_dir / f"{ATTEMPT_ID}.json").exists()


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
    env = _fake_environment(tmp_path, "term-ignore")
    completed = _source(
        f"run_container_check {DIGEST_A!r} {SOURCE_A}",
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


def test_invalid_image_entrypoint_never_reaches_run_and_cleans_boundaries(
    tmp_path,
):
    env = _fake_environment(tmp_path, "bad-entrypoint")
    completed = _source(
        f"run_container_check {DIGEST_A!r} {SOURCE_A}",
        env=env,
    )
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")

    assert completed.returncode == 1
    assert not any(line.startswith("run ") for line in log.splitlines())
    assert not list(tmp_path.glob("kiwoom-check-secrets.*"))
    assert not list(tmp_path.glob("kiwoom-check-data.*"))


def test_root_owned_docker_run_has_exact_ordered_execution_boundary(tmp_path):
    env = _fake_environment(tmp_path, "success")
    completed = _source(
        f"run_container_check {DIGEST_A!r} {SOURCE_A}",
        env=env,
    )
    log = (tmp_path / "docker.log").read_text(encoding="utf-8")
    run_line = next(line for line in log.splitlines() if line.startswith("run "))
    tokens = shlex.split(run_line)
    mount_indexes = [
        index for index, value in enumerate(tokens) if value == "--mount"
    ]

    assert completed.returncode == 0, completed.stderr
    assert len(mount_indexes) == 3
    normalized = list(tokens)
    expected_mount_suffixes = [
        ",target=/var/lib/kiwoom,readonly",
        ",target=/run/secrets/KIWOOM_APP_KEY,readonly",
        ",target=/run/secrets/KIWOOM_SECRET_KEY,readonly",
    ]
    expected_sources = [
        r"/kiwoom-check-data\.[^,]+",
        r"/kiwoom-check-secrets\.[^,]+/app-key",
        r"/kiwoom-check-secrets\.[^,]+/secret-key",
    ]
    replacements = ["<CHECK_DATA>", "<APP_KEY>", "<SECRET_KEY>"]
    for index, suffix, source_pattern, replacement in zip(
        mount_indexes,
        expected_mount_suffixes,
        expected_sources,
        replacements,
        strict=True,
    ):
        mount = tokens[index + 1]
        assert mount.startswith("type=bind,source=")
        assert mount.endswith(suffix)
        source = mount.removeprefix("type=bind,source=").removesuffix(suffix)
        assert re.search(source_pattern + r"$", source)
        normalized[index + 1] = (
            f"type=bind,source={replacement}{suffix}"
        )

    expected_name = "kiwoom-check-aaaaaaaaaaaa-111111111111"
    assert normalized == [
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        expected_name,
        "--label",
        "io.kiwoom-stock.project=kiwoom-stock",
        "--label",
        "io.kiwoom-stock.lifecycle=production-check-only",
        "--init",
        "--no-healthcheck",
        "--user",
        "0:0",
        "--network",
        "none",
        "--read-only",
        "--cpus",
        "0.75",
        "--memory",
        "536870912",
        "--memory-swap",
        "536870912",
        "--pids-limit",
        "128",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CHOWN",
        "--cap-add",
        "SETGID",
        "--cap-add",
        "SETUID",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,noexec,mode=1777",
        "--tmpfs",
        "/run/secrets:rw,nosuid,nodev,noexec,mode=0700",
        "--tmpfs",
        "/run/kiwoom-secrets:rw,nosuid,nodev,noexec,mode=0700",
        "--mount",
        "type=bind,source=<CHECK_DATA>,target=/var/lib/kiwoom,readonly",
        "--mount",
        (
            "type=bind,source=<APP_KEY>,"
            "target=/run/secrets/KIWOOM_APP_KEY,readonly"
        ),
        "--mount",
        (
            "type=bind,source=<SECRET_KEY>,"
            "target=/run/secrets/KIWOOM_SECRET_KEY,readonly"
        ),
        "--env",
        "KIWOOM_API_MODE=prod",
        "--env",
        "KIWOOM_APP_ENV=prod",
        "--env",
        "KIWOOM_PROCESS_NAME=paper-monitor",
        "--env",
        "KIWOOM_CREDENTIALS_DIR=/run/secrets",
        "--env",
        "KIWOOM_OUTPUT_DIR=/var/lib/kiwoom/output",
        "--env",
        "KIWOOM_DB_PATH=/var/lib/kiwoom/trades.db",
        DIGEST_A,
        "python",
        "-m",
        "kiwoom_stock",
        "--check-config",
    ]
    assert "--privileged" not in tokens
    assert "--device" not in tokens
    assert "--pid" not in tokens
    assert "--ipc" not in tokens
    assert "/var/run/docker.sock" not in run_line


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
    run_block = text.split("run_container_check() {", maxsplit=1)[1].split(
        "\nvalidate_release_values() {",
        maxsplit=1,
    )[0]

    assert "APP_KEY_FILE" not in run_block
    assert "SECRET_KEY_FILE" not in run_block
    assert "docker compose" not in text
    assert "compose.yaml" not in run_block
    assert "compose.prod.yaml" not in run_block
    assert "CHECK_ONLY_NON_SECRET_APP_KEY" in text
    assert "CHECK_ONLY_NON_SECRET_SECRET_KEY" in text
    assert "docker volume" not in text
    assert "rm -rf" not in text
    assert "trap 'cleanup_runtime' EXIT" in text
    assert "trap 'cleanup_runtime' ERR" in text
    assert "trap 'cleanup_runtime; exit 143' TERM" in text
