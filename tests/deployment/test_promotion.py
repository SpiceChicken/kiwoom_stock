"""Tests for the post-OIDC promotion boundary without external writes."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import stat
import time
import zipfile

import pytest

from kiwoom_stock.deployment import promotion


SOURCE = "a" * 40
DIGEST = promotion.IMAGE_PREFIX + "b" * 64
RUN_ID = 123
JOB_ID = 456
COMMAND_ID = "12345678-1234-1234-1234-123456789abc"
COMPOSE = b"services:\n  app: {}\n"
COMPOSE_PROD = b"services:\n  app:\n    read_only: true\n"


def _zip(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, payload in entries.items():
            bundle.writestr(name, payload)
    return output.getvalue()


def _manifest_archive(**updates):
    manifest = {
        "schema_version": 1,
        "source_sha": SOURCE,
        "image_digest": DIGEST,
        "image_size_mib": 400,
        "compose_sha256": hashlib.sha256(COMPOSE).hexdigest(),
        "compose_prod_sha256": hashlib.sha256(COMPOSE_PROD).hexdigest(),
        "build_run_id": RUN_ID,
        "build_job_id": JOB_ID,
    }
    manifest.update(updates)
    return _zip({"release-manifest.json": json.dumps(manifest)})


def _image_inspect(source=SOURCE, size=400 * 1024 * 1024):
    return [
        {
            "Size": size,
            "Config": {
                "Entrypoint": [
                    "python",
                    "/usr/local/bin/kiwoom-runtime-entrypoint.py",
                ],
                "User": "10001:10001",
                "Labels": {"org.opencontainers.image.revision": source},
            },
        }
    ]


class FakeHttp:
    def __init__(self, archive=None):
        self.archive = archive if archive is not None else _manifest_archive()
        self.calls = []

    def get_json(self, url, token, limit, clock, deadline):
        assert clock.monotonic() < deadline
        self.calls.append(("json", url, token, limit))
        if url.endswith(f"/actions/runs/{RUN_ID}"):
            return {
                "id": RUN_ID,
                "event": "workflow_dispatch",
                "head_branch": "main",
                "head_sha": SOURCE,
                "path": ".github/workflows/cd-production-check.yml",
                "status": "completed",
                "conclusion": "success",
            }
        if url.endswith("/jobs?per_page=100"):
            return {
                "total_count": 1,
                "jobs": [
                    {
                        "id": JOB_ID,
                        "name": "validate and publish immutable candidate",
                        "status": "completed",
                        "conclusion": "success",
                    }
                ],
            }
        if url.endswith("/artifacts?per_page=100"):
            return {
                "total_count": 1,
                "artifacts": [
                    {
                        "id": 789,
                        "name": f"release-manifest-{SOURCE}-{RUN_ID}",
                        "size_in_bytes": len(self.archive),
                        "digest": "sha256:" + hashlib.sha256(self.archive).hexdigest(),
                        "expired": False,
                        "workflow_run": {"id": RUN_ID},
                    }
                ],
            }
        for name, payload in (
            ("compose.yaml", COMPOSE),
            ("compose.prod.yaml", COMPOSE_PROD),
        ):
            if f"/contents/{name}?" in url:
                return {
                    "type": "file",
                    "path": name,
                    "encoding": "base64",
                    "content": base64.b64encode(payload).decode("ascii"),
                }
        raise AssertionError(url)

    def get_bytes(self, url, token, limit, clock, deadline):
        assert clock.monotonic() < deadline
        self.calls.append(("bytes", url, token, limit))
        return self.archive


class FakeRunner:
    def __init__(
        self,
        *,
        command_id=COMMAND_ID,
        statuses=None,
        send_code=0,
        cached_code=0,
        cached_stderr="",
        remove_code=0,
        pull_code=0,
        instance_id=promotion.INSTANCE_ID,
        response_code=0,
        success_marker=True,
    ):
        self.command_id = command_id
        self.statuses = list(statuses or ["Pending", "Success"])
        self.send_code = send_code
        self.cached_code = cached_code
        self.cached_stderr = cached_stderr
        self.remove_code = remove_code
        self.pull_code = pull_code
        self.instance_id = instance_id
        self.response_code = response_code
        self.success_marker = success_marker
        self.calls = []
        self.inspect_count = 0

    def run(self, argv, env, timeout_seconds, output_limit):
        argv = list(argv)
        env = dict(env)
        self.calls.append((argv, env, timeout_seconds, output_limit))
        if argv[:2] == ["docker", "pull"]:
            assert "GH_TOKEN" not in env
            assert "AWS_ACCESS_KEY_ID" not in env
            return promotion.CommandResult(self.pull_code, "pulled", "pull failed")
        if argv[:4] == ["docker", "image", "rm", "--force"]:
            return promotion.CommandResult(self.remove_code, "removed", "remove failed")
        if argv[:3] == ["docker", "image", "inspect"]:
            assert "GH_TOKEN" not in env
            assert "AWS_ACCESS_KEY_ID" not in env
            self.inspect_count += 1
            if self.inspect_count == 1:
                return promotion.CommandResult(
                    self.cached_code,
                    json.dumps(_image_inspect()) if self.cached_code == 0 else "",
                    self.cached_stderr,
                )
            return promotion.CommandResult(0, json.dumps(_image_inspect()), "")
        assert argv[0] == "aws"
        assert "GH_TOKEN" not in env
        assert set(env) == {
            "PATH",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "AWS_DEFAULT_REGION",
            "AWS_MAX_ATTEMPTS",
            "AWS_EC2_METADATA_DISABLED",
        }
        if argv[1:3] == ["ssm", "send-command"]:
            return promotion.CommandResult(self.send_code, self.command_id, "secret output")
        status = self.statuses.pop(0)
        if isinstance(status, tuple):
            return promotion.CommandResult(*status)
        marker = (
            "production check passed: "
            f"source_sha={SOURCE} image={DIGEST} rollback=false"
        )
        payload = {
            "Status": status,
            "InstanceId": self.instance_id,
            "ResponseCode": self.response_code,
            "StandardOutputContent": (
                marker + "\n"
                if status == "Success" and self.success_marker
                else "wrong marker\n" if status == "Success" else ""
            ),
            "StandardErrorContent": "",
        }
        return promotion.CommandResult(0, json.dumps(payload), "")


class FakeClock:
    def monotonic(self):
        return 0.0


class AdvancingClock:
    def __init__(self, step):
        self.value = -step
        self.step = step

    def monotonic(self):
        self.value += self.step
        return self.value


class ManualClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class FakeBinaryRunner:
    def __init__(self, result=None, error=None):
        self.result = result or promotion.BinaryCommandResult(0, b"payload", b"")
        self.error = error
        self.calls = []

    def run_binary(self, argv, env, timeout_seconds, output_limit, stdin):
        self.calls.append(
            (list(argv), dict(env), timeout_seconds, output_limit, stdin)
        )
        if self.error is not None:
            raise promotion.PromotionError(self.error)
        return self.result

    def run(self, argv, env, timeout_seconds, output_limit):
        raise AssertionError("text command path is not used by the HTTP adapter")


class FakeSleeper:
    def __init__(self):
        self.calls = []

    def sleep(self, seconds):
        self.calls.append(seconds)


def _execute(
    tmp_path,
    *,
    http=None,
    runner=None,
    approved_source=SOURCE,
    clock=None,
    aws_credentials=None,
):
    http = http or FakeHttp()
    runner = runner or FakeRunner()
    sleeper = FakeSleeper()
    evidence = tmp_path / "evidence.json"
    promotion.execute(
        candidate=promotion.Candidate(SOURCE, DIGEST, RUN_ID),
        approved_source_sha=approved_source,
        approved_image_digest=DIGEST,
        approved_build_run_id=str(RUN_ID),
        role_arn=promotion.ROLE_ARN,
        region=promotion.REGION,
        instance_id=promotion.INSTANCE_ID,
        promotion_attempt_id="999",
        evidence_path=evidence,
        github_token="github-token",
        api_url="https://api.github.test",
        path_env="/usr/bin:/bin",
        aws_credentials=(
            {
                "AWS_ACCESS_KEY_ID": "access",
                "AWS_SECRET_ACCESS_KEY": "secret",
                "AWS_SESSION_TOKEN": "session",
            }
            if aws_credentials is None
            else aws_credentials
        ),
        http=http,
        runner=runner,
        clock=clock or FakeClock(),
        sleeper=sleeper,
    )
    return evidence, http, runner, sleeper


@pytest.mark.parametrize(
    ("source", "digest", "run_id", "category"),
    [
        ("A" * 40, DIGEST, "123", "source_sha_invalid"),
        (SOURCE, promotion.IMAGE_PREFIX + "B" * 64, "123", "image_digest_invalid"),
        (SOURCE, DIGEST, "0", "build_run_id_invalid"),
        (SOURCE, DIGEST, "01", "build_run_id_invalid"),
    ],
)
def test_candidate_contract_is_exact(source, digest, run_id, category):
    with pytest.raises(promotion.PromotionError, match=category):
        promotion.parse_candidate(source, digest, run_id)


def test_http_client_rejects_unsafe_initial_url_without_network():
    runner = FakeBinaryRunner()
    with pytest.raises(promotion.PromotionError, match="http_url_unsafe"):
        promotion.CurlHttpClient(runner, "/usr/bin:/bin").get_bytes(
            "http://api.github.test/artifact",
            "token",
            1024,
            FakeClock(),
            10.0,
        )
    assert runner.calls == []


def test_curl_http_boundary_keeps_token_only_on_stdin_and_is_https_bounded():
    clock = ManualClock()
    clock.advance(2.0)
    runner = FakeBinaryRunner(
        promotion.BinaryCommandResult(0, b"response", b"")
    )
    token = "ghs_secret_123"
    client = promotion.CurlHttpClient(runner, "/trusted/bin")

    assert client.get_bytes(
        "https://api.github.test/artifact", token, 1024, clock, 14.345
    ) == b"response"

    argv, env, timeout, output_limit, stdin = runner.calls[0]
    assert env == {"PATH": "/trusted/bin"}
    assert timeout == pytest.approx(12.345)
    assert output_limit == 1025
    assert token.encode() in stdin
    assert token not in " ".join(argv)
    assert token not in repr(env)
    assert argv[argv.index("--user-agent") + 1] == promotion.HTTP_USER_AGENT
    assert argv[1] == "--disable"
    assert argv[argv.index("--proto") + 1] == "=https"
    assert argv[argv.index("--proto-redir") + 1] == "=https"
    assert argv[argv.index("--max-redirs") + 1] == str(
        promotion.HTTP_REDIRECT_LIMIT
    )
    assert argv[argv.index("--connect-timeout") + 1] == str(
        promotion.HTTP_CONNECT_TIMEOUT_SECONDS
    )
    assert float(argv[argv.index("--max-time") + 1]) <= timeout
    assert argv[argv.index("--max-filesize") + 1] == "1024"
    assert "--location" in argv
    assert "--location-trusted" not in argv
    assert stdin == b'header = "Authorization: Bearer ghs_secret_123"\n'


@pytest.mark.parametrize("token", ["", "bad token", "bad\nnext", 'bad"quote'])
def test_curl_http_token_config_is_injection_safe(token):
    runner = FakeBinaryRunner()
    with pytest.raises(promotion.PromotionError, match="github_token_invalid"):
        promotion.CurlHttpClient(runner, "/usr/bin").get_bytes(
            "https://api.github.test/artifact",
            token,
            1024,
            FakeClock(),
            10.0,
        )
    assert runner.calls == []


@pytest.mark.parametrize(
    ("runner_category", "http_category"),
    [
        ("subprocess_timeout", "execution_deadline_exhausted"),
        ("subprocess_output_limit_exceeded", "github_response_size_invalid"),
        ("subprocess_failed", "github_http_failed"),
    ],
)
def test_curl_http_maps_raw_process_errors_to_bounded_categories(
    runner_category, http_category
):
    runner = FakeBinaryRunner(error=runner_category)
    with pytest.raises(promotion.PromotionError, match=http_category) as raised:
        promotion.CurlHttpClient(runner, "/usr/bin").get_bytes(
            "https://api.github.test/signed?secret=hidden",
            "ghs_secret",
            1024,
            FakeClock(),
            10.0,
        )
    assert "signed" not in str(raised.value)
    assert "ghs_secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("returncode", "http_category"),
    [
        (28, "execution_deadline_exhausted"),
        (63, "github_response_size_invalid"),
        (22, "github_http_failed"),
    ],
)
def test_curl_http_maps_native_exit_codes_to_bounded_categories(
    returncode, http_category
):
    runner = FakeBinaryRunner(
        result=promotion.BinaryCommandResult(
            returncode,
            b"",
            b"signed URL and token must remain private",
        )
    )
    with pytest.raises(promotion.PromotionError, match=http_category) as raised:
        promotion.CurlHttpClient(runner, "/usr/bin").get_bytes(
            "https://api.github.test/signed?secret=hidden",
            "ghs_secret",
            1024,
            FakeClock(),
            10.0,
        )
    assert str(raised.value) == http_category
    assert "signed" not in str(raised.value)
    assert "ghs_secret" not in str(raised.value)


def test_initialize_evidence_is_bounded_private_and_invalid_input_is_redacted(tmp_path):
    path = tmp_path / "evidence.json"
    promotion.initialize_evidence(
        path, "invalid", "invalid", "invalid", "invalid"
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_sha"] is None
    assert payload["image_digest"] is None
    assert payload["command_id"] is None
    assert payload["last_observed_status"] == "Initialized"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.stat().st_size <= 8192


def test_atomic_evidence_fsyncs_file_and_parent_directory(tmp_path, monkeypatch):
    calls = []
    original = os.fsync

    def recording_fsync(fd):
        calls.append(stat.S_ISDIR(os.fstat(fd).st_mode))
        original(fd)

    monkeypatch.setattr(promotion.os, "fsync", recording_fsync)
    promotion.write_private_evidence(
        tmp_path / "evidence.json", promotion._initial_evidence(None)
    )
    assert calls == [False, True]


def test_preflight_requires_exact_tuple_and_fixed_boundary():
    candidate = promotion.parse_candidate(SOURCE, DIGEST, str(RUN_ID))
    promotion.validate_approved_tuple(candidate, SOURCE, DIGEST, str(RUN_ID))
    promotion.validate_fixed_boundary(
        promotion.ROLE_ARN, promotion.REGION, promotion.INSTANCE_ID
    )
    with pytest.raises(promotion.PromotionError, match="approved_tuple_mismatch"):
        promotion.validate_approved_tuple(candidate, "c" * 40, DIGEST, str(RUN_ID))
    with pytest.raises(promotion.PromotionError, match="role_arn_mismatch"):
        promotion.validate_fixed_boundary("other", promotion.REGION, promotion.INSTANCE_ID)


@pytest.mark.parametrize("mutation", ["extra", "bool", "tuple", "hash", "size"])
def test_modern_manifest_is_strict(mutation):
    updates = {}
    if mutation == "extra":
        updates["unexpected"] = "value"
    elif mutation == "bool":
        updates["build_job_id"] = True
    elif mutation == "tuple":
        updates["source_sha"] = "c" * 40
    elif mutation == "hash":
        updates["compose_sha256"] = "C" * 64
    elif mutation == "size":
        updates["image_size_mib"] = 851
    archive = _manifest_archive(**updates)
    contract = promotion.ArtifactContract(
        1,
        len(archive),
        "sha256:" + hashlib.sha256(archive).hexdigest(),
        JOB_ID,
        False,
    )
    with pytest.raises(promotion.PromotionError):
        promotion.validate_artifact(
            promotion.Candidate(SOURCE, DIGEST, RUN_ID), contract, archive
        )


def test_legacy_contract_is_limited_to_the_exact_candidate():
    inspected = _image_inspect(
        source=promotion.LEGACY_SOURCE_SHA,
        size=400 * 1024 * 1024,
    )
    archive = _zip(
        {
            "reports/pytest-production-check.xml": "<testsuite/>",
            "runtime-image-inspect.json": json.dumps(inspected),
        }
    )
    contract = promotion.ArtifactContract(
        1,
        len(archive),
        "sha256:" + hashlib.sha256(archive).hexdigest(),
        promotion.LEGACY_BUILD_JOB_ID,
        True,
    )
    release = promotion.validate_artifact(
        promotion.Candidate(
            promotion.LEGACY_SOURCE_SHA,
            promotion.LEGACY_IMAGE_DIGEST,
            promotion.LEGACY_BUILD_RUN_ID,
        ),
        contract,
        archive,
    )
    assert release == promotion.ReleaseContract(None, None, None)


def _legacy_provenance(candidate=None):
    candidate = candidate or promotion.Candidate(
        promotion.LEGACY_SOURCE_SHA,
        promotion.LEGACY_IMAGE_DIGEST,
        promotion.LEGACY_BUILD_RUN_ID,
    )
    run = {
        "id": candidate.build_run_id,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": candidate.source_sha,
        "path": ".github/workflows/cd-production-check.yml",
        "status": "completed",
        "conclusion": "cancelled",
    }
    jobs = {
        "total_count": 1,
        "jobs": [
            {
                "id": promotion.LEGACY_BUILD_JOB_ID,
                "name": "validate and publish immutable candidate",
                "status": "completed",
                "conclusion": "success",
            }
        ],
    }
    artifacts = {
        "total_count": 1,
        "artifacts": [
            {
                "id": 7,
                "name": f"candidate-{candidate.source_sha}",
                "size_in_bytes": 100,
                "digest": "sha256:" + "c" * 64,
                "expired": False,
                "workflow_run": {"id": candidate.build_run_id},
            }
        ],
    }
    return candidate, run, jobs, artifacts


def test_legacy_provenance_positive_is_exact():
    candidate, run, jobs, artifacts = _legacy_provenance()
    contract = promotion.validate_provenance(candidate, run, jobs, artifacts)
    assert contract.legacy is True
    assert contract.build_job_id == promotion.LEGACY_BUILD_JOB_ID


@pytest.mark.parametrize(
    "mutation", ["source", "digest", "run", "job", "conclusion", "artifact"]
)
def test_legacy_provenance_near_misses_are_rejected(mutation):
    candidate, run, jobs, artifacts = _legacy_provenance()
    if mutation == "source":
        candidate = promotion.Candidate(
            "d" * 40, candidate.image_digest, candidate.build_run_id
        )
        run["head_sha"] = candidate.source_sha
    elif mutation == "digest":
        candidate = promotion.Candidate(
            candidate.source_sha,
            promotion.IMAGE_PREFIX + "d" * 64,
            candidate.build_run_id,
        )
    elif mutation == "run":
        candidate = promotion.Candidate(
            candidate.source_sha, candidate.image_digest, candidate.build_run_id + 1
        )
        run["id"] = candidate.build_run_id
        artifacts["artifacts"][0]["workflow_run"]["id"] = candidate.build_run_id
    elif mutation == "job":
        jobs["jobs"][0]["id"] += 1
    elif mutation == "conclusion":
        run["conclusion"] = "success"
    elif mutation == "artifact":
        artifacts["artifacts"][0]["name"] = "candidate-near-miss"
    with pytest.raises(promotion.PromotionError):
        promotion.validate_provenance(candidate, run, jobs, artifacts)


def _modern_provenance_payloads():
    candidate = promotion.Candidate(SOURCE, DIGEST, RUN_ID)
    run = {
        "id": RUN_ID,
        "event": "workflow_dispatch",
        "head_branch": "main",
        "head_sha": SOURCE,
        "path": ".github/workflows/cd-production-check.yml",
        "status": "completed",
        "conclusion": "success",
    }
    job = {
        "id": JOB_ID,
        "name": "validate and publish immutable candidate",
        "status": "completed",
        "conclusion": "success",
    }
    artifact = {
        "id": 789,
        "name": f"release-manifest-{SOURCE}-{RUN_ID}",
        "size_in_bytes": 100,
        "digest": "sha256:" + "c" * 64,
        "expired": False,
        "workflow_run": {"id": RUN_ID},
    }
    return (
        candidate,
        run,
        {"total_count": 1, "jobs": [job]},
        {"total_count": 1, "artifacts": [artifact]},
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "job_zero",
        "job_two",
        "artifact_zero",
        "artifact_two",
        "jobs_shape",
        "artifacts_shape",
        "jobs_pagination",
        "artifacts_pagination",
    ],
)
def test_modern_provenance_collection_negatives_are_fail_closed(mutation):
    candidate, run, jobs, artifacts = _modern_provenance_payloads()
    if mutation == "job_zero":
        jobs["jobs"] = []
    elif mutation == "job_two":
        jobs["jobs"] = jobs["jobs"] * 2
    elif mutation == "artifact_zero":
        artifacts["artifacts"] = []
    elif mutation == "artifact_two":
        artifacts["artifacts"] = artifacts["artifacts"] * 2
    elif mutation == "jobs_shape":
        jobs["jobs"] = "not-a-list"
    elif mutation == "artifacts_shape":
        artifacts["artifacts"] = "not-a-list"
    elif mutation == "jobs_pagination":
        jobs["total_count"] = 101
    elif mutation == "artifacts_pagination":
        artifacts["total_count"] = 101
    with pytest.raises(promotion.PromotionError):
        promotion.validate_provenance(candidate, run, jobs, artifacts)


@pytest.mark.parametrize(
    "response",
    [
        {"type": "file", "path": "wrong.yaml", "encoding": "base64", "content": "YQ=="},
        {"type": "file", "path": "compose.yaml", "encoding": "base64", "content": "!!!!"},
        {
            "type": "file",
            "path": "compose.yaml",
            "encoding": "base64",
            "content": base64.b64encode(b"x" * (1024 * 1024 + 1)).decode("ascii"),
        },
    ],
)
def test_compose_contents_contract_negatives(response):
    with pytest.raises(promotion.PromotionError):
        promotion.validate_compose_payload("compose.yaml", response)


@pytest.mark.parametrize("mutation", ["extra_member", "symlink", "compression"])
def test_artifact_zip_member_and_compression_bounds(mutation):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        if mutation == "extra_member":
            bundle.writestr("release-manifest.json", "{}")
            bundle.writestr("unexpected.txt", "x")
        elif mutation == "symlink":
            info = zipfile.ZipInfo("release-manifest.json")
            info.external_attr = 0o120777 << 16
            bundle.writestr(info, "{}")
        else:
            bundle.writestr("release-manifest.json", b"x" * (4 * 1024 * 1024 + 1))
    archive = output.getvalue()
    contract = promotion.ArtifactContract(
        1,
        len(archive),
        "sha256:" + hashlib.sha256(archive).hexdigest(),
        JOB_ID,
        False,
    )
    with pytest.raises(promotion.PromotionError):
        promotion.validate_artifact(
            promotion.Candidate(SOURCE, DIGEST, RUN_ID), contract, archive
        )


def test_execute_refetches_everything_sends_once_polls_and_writes_redacted_evidence(
    tmp_path,
):
    evidence, http, runner, sleeper = _execute(tmp_path)

    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["terminal_status"] == "Success"
    assert payload["command_id"] == COMMAND_ID
    assert payload["poll_attempts"] == 2
    assert payload["instance_verified"] is True
    assert payload["response_code_verified"] is True
    assert payload["pass_marker_verified"] is True
    assert payload["operator_follow_up_required"] is False
    assert "github-token" not in evidence.read_text(encoding="utf-8")
    assert "secret output" not in evidence.read_text(encoding="utf-8")
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert len(http.calls) == 6
    sends = [call for call in runner.calls if call[0][1:3] == ["ssm", "send-command"]]
    polls = [
        call for call in runner.calls
        if call[0][1:3] == ["ssm", "get-command-invocation"]
    ]
    assert len(sends) == 1
    assert len(polls) == 2
    parameters = json.loads(sends[0][0][sends[0][0].index("--parameters") + 1])
    assert parameters == promotion.build_parameters(
        promotion.Candidate(SOURCE, DIGEST, RUN_ID),
        999,
        hashlib.sha256(COMPOSE).hexdigest(),
        hashlib.sha256(COMPOSE_PROD).hexdigest(),
    )
    assert sleeper.calls == [promotion.POLL_SECONDS]
    docker_argv = [call[0][:4] for call in runner.calls if call[0][0] == "docker"]
    assert docker_argv == [
        ["docker", "image", "inspect", DIGEST],
        ["docker", "image", "rm", "--force"],
        ["docker", "pull", DIGEST],
        ["docker", "image", "inspect", DIGEST],
    ]
    assert sends[0][1]["AWS_MAX_ATTEMPTS"] == "1"
    assert all(call[1]["AWS_MAX_ATTEMPTS"] == "3" for call in polls)


def test_execute_does_not_read_or_trust_initial_evidence(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text("not json and not authoritative", encoding="utf-8")
    result, _, _, _ = _execute(tmp_path)
    assert json.loads(result.read_text(encoding="utf-8"))["terminal_status"] == "Success"


def test_tuple_failure_performs_no_http_docker_or_aws(tmp_path):
    http = FakeHttp()
    runner = FakeRunner()
    with pytest.raises(promotion.PromotionError, match="approved_tuple_mismatch"):
        _execute(tmp_path, http=http, runner=runner, approved_source="c" * 40)
    assert http.calls == []
    assert runner.calls == []
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["command_id"] is None
    assert payload["poll_attempts"] == 0


def test_malformed_command_id_fails_without_polling(tmp_path):
    runner = FakeRunner(command_id="not-a-command-id")
    with pytest.raises(promotion.PromotionError, match="command_id_invalid"):
        _execute(tmp_path, runner=runner)
    sends = [call for call in runner.calls if call[0][1:3] == ["ssm", "send-command"]]
    polls = [
        call for call in runner.calls
        if call[0][1:3] == ["ssm", "get-command-invocation"]
    ]
    assert len(sends) == 1
    assert polls == []
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["command_id"] is None
    assert payload["failure_category"] == "command_id_invalid"


def test_send_failure_records_only_bounded_category(tmp_path):
    runner = FakeRunner(send_code=42)
    with pytest.raises(promotion.PromotionError, match="send_command_failed"):
        _execute(tmp_path, runner=runner)
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["command_id"] is None
    assert payload["poll_attempts"] == 0
    assert payload["failure_category"] == "send_command_failed"
    assert "secret output" not in json.dumps(payload)


@pytest.mark.parametrize(
    "missing",
    ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"],
)
def test_missing_aws_credential_fails_before_send(tmp_path, missing):
    credentials = {
        "AWS_ACCESS_KEY_ID": "access",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_SESSION_TOKEN": "session",
    }
    credentials.pop(missing)
    runner = FakeRunner()
    with pytest.raises(promotion.PromotionError, match="aws_credentials_missing"):
        _execute(tmp_path, runner=runner, aws_credentials=credentials)
    assert not any(
        call[0][1:3] == ["ssm", "send-command"] for call in runner.calls
    )


@pytest.mark.parametrize(
    "runner",
    [
        FakeRunner(instance_id="i-00000000000000000"),
        FakeRunner(response_code=1),
        FakeRunner(success_marker=False),
    ],
)
def test_terminal_success_still_requires_instance_response_and_marker(
    tmp_path, runner
):
    with pytest.raises(
        promotion.PromotionError, match="production_check_validation_failed"
    ):
        _execute(tmp_path, runner=runner)
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["terminal_status"] == "Success"
    assert not all(
        (
            payload["instance_verified"],
            payload["response_code_verified"],
            payload["pass_marker_verified"],
        )
    )


@pytest.mark.parametrize(
    ("runner", "category"),
    [
        (FakeRunner(remove_code=1), "cached_image_remove_failed"),
        (FakeRunner(pull_code=1), "anonymous_image_pull_failed"),
        (
            FakeRunner(cached_code=1, cached_stderr="unexpected daemon failure"),
            "cached_image_inspect_failed",
        ),
    ],
)
def test_anonymous_image_boundary_fails_before_send(tmp_path, runner, category):
    with pytest.raises(promotion.PromotionError, match=category):
        _execute(tmp_path, runner=runner)
    assert not any(
        call[0][1:3] == ["ssm", "send-command"] for call in runner.calls
    )


def test_exact_cached_image_not_found_allows_anonymous_pull(tmp_path):
    runner = FakeRunner(
        cached_code=1,
        cached_stderr=f"Error response from daemon: No such image: {DIGEST}",
    )
    _execute(tmp_path, runner=runner)
    docker_calls = [call[0] for call in runner.calls if call[0][0] == "docker"]
    assert not any(call[1:3] == ["image", "rm"] for call in docker_calls)
    assert any(call[1] == "pull" for call in docker_calls)


def test_invocation_does_not_exist_is_the_only_retryable_poll_error(tmp_path):
    missing = (
        1,
        "",
        "An error occurred (InvocationDoesNotExist) when calling the "
        "GetCommandInvocation operation: command is not ready",
    )
    runner = FakeRunner(statuses=[missing, "Success"])
    evidence, _, _, sleeper = _execute(tmp_path, runner=runner)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert payload["terminal_status"] == "Success"
    assert payload["poll_attempts"] == 2
    assert sleeper.calls == [promotion.POLL_SECONDS]


@pytest.mark.parametrize(
    "stderr",
    [
        "An error occurred (AccessDeniedException) when calling the "
        "GetCommandInvocation operation: denied",
        "network connection failed",
        "An error occurred (ExpiredTokenException) when calling the "
        "GetCommandInvocation operation: expired",
    ],
)
def test_other_poll_errors_fail_immediately_with_durable_attempt(
    tmp_path, stderr
):
    runner = FakeRunner(statuses=[(1, "", stderr)])
    with pytest.raises(promotion.PromotionError, match="invocation_query_failed"):
        _execute(tmp_path, runner=runner)
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["poll_attempts"] == 1
    assert payload["last_observed_status"] == "InvocationQueryAttempt"
    polls = [
        call for call in runner.calls
        if call[0][1:3] == ["ssm", "get-command-invocation"]
    ]
    assert len(polls) == 1


def test_end_to_end_deadline_fails_before_job_timeout_and_before_send(tmp_path):
    runner = FakeRunner()
    with pytest.raises(promotion.PromotionError, match="execution_deadline_exhausted"):
        _execute(tmp_path, runner=runner, clock=AdvancingClock(250.0))
    assert promotion.EXECUTION_BUDGET_SECONDS < 25 * 60
    assert not any(
        call[0][1:3] == ["ssm", "send-command"] for call in runner.calls
    )
    payload = json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))
    assert payload["failure_category"] == "execution_deadline_exhausted"


def test_subprocess_runner_bounds_timeout_and_output():
    runner = promotion.SubprocessCommandRunner()
    with pytest.raises(promotion.PromotionError, match="subprocess_timeout"):
        runner.run(
            ["sh", "-c", "sleep 2"],
            {"PATH": "/usr/bin:/bin"},
            0.05,
            1024,
        )
    with pytest.raises(
        promotion.PromotionError, match="subprocess_output_limit_exceeded"
    ):
        runner.run(
            ["sh", "-c", "head -c 2048 /dev/zero"],
            {"PATH": "/usr/bin:/bin"},
            2.0,
            1024,
        )


@pytest.mark.parametrize("phase", ["dns", "tls", "header", "chunk"])
def test_binary_runner_kills_slow_http_worker_process_group(tmp_path, phase):
    marker = tmp_path / f"{phase}.survived"
    runner = promotion.SubprocessCommandRunner()
    secret = "signed-url-and-token-must-not-reach-error"
    with pytest.raises(promotion.PromotionError) as raised:
        runner.run_binary(
            [
                "sh",
                "-c",
                'printf "%s" "$2" >&2; (sleep 0.15; touch "$1") & wait',
                "http-worker",
                str(marker),
                secret,
            ],
            {"PATH": "/usr/bin:/bin"},
            0.03,
            128,
            b"stdin-secret",
        )
    assert raised.value.category == "subprocess_timeout"
    assert secret not in str(raised.value)
    assert "stdin-secret" not in str(raised.value)
    time.sleep(0.2)
    assert not marker.exists()


def test_unexpected_adapter_error_records_internal_failure(tmp_path):
    class BrokenHttp(FakeHttp):
        def get_json(self, url, token, limit, clock, deadline):
            raise NotImplementedError("external implementation detail")

    with pytest.raises(promotion.PromotionError, match="internal_failure"):
        _execute(tmp_path, http=BrokenHttp())
    text = (tmp_path / "evidence.json").read_text(encoding="utf-8")
    assert "external implementation detail" not in text
    assert json.loads(text)["failure_category"] == "internal_failure"


@pytest.mark.parametrize(
    "status", ["Cancelled", "TimedOut", "Failed", "Cancelling", "Success"]
)
def test_terminal_and_unknown_statuses_fail_closed(tmp_path, status):
    runner = FakeRunner(statuses=[status])
    if status == "Success":
        _execute(tmp_path, runner=runner)
    else:
        with pytest.raises(promotion.PromotionError):
            _execute(tmp_path, runner=runner)


def test_cli_preflight_has_no_output_transport(tmp_path, capsys):
    del tmp_path
    result = promotion.main(
        [
            "preflight",
            "--source-sha", SOURCE,
            "--image-digest", DIGEST,
            "--build-run-id", str(RUN_ID),
            "--promotion-attempt-id", "999",
            "--approved-source-sha", SOURCE,
            "--approved-image-digest", DIGEST,
            "--approved-build-run-id", str(RUN_ID),
            "--role-arn", promotion.ROLE_ARN,
            "--region", promotion.REGION,
            "--instance-id", promotion.INSTANCE_ID,
        ]
    )
    assert result == 0
    assert capsys.readouterr().out == ""


def test_cli_execute_tuple_failure_updates_evidence_before_external_io(
    tmp_path, monkeypatch
):
    evidence = tmp_path / "evidence.json"
    monkeypatch.setenv("GH_TOKEN", "unused")
    result = promotion.main(
        [
            "execute",
            "--source-sha", SOURCE,
            "--image-digest", DIGEST,
            "--build-run-id", str(RUN_ID),
            "--promotion-attempt-id", "999",
            "--approved-source-sha", "c" * 40,
            "--approved-image-digest", DIGEST,
            "--approved-build-run-id", str(RUN_ID),
            "--role-arn", promotion.ROLE_ARN,
            "--region", promotion.REGION,
            "--instance-id", promotion.INSTANCE_ID,
            "--evidence", str(evidence),
            "--github-api-url", "https://api.github.test",
        ]
    )
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    assert result == 1
    assert payload["failure_category"] == "approved_tuple_mismatch"
    assert payload["command_id"] is None
