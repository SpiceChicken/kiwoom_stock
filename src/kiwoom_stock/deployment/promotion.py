"""Fail-closed production-check promotion boundary.

The ``execute`` command intentionally performs every authoritative check in one
post-OIDC process.  Its only durable state is bounded, redacted audit evidence.
"""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import BinaryIO, cast, Mapping, Protocol, Sequence
import urllib.parse
import zipfile


REPOSITORY = "SpiceChicken/kiwoom_stock"
IMAGE_PREFIX = "ghcr.io/spicechicken/kiwoom_stock@sha256:"
ROLE_ARN = (
    "arn:aws:iam::380648615401:"
    "role/kiwoom-stock-github-production-check"
)
REGION = "ap-northeast-2"
INSTANCE_ID = "i-0e42e09d6c087ba29"
DOCUMENT_NAME = "KiwoomStock-ProductionCheck"
MAX_IMAGE_MIB = 850
AWS_MAX_ATTEMPTS = "3"
AWS_SEND_MAX_ATTEMPTS = "1"
POLL_LIMIT = 90
POLL_SECONDS = 10.0
EXECUTION_BUDGET_SECONDS = 960.0
COMMAND_OUTPUT_LIMIT = 2 * 1024 * 1024
HTTP_REDIRECT_LIMIT = 5
HTTP_CONNECT_TIMEOUT_SECONDS = 10.0
HTTP_USER_AGENT = "kiwoom-stock-promotion/1"
COMMAND_ID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12}"
)
SOURCE_RE = re.compile(r"[0-9a-f]{40}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
IMAGE_RE = re.compile(
    r"ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}"
)
TERMINAL_STATUSES = {"Success", "Cancelled", "TimedOut", "Failed"}
ACTIVE_STATUSES = {"Pending", "InProgress", "Delayed"}
MANIFEST_KEYS = {
    "schema_version",
    "source_sha",
    "image_digest",
    "image_size_mib",
    "compose_sha256",
    "compose_prod_sha256",
    "build_run_id",
    "build_job_id",
}


class PromotionError(RuntimeError):
    """A bounded, operator-safe promotion failure."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class Candidate:
    """Immutable protected release tuple.

    ``source_sha`` is a full Git object id, ``image_digest`` is an exact OCI
    digest reference, and ``build_run_id`` is a positive GitHub run identifier.
    """

    source_sha: str
    image_digest: str
    build_run_id: int


def parse_positive_decimal(value: str, category: str) -> int:
    if len(value) > 20 or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise PromotionError(category)
    return int(value)


@dataclass(frozen=True)
class ArtifactContract:
    artifact_id: int
    size_bytes: int
    digest: str
    build_job_id: int


@dataclass(frozen=True)
class ReleaseContract:
    image_size_mib: int
    compose_sha256: str
    compose_prod_sha256: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BinaryCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class HttpClient(Protocol):
    def get_json(
        self,
        url: str,
        token: str,
        limit: int,
        clock: Clock,
        deadline: float,
    ) -> object: ...

    def get_bytes(
        self,
        url: str,
        token: str,
        limit: int,
        clock: Clock,
        deadline: float,
    ) -> bytes: ...


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult: ...

    def run_binary(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
        stdin: bytes,
    ) -> BinaryCommandResult: ...


class Clock(Protocol):
    def monotonic(self) -> float: ...


class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None: ...


class CurlHttpClient:
    """Killable HTTPS adapter whose bearer token exists only on stdin."""

    def __init__(self, runner: CommandRunner, path_env: str) -> None:
        self._runner = runner
        self._env = {"PATH": path_env}

    @staticmethod
    def _token_bytes(token: str) -> bytes:
        try:
            encoded = token.encode("ascii")
        except UnicodeEncodeError as error:
            raise PromotionError("github_token_invalid") from error
        if not encoded or any(
            byte < 0x21 or byte > 0x7E or byte in {0x22, 0x5C}
            for byte in encoded
        ):
            raise PromotionError("github_token_invalid")
        return encoded

    def _get(
        self,
        url: str,
        token: str,
        limit: int,
        clock: Clock,
        deadline: float,
    ) -> bytes:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise PromotionError("http_url_unsafe")
        token_bytes = self._token_bytes(token)
        remaining = deadline - clock.monotonic()
        if remaining < 0.001:
            raise PromotionError("execution_deadline_exhausted")
        max_time = int(remaining * 1000) / 1000
        argv = [
            "curl",
            "--disable",
            "--config", "-",
            "--silent",
            "--fail",
            "--location",
            "--max-redirs", str(HTTP_REDIRECT_LIMIT),
            "--proto", "=https",
            "--proto-redir", "=https",
            "--connect-timeout", str(HTTP_CONNECT_TIMEOUT_SECONDS),
            "--max-time", f"{max_time:.3f}",
            "--max-filesize", str(limit),
            "--user-agent", HTTP_USER_AGENT,
            "--header", "Accept: application/vnd.github+json",
            "--header", "X-GitHub-Api-Version: 2022-11-28",
            "--url", url,
        ]
        config = b'header = "Authorization: Bearer ' + token_bytes + b'"\n'
        try:
            result = self._runner.run_binary(
                argv,
                self._env,
                remaining,
                limit + 1,
                config,
            )
        except PromotionError as error:
            categories = {
                "subprocess_timeout": "execution_deadline_exhausted",
                "subprocess_output_limit_exceeded": "github_response_size_invalid",
            }
            raise PromotionError(
                categories.get(error.category, "github_http_failed")
            ) from error
        if result.returncode != 0:
            exit_categories = {
                28: "execution_deadline_exhausted",
                63: "github_response_size_invalid",
            }
            raise PromotionError(
                exit_categories.get(result.returncode, "github_http_failed")
            )
        if not result.stdout or len(result.stdout) > limit:
            raise PromotionError("github_response_size_invalid")
        return result.stdout

    def get_json(
        self,
        url: str,
        token: str,
        limit: int,
        clock: Clock,
        deadline: float,
    ) -> object:
        try:
            return json.loads(
                self._get(url, token, limit, clock, deadline).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PromotionError("github_json_invalid") from error

    def get_bytes(
        self,
        url: str,
        token: str,
        limit: int,
        clock: Clock,
        deadline: float,
    ) -> bytes:
        return self._get(url, token, limit, clock, deadline)


class SubprocessCommandRunner:
    @staticmethod
    def _read_bounded(
        stream: BinaryIO, limit: int, chunks: list[bytes], overflow: list[bool]
    ) -> None:
        total = 0
        while True:
            block = stream.read(65536)
            if not block:
                return
            total += len(block)
            if total <= limit:
                chunks.append(block)
            else:
                overflow[0] = True

    def run_binary(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
        stdin: bytes,
    ) -> BinaryCommandResult:
        try:
            process = subprocess.Popen(
                list(argv),
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            stdout_overflow = [False]
            stderr_overflow = [False]
            readers = [
                threading.Thread(
                    target=self._read_bounded,
                    args=(process.stdout, output_limit, stdout_chunks, stdout_overflow),
                    daemon=True,
                ),
                threading.Thread(
                    target=self._read_bounded,
                    args=(process.stderr, output_limit, stderr_chunks, stderr_overflow),
                    daemon=True,
                ),
            ]
            for reader in readers:
                reader.start()
            try:
                process.stdin.write(stdin)
            except BrokenPipeError:
                pass
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
            try:
                returncode = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired as error:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise PromotionError("subprocess_timeout") from error
            for reader in readers:
                reader.join(timeout=5)
            if stdout_overflow[0] or stderr_overflow[0]:
                raise PromotionError("subprocess_output_limit_exceeded")
            return BinaryCommandResult(
                returncode,
                b"".join(stdout_chunks),
                b"".join(stderr_chunks),
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise PromotionError("subprocess_failed") from error

    def run(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout_seconds: float,
        output_limit: int,
    ) -> CommandResult:
        result = self.run_binary(
            argv, env, timeout_seconds, output_limit, b""
        )
        try:
            return CommandResult(
                result.returncode,
                result.stdout.decode("utf-8", errors="strict"),
                result.stderr.decode("utf-8", errors="strict"),
            )
        except UnicodeDecodeError as error:
            raise PromotionError("subprocess_output_invalid") from error


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def parse_candidate(source_sha: str, image_digest: str, build_run_id: str) -> Candidate:
    if SOURCE_RE.fullmatch(source_sha) is None:
        raise PromotionError("source_sha_invalid")
    if IMAGE_RE.fullmatch(image_digest) is None:
        raise PromotionError("image_digest_invalid")
    return Candidate(
        source_sha,
        image_digest,
        parse_positive_decimal(build_run_id, "build_run_id_invalid"),
    )


def validate_approved_tuple(
    candidate: Candidate,
    approved_source_sha: str,
    approved_image_digest: str,
    approved_build_run_id: str,
) -> None:
    if (
        candidate.source_sha != approved_source_sha
        or candidate.image_digest != approved_image_digest
        or str(candidate.build_run_id) != approved_build_run_id
    ):
        raise PromotionError("approved_tuple_mismatch")


def validate_fixed_boundary(role_arn: str, region: str, instance_id: str) -> None:
    if role_arn != ROLE_ARN:
        raise PromotionError("role_arn_mismatch")
    if region != REGION:
        raise PromotionError("region_mismatch")
    if instance_id != INSTANCE_ID:
        raise PromotionError("instance_id_mismatch")


def _initial_evidence(
    candidate: Candidate | None, promotion_attempt_id: int | None = None
) -> dict[str, object]:
    return {
        "source_sha": candidate.source_sha if candidate else None,
        "image_digest": candidate.image_digest if candidate else None,
        "image_size_mib": None,
        "compose_sha256": None,
        "compose_prod_sha256": None,
        "build_run_id": candidate.build_run_id if candidate else None,
        "promotion_attempt_id": promotion_attempt_id,
        "instance_id": INSTANCE_ID,
        "command_id": None,
        "terminal_status": None,
        "last_observed_status": "Initialized",
        "poll_attempts": 0,
        "operator_follow_up_required": True,
        "response_code": None,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "instance_verified": False,
        "response_code_verified": False,
        "pass_marker_verified": False,
        "contract_expected_no_github_secrets": True,
        "contract_expected_worker_inactive": True,
    }


def write_private_evidence(path: Path, evidence: Mapping[str, object]) -> None:
    payload = json.dumps(evidence, indent=2, sort_keys=True) + "\n"
    encoded = payload.encode("utf-8")
    if not 0 < len(encoded) <= 8192:
        raise PromotionError("evidence_size_invalid")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def initialize_evidence(
    path: Path,
    source_sha: str,
    image_digest: str,
    build_run_id: str,
    promotion_attempt_id: str,
) -> None:
    try:
        candidate = parse_candidate(source_sha, image_digest, build_run_id)
        attempt_id = parse_positive_decimal(
            promotion_attempt_id, "promotion_attempt_id_invalid"
        )
    except PromotionError:
        candidate = None
        attempt_id = None
    write_private_evidence(path, _initial_evidence(candidate, attempt_id))


def _as_dict(value: object, category: str) -> dict[str, object]:
    if type(value) is not dict:
        raise PromotionError(category)
    return value


def validate_provenance(
    candidate: Candidate,
    run_payload: object,
    jobs_payload: object,
    artifacts_payload: object,
) -> ArtifactContract:
    run = _as_dict(run_payload, "candidate_run_shape_invalid")
    required = {
        "id": candidate.build_run_id,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": candidate.source_sha,
        "path": ".github/workflows/cd-production-check.yml",
        "status": "completed",
        "conclusion": "success",
    }
    if any(run.get(key) != expected for key, expected in required.items()):
        raise PromotionError("candidate_run_mismatch")
    jobs = _as_dict(jobs_payload, "candidate_jobs_shape_invalid")
    jobs_total = jobs.get("total_count")
    if type(jobs_total) is not int or jobs_total > 100:
        raise PromotionError("candidate_jobs_count_invalid")
    job_values = jobs.get("jobs")
    if type(job_values) is not list:
        raise PromotionError("candidate_jobs_shape_invalid")
    matches = [
        job for job in job_values
        if type(job) is dict
        and job.get("name") == "validate and publish immutable candidate"
    ]
    if len(matches) != 1:
        raise PromotionError("candidate_job_not_unique")
    job = matches[0]
    job_id = job.get("id")
    if (
        job.get("status") != "completed"
        or job.get("conclusion") != "success"
        or type(job_id) is not int
        or job_id <= 0
    ):
        raise PromotionError("candidate_job_invalid")
    artifacts = _as_dict(artifacts_payload, "candidate_artifacts_shape_invalid")
    artifacts_total = artifacts.get("total_count")
    artifact_values = artifacts.get("artifacts")
    if (
        type(artifacts_total) is not int
        or artifacts_total > 100
        or type(artifact_values) is not list
    ):
        raise PromotionError("candidate_artifacts_count_invalid")
    expected_name = (
        f"release-manifest-{candidate.source_sha}-{candidate.build_run_id}"
    )
    artifact_matches = [
        item for item in artifact_values
        if type(item) is dict and item.get("name") == expected_name
    ]
    if len(artifact_matches) != 1:
        raise PromotionError("candidate_artifact_not_unique")
    artifact = artifact_matches[0]
    artifact_id = artifact.get("id")
    size_bytes = artifact.get("size_in_bytes")
    digest = artifact.get("digest")
    workflow_run = artifact.get("workflow_run")
    if (
        type(artifact_id) is not int
        or artifact_id <= 0
        or type(size_bytes) is not int
        or not 0 < size_bytes <= 64 * 1024
        or type(digest) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
        or artifact.get("expired") is not False
        or type(workflow_run) is not dict
        or workflow_run.get("id") != candidate.build_run_id
    ):
        raise PromotionError("candidate_artifact_invalid")
    return ArtifactContract(artifact_id, size_bytes, digest, job_id)


def validate_artifact(
    candidate: Candidate, contract: ArtifactContract, archive: bytes
) -> ReleaseContract:
    if len(archive) != contract.size_bytes:
        raise PromotionError("artifact_size_mismatch")
    actual_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    if actual_digest != contract.digest:
        raise PromotionError("artifact_digest_mismatch")
    expected_names = {"release-manifest.json"}
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            infos = bundle.infolist()
            if len(infos) != len(expected_names) or {
                info.filename for info in infos
            } != expected_names:
                raise PromotionError("artifact_member_set_invalid")
            for info in infos:
                path = PurePosixPath(info.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or info.is_dir()
                    or stat.S_ISLNK(info.external_attr >> 16)
                    or info.file_size > 16 * 1024 * 1024
                    or info.compress_size > 4 * 1024 * 1024
                ):
                    raise PromotionError("artifact_member_unsafe")
            payload = bundle.read("release-manifest.json")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("artifact_archive_invalid") from error
    if len(payload) > 16 * 1024:
        raise PromotionError("manifest_size_invalid")
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PromotionError("manifest_json_invalid") from error
    if type(manifest) is not dict or set(manifest) != MANIFEST_KEYS:
        raise PromotionError("manifest_schema_invalid")
    string_keys = {
        "source_sha", "image_digest", "compose_sha256", "compose_prod_sha256"
    }
    integer_keys = {"schema_version", "image_size_mib", "build_run_id", "build_job_id"}
    if any(type(manifest[key]) is not str for key in string_keys) or any(
        type(manifest[key]) is not int for key in integer_keys
    ):
        raise PromotionError("manifest_types_invalid")
    if (
        manifest["schema_version"] != 1
        or manifest["source_sha"] != candidate.source_sha
        or manifest["image_digest"] != candidate.image_digest
        or manifest["build_run_id"] != candidate.build_run_id
        or manifest["build_job_id"] != contract.build_job_id
    ):
        raise PromotionError("manifest_tuple_mismatch")
    image_size = manifest["image_size_mib"]
    compose_hash = manifest["compose_sha256"]
    compose_prod_hash = manifest["compose_prod_sha256"]
    if (
        not 0 < image_size <= MAX_IMAGE_MIB
        or HASH_RE.fullmatch(compose_hash) is None
        or HASH_RE.fullmatch(compose_prod_hash) is None
    ):
        raise PromotionError("manifest_contract_invalid")
    return ReleaseContract(image_size, compose_hash, compose_prod_hash)


def validate_compose_payload(path: str, response: object) -> tuple[bytes, str]:
    value = _as_dict(response, "compose_response_shape_invalid")
    content = value.get("content")
    if (
        value.get("type") != "file"
        or value.get("path") != path
        or value.get("encoding") != "base64"
        or type(content) is not str
    ):
        raise PromotionError("compose_response_contract_invalid")
    try:
        payload = base64.b64decode(
            content.replace("\n", "").encode("ascii"), validate=True
        )
    except (UnicodeEncodeError, binascii.Error) as error:
        raise PromotionError("compose_base64_invalid") from error
    if not 0 < len(payload) <= 1024 * 1024:
        raise PromotionError("compose_size_invalid")
    return payload, hashlib.sha256(payload).hexdigest()


def _validate_image_config(candidate: Candidate, inspected: object) -> int:
    if type(inspected) is not list or len(inspected) != 1 or type(inspected[0]) is not dict:
        raise PromotionError("image_inspect_shape_invalid")
    image = inspected[0]
    config = image.get("Config")
    if type(config) is not dict:
        raise PromotionError("image_config_invalid")
    labels = config.get("Labels")
    if (
        type(labels) is not dict
        or labels.get("org.opencontainers.image.revision") != candidate.source_sha
        or config.get("Entrypoint")
        != ["python", "/usr/local/bin/kiwoom-runtime-entrypoint.py"]
        or config.get("User") != "10001:10001"
    ):
        raise PromotionError("image_runtime_contract_invalid")
    size = image.get("Size")
    if type(size) is not int or size <= 0:
        return 0
    return (size + 1048575) // 1048576


def validate_runtime_image(candidate: Candidate, stdout: str) -> int:
    try:
        inspected = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise PromotionError("image_inspect_json_invalid") from error
    size_mib = _validate_image_config(candidate, inspected)
    if not 0 < size_mib <= MAX_IMAGE_MIB:
        raise PromotionError("image_size_invalid")
    return size_mib


def build_parameters(
    candidate: Candidate,
    promotion_attempt_id: int,
    compose_sha: str,
    compose_prod_sha: str,
) -> dict[str, list[str]]:
    return {
        "ImageDigest": [candidate.image_digest],
        "SourceSha": [candidate.source_sha],
        "PromotionAttemptId": [str(promotion_attempt_id)],
        "ComposeSha256": [compose_sha],
        "ComposeProdSha256": [compose_prod_sha],
        "ExpectedInstanceId": [INSTANCE_ID],
        "Region": [REGION],
    }


def build_send_argv(candidate: Candidate, parameters: Mapping[str, list[str]]) -> list[str]:
    return [
        "aws", "ssm", "send-command", "--region", REGION,
        "--document-name", DOCUMENT_NAME, "--instance-ids", INSTANCE_ID,
        "--timeout-seconds", "750", "--comment",
        f"kiwoom digest promotion {candidate.source_sha}",
        "--parameters", json.dumps(parameters, separators=(",", ":"), sort_keys=True),
        "--query", "Command.CommandId", "--output", "text",
        "--cli-connect-timeout", "10", "--cli-read-timeout", "30",
    ]


def _github_urls(api_url: str, candidate: Candidate) -> dict[str, str]:
    base = f"{api_url}/repos/{REPOSITORY}"
    run = f"{base}/actions/runs/{candidate.build_run_id}"
    return {
        "run": run,
        "jobs": f"{run}/jobs?per_page=100",
        "artifacts": f"{run}/artifacts?per_page=100",
        "artifact": f"{base}/actions/artifacts/{{artifact_id}}/zip",
        "compose": f"{base}/contents/{{path}}?ref={candidate.source_sha}",
    }


def _docker_env(path: str, docker_config: str) -> dict[str, str]:
    return {"PATH": path, "DOCKER_CONFIG": docker_config}


def _aws_env(
    path: str, credentials: Mapping[str, str], max_attempts: str
) -> dict[str, str]:
    required = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
    if any(not credentials.get(key) for key in required):
        raise PromotionError("aws_credentials_missing")
    return {
        "PATH": path,
        "AWS_ACCESS_KEY_ID": credentials["AWS_ACCESS_KEY_ID"],
        "AWS_SECRET_ACCESS_KEY": credentials["AWS_SECRET_ACCESS_KEY"],
        "AWS_SESSION_TOKEN": credentials["AWS_SESSION_TOKEN"],
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_MAX_ATTEMPTS": max_attempts,
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def _remaining(clock: Clock, deadline: float, cap: float) -> float:
    remaining = deadline - clock.monotonic()
    if remaining <= 1.0:
        raise PromotionError("execution_deadline_exhausted")
    return min(cap, remaining - 1.0)


def _exact_image_not_found(stderr: str, image: str) -> bool:
    return stderr.strip() in {
        f"Error: No such image: {image}",
        f"Error response from daemon: No such image: {image}",
    }


def _invocation_does_not_exist(stderr: str) -> bool:
    return re.fullmatch(
        r"An error occurred \(InvocationDoesNotExist\) when calling the "
        r"GetCommandInvocation operation: [^\r\n]{1,512}",
        stderr.strip(),
    ) is not None


def execute(
    *,
    candidate: Candidate,
    approved_source_sha: str,
    approved_image_digest: str,
    approved_build_run_id: str,
    role_arn: str,
    region: str,
    instance_id: str,
    promotion_attempt_id: str,
    evidence_path: Path,
    github_token: str,
    api_url: str,
    path_env: str,
    aws_credentials: Mapping[str, str],
    http: HttpClient,
    runner: CommandRunner,
    clock: Clock,
    sleeper: Sleeper,
) -> None:
    """Re-fetch, validate, send once, and poll without trusting prior state."""
    deadline = clock.monotonic() + EXECUTION_BUDGET_SECONDS
    attempt_id: int | None = None
    evidence = _initial_evidence(candidate)
    write_private_evidence(evidence_path, evidence)
    try:
        attempt_id = parse_positive_decimal(
            promotion_attempt_id, "promotion_attempt_id_invalid"
        )
        evidence["promotion_attempt_id"] = attempt_id
        write_private_evidence(evidence_path, evidence)
        validate_approved_tuple(
            candidate, approved_source_sha, approved_image_digest, approved_build_run_id
        )
        validate_fixed_boundary(role_arn, region, instance_id)
        if not github_token:
            raise PromotionError("github_token_missing")
        urls = _github_urls(api_url, candidate)
        artifact = validate_provenance(
            candidate,
            http.get_json(
                urls["run"], github_token, 1024 * 1024, clock, deadline
            ),
            http.get_json(
                urls["jobs"], github_token, 2 * 1024 * 1024, clock, deadline
            ),
            http.get_json(
                urls["artifacts"], github_token, 1024 * 1024, clock, deadline
            ),
        )
        _remaining(clock, deadline, 30.0)
        archive = http.get_bytes(
            urls["artifact"].format(artifact_id=artifact.artifact_id),
            github_token,
            64 * 1024,
            clock,
            deadline,
        )
        release = validate_artifact(candidate, artifact, archive)
        _remaining(clock, deadline, 30.0)
        compose_hashes: dict[str, str] = {}
        for compose_path in ("compose.yaml", "compose.prod.yaml"):
            _, compose_hashes[compose_path] = validate_compose_payload(
                compose_path,
                http.get_json(
                    urls["compose"].format(path=compose_path),
                    github_token,
                    2 * 1024 * 1024,
                    clock,
                    deadline,
                ),
            )
        if (
            compose_hashes["compose.yaml"] != release.compose_sha256
            or compose_hashes["compose.prod.yaml"] != release.compose_prod_sha256
        ):
            raise PromotionError("manifest_compose_hash_mismatch")
        evidence.update(
            compose_sha256=compose_hashes["compose.yaml"],
            compose_prod_sha256=compose_hashes["compose.prod.yaml"],
            last_observed_status="ProvenanceValidated",
        )
        write_private_evidence(evidence_path, evidence)

        docker_config = tempfile.mkdtemp(prefix="kiwoom-anonymous-docker-")
        try:
            docker_env = _docker_env(path_env, docker_config)
            cached = runner.run(
                ["docker", "image", "inspect", candidate.image_digest],
                docker_env,
                _remaining(clock, deadline, 30.0),
                COMMAND_OUTPUT_LIMIT,
            )
            if cached.returncode == 0:
                removed = runner.run(
                    ["docker", "image", "rm", "--force", candidate.image_digest],
                    docker_env,
                    _remaining(clock, deadline, 60.0),
                    COMMAND_OUTPUT_LIMIT,
                )
                if removed.returncode != 0:
                    raise PromotionError("cached_image_remove_failed")
            elif not _exact_image_not_found(cached.stderr, candidate.image_digest):
                raise PromotionError("cached_image_inspect_failed")
            pull = runner.run(
                ["docker", "pull", candidate.image_digest],
                docker_env,
                _remaining(clock, deadline, 480.0),
                COMMAND_OUTPUT_LIMIT,
            )
            if pull.returncode != 0:
                raise PromotionError("anonymous_image_pull_failed")
            inspect = runner.run(
                ["docker", "image", "inspect", candidate.image_digest],
                docker_env,
                _remaining(clock, deadline, 30.0),
                COMMAND_OUTPUT_LIMIT,
            )
            if inspect.returncode != 0:
                raise PromotionError("image_inspect_failed")
            image_size_mib = validate_runtime_image(candidate, inspect.stdout)
        finally:
            shutil.rmtree(docker_config, ignore_errors=True)
        if image_size_mib != release.image_size_mib:
            raise PromotionError("manifest_image_size_mismatch")
        evidence.update(
            image_size_mib=image_size_mib,
            last_observed_status="RuntimeContractValidated",
        )
        write_private_evidence(evidence_path, evidence)

        parameters = build_parameters(
            candidate,
            cast(int, attempt_id),
            compose_hashes["compose.yaml"],
            compose_hashes["compose.prod.yaml"],
        )
        send_env = _aws_env(
            path_env, aws_credentials, AWS_SEND_MAX_ATTEMPTS
        )
        poll_env = _aws_env(path_env, aws_credentials, AWS_MAX_ATTEMPTS)
        sent = runner.run(
            build_send_argv(candidate, parameters),
            send_env,
            _remaining(clock, deadline, 45.0),
            COMMAND_OUTPUT_LIMIT,
        )
        parameters = {}  # Drop the only in-process reference immediately after consumption.
        if sent.returncode != 0:
            raise PromotionError("send_command_failed")
        command_id = sent.stdout.strip()
        if COMMAND_ID_RE.fullmatch(command_id) is None:
            raise PromotionError("command_id_invalid")
        evidence.update(
            command_id=command_id,
            last_observed_status="CommandCreatedAwaitingInvocation",
        )
        write_private_evidence(evidence_path, evidence)

        invocation: dict[str, object] | None = None
        terminal_status: str | None = None
        for attempt in range(1, POLL_LIMIT + 1):
            evidence.update(
                poll_attempts=attempt,
                last_observed_status="InvocationQueryAttempt",
            )
            write_private_evidence(evidence_path, evidence)
            result = runner.run(
                [
                    "aws", "ssm", "get-command-invocation", "--region", REGION,
                    "--command-id", command_id, "--instance-id", INSTANCE_ID,
                    "--output", "json", "--cli-connect-timeout", "10",
                    "--cli-read-timeout", "30",
                ],
                poll_env,
                _remaining(clock, deadline, 30.0),
                COMMAND_OUTPUT_LIMIT,
            )
            if result.returncode != 0:
                if not _invocation_does_not_exist(result.stderr):
                    raise PromotionError("invocation_query_failed")
                evidence["last_observed_status"] = "InvocationDoesNotExist"
                write_private_evidence(evidence_path, evidence)
            else:
                try:
                    invocation = _as_dict(
                        json.loads(result.stdout), "invocation_shape_invalid"
                    )
                except json.JSONDecodeError as error:
                    raise PromotionError("invocation_json_invalid") from error
                status_value = invocation.get("Status")
                if type(status_value) is not str:
                    raise PromotionError("invocation_status_invalid")
                if status_value not in TERMINAL_STATUSES | ACTIVE_STATUSES:
                    raise PromotionError("invocation_status_invalid")
                evidence["last_observed_status"] = status_value
                write_private_evidence(evidence_path, evidence)
                if status_value in TERMINAL_STATUSES:
                    terminal_status = status_value
                    break
            if attempt < POLL_LIMIT:
                _remaining(clock, deadline, POLL_SECONDS + 1.0)
                sleeper.sleep(POLL_SECONDS)
        if invocation is None or terminal_status is None:
            raise PromotionError("poll_limit_exhausted")
        stdout = invocation.get("StandardOutputContent", "")
        stderr = invocation.get("StandardErrorContent", "")
        if type(stdout) is not str or type(stderr) is not str:
            raise PromotionError("invocation_output_invalid")
        marker = (
            "production check passed: "
            f"source_sha={candidate.source_sha} "
            f"image={candidate.image_digest} rollback=false"
        )
        instance_verified = invocation.get("InstanceId") == INSTANCE_ID
        response_code = invocation.get("ResponseCode")
        response_code_verified = type(response_code) is int and response_code == 0
        marker_verified = stdout.splitlines().count(marker) == 1
        evidence.update(
            terminal_status=terminal_status,
            last_observed_status=terminal_status,
            operator_follow_up_required=terminal_status != "Success",
            response_code=response_code,
            stdout_bytes=len(stdout.encode("utf-8")),
            stderr_bytes=len(stderr.encode("utf-8")),
            instance_verified=instance_verified,
            response_code_verified=response_code_verified,
            pass_marker_verified=marker_verified,
        )
        write_private_evidence(evidence_path, evidence)
        if not (
            terminal_status == "Success"
            and instance_verified
            and response_code_verified
            and marker_verified
        ):
            raise PromotionError("production_check_validation_failed")
    except PromotionError as error:
        evidence["failure_category"] = error.category
        evidence["operator_follow_up_required"] = True
        write_private_evidence(evidence_path, evidence)
        raise
    except Exception as error:
        evidence["failure_category"] = "internal_failure"
        evidence["operator_follow_up_required"] = True
        try:
            write_private_evidence(evidence_path, evidence)
        except Exception:
            pass
        raise PromotionError("internal_failure") from error


def _add_tuple_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--build-run-id", required=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("initialize-evidence")
    _add_tuple_arguments(initialize)
    initialize.add_argument("--promotion-attempt-id", required=True)
    initialize.add_argument("--evidence", type=Path, required=True)
    preflight = commands.add_parser("preflight")
    _add_tuple_arguments(preflight)
    preflight.add_argument("--approved-source-sha", required=True)
    preflight.add_argument("--approved-image-digest", required=True)
    preflight.add_argument("--approved-build-run-id", required=True)
    preflight.add_argument("--role-arn", required=True)
    preflight.add_argument("--region", required=True)
    preflight.add_argument("--instance-id", required=True)
    preflight.add_argument("--promotion-attempt-id", required=True)
    execution = commands.add_parser("execute")
    _add_tuple_arguments(execution)
    execution.add_argument("--approved-source-sha", required=True)
    execution.add_argument("--approved-image-digest", required=True)
    execution.add_argument("--approved-build-run-id", required=True)
    execution.add_argument("--role-arn", required=True)
    execution.add_argument("--region", required=True)
    execution.add_argument("--instance-id", required=True)
    execution.add_argument("--promotion-attempt-id", required=True)
    execution.add_argument("--evidence", type=Path, required=True)
    execution.add_argument("--github-api-url", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "initialize-evidence":
            initialize_evidence(
                arguments.evidence,
                arguments.source_sha,
                arguments.image_digest,
                arguments.build_run_id,
                arguments.promotion_attempt_id,
            )
            return 0
        try:
            candidate = parse_candidate(
                arguments.source_sha,
                arguments.image_digest,
                arguments.build_run_id,
            )
            parse_positive_decimal(
                arguments.promotion_attempt_id,
                "promotion_attempt_id_invalid",
            )
        except PromotionError as error:
            if arguments.command == "execute":
                failed = _initial_evidence(None)
                failed["failure_category"] = error.category
                write_private_evidence(arguments.evidence, failed)
            raise
        if arguments.command == "preflight":
            validate_approved_tuple(
                candidate,
                arguments.approved_source_sha,
                arguments.approved_image_digest,
                arguments.approved_build_run_id,
            )
            validate_fixed_boundary(
                arguments.role_arn, arguments.region, arguments.instance_id
            )
            return 0
        runner = SubprocessCommandRunner()
        execute(
            candidate=candidate,
            approved_source_sha=arguments.approved_source_sha,
            approved_image_digest=arguments.approved_image_digest,
            approved_build_run_id=arguments.approved_build_run_id,
            role_arn=arguments.role_arn,
            region=arguments.region,
            instance_id=arguments.instance_id,
            promotion_attempt_id=arguments.promotion_attempt_id,
            evidence_path=arguments.evidence,
            github_token=os.environ.get("GH_TOKEN", ""),
            api_url=arguments.github_api_url,
            path_env=os.environ.get("PATH", "/usr/bin:/bin"),
            aws_credentials=os.environ,
            http=CurlHttpClient(runner, os.environ.get("PATH", "/usr/bin:/bin")),
            runner=runner,
            clock=SystemClock(),
            sleeper=SystemSleeper(),
        )
        return 0
    except PromotionError as error:
        print(f"promotion failed: {error.category}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
