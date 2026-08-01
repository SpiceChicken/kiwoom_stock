#!/usr/bin/env bash
#
# Root-owned EC2 command for one immutable, networkless configuration check.
# Production credentials are metadata-checked but are never mounted.

set -Eeuo pipefail

readonly EXPECTED_REPOSITORY="SpiceChicken/kiwoom_stock"
readonly IMAGE_PREFIX="ghcr.io/spicechicken/kiwoom_stock@sha256:"
readonly LOCK_FILE="${KIWOOM_DEPLOY_LOCK_FILE:-/run/lock/kiwoom-stock-deploy.lock}"
readonly STATE_DIR="${KIWOOM_DEPLOY_STATE_DIR:-/opt/kiwoom-stock/deployments}"
readonly SOURCE_DIR="${STATE_DIR}/sources"
readonly ATTEMPT_DIR="${STATE_DIR}/promotion-attempts"
readonly SECRET_DIR="${KIWOOM_DEPLOY_SECRET_DIR:-/run/kiwoom-stock/credentials}"
readonly APP_KEY_FILE="${SECRET_DIR}/app-key"
readonly SECRET_KEY_FILE="${SECRET_DIR}/secret-key"
readonly RUNTIME_PARENT="${KIWOOM_DEPLOY_RUNTIME_PARENT:-/run}"
readonly MIN_FREE_DISK_MIB=1536
readonly MIN_AVAILABLE_MEMORY_MIB=256
readonly MAX_CREDENTIAL_BYTES=8192
readonly CHECK_CPUS="0.75"
readonly CHECK_MEMORY_BYTES=536870912
readonly CHECK_PIDS_LIMIT=128
readonly PULL_TIMEOUT_SECONDS="${KIWOOM_DEPLOY_PULL_TIMEOUT_SECONDS:-300}"
readonly CHECK_TIMEOUT_SECONDS="${KIWOOM_DEPLOY_CHECK_TIMEOUT_SECONDS:-120}"
readonly KILL_AFTER_SECONDS="${KIWOOM_DEPLOY_KILL_AFTER_SECONDS:-15}"
readonly DOWNLOAD_TIMEOUT_SECONDS="${KIWOOM_DEPLOY_DOWNLOAD_TIMEOUT_SECONDS:-45}"

ACTIVE_CONTAINER_NAME=""
PLACEHOLDER_DIR=""
CHECK_DATA_DIR=""

fail() {
    printf 'production check failed: %s\n' "$1" >&2
    exit 1
}

validate_image() {
    [[ "$1" =~ ^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$ ]] \
        || fail "image must be the exact public GHCR repository and sha256 digest"
}

validate_source_sha() {
    [[ "$1" =~ ^[0-9a-f]{40}$ ]] \
        || fail "source SHA must be 40 lowercase hex characters"
}

validate_file_sha256() {
    [[ "$1" =~ ^[0-9a-f]{64}$ ]] || fail "file SHA256 must be 64 lowercase hex characters"
}

validate_instance_id() {
    [[ "$1" =~ ^i-[0-9a-f]{17}$ ]] || fail "invalid EC2 instance ID"
}

validate_region() {
    [[ "$1" =~ ^[a-z]{2}(-gov)?-[a-z]+-[0-9]$ ]] || fail "invalid AWS region"
}

validate_promotion_attempt_id() {
    [[ "$1" =~ ^[1-9][0-9]{0,19}$ ]] \
        || fail "promotion attempt ID must be a positive bounded decimal"
}

reject_symlink_components() {
    local path="$1"
    local current="/"
    local component
    IFS='/' read -r -a components <<<"${path#/}"
    for component in "${components[@]}"; do
        [[ -n "${component}" ]] || continue
        current="${current%/}/${component}"
        [[ ! -L "${current}" ]] || fail "credential path must not contain symbolic links"
    done
}

validate_secret_metadata() {
    local path="$1"
    local metadata size
    reject_symlink_components "${path}"
    [[ -f "${path}" && ! -L "${path}" ]] \
        || fail "required credential file metadata is invalid"
    metadata="$(stat -c '%u:%g:%a:%h:%F' -- "${path}")" \
        || fail "required credential file metadata is unavailable"
    size="$(stat -c '%s' -- "${path}")" \
        || fail "required credential file size is unavailable"
    [[ "${metadata}" == "0:0:400:1:regular file" ]] \
        || fail "required credential file must be root:root, mode 0400, one regular link"
    [[ "${size}" =~ ^[0-9]+$ ]] \
        || fail "required credential file size is invalid"
    (( size > 0 && size <= MAX_CREDENTIAL_BYTES )) \
        || fail "required credential file must be between 1 and 8192 bytes"
}

validate_secret_directory() {
    local metadata expected_owner
    reject_symlink_components "${SECRET_DIR}"
    [[ -d "${SECRET_DIR}" && ! -L "${SECRET_DIR}" ]] \
        || fail "credential directory metadata is invalid"
    metadata="$(stat -c '%u:%g:%a:%F' -- "${SECRET_DIR}")" \
        || fail "credential directory metadata is unavailable"
    [[ "${metadata}" == "0:0:700:directory" ]] \
        || fail "credential directory must be root:root and mode 0700"
    validate_secret_metadata "${APP_KEY_FILE}"
    validate_secret_metadata "${SECRET_KEY_FILE}"
}

validate_resources() {
    local docker_root available_disk available_memory
    docker_root="$(docker info --format '{{.DockerRootDir}}')" \
        || fail "Docker daemon is unavailable"
    [[ -n "${docker_root}" && -d "${docker_root}" ]] \
        || fail "Docker root directory is unavailable"
    available_disk="$(df -Pm "${docker_root}" | awk 'NR == 2 {print $4}')"
    available_memory="$(awk '/^MemAvailable:/ {print int($2 / 1024)}' /proc/meminfo)"
    [[ "${available_disk}" =~ ^[0-9]+$ ]] || fail "free disk measurement failed"
    [[ "${available_memory}" =~ ^[0-9]+$ ]] || fail "available memory measurement failed"
    (( available_disk >= MIN_FREE_DISK_MIB )) \
        || fail "free disk is below the 1536 MiB deployment floor"
    (( available_memory >= MIN_AVAILABLE_MEMORY_MIB )) \
        || fail "available memory is below the 256 MiB deployment floor"
    printf 'resource preflight: disk_mib=%s memory_mib=%s\n' \
        "${available_disk}" "${available_memory}"
}

validate_instance_identity() {
    local expected_instance="$1"
    local expected_region="$2"
    local token document actual_instance actual_region
    token="$(curl --fail --silent --show-error --max-time 5 -X PUT \
        -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
        http://169.254.169.254/latest/api/token)" \
        || fail "IMDSv2 token request failed"
    document="$(curl --fail --silent --show-error --max-time 5 \
        -H "X-aws-ec2-metadata-token: ${token}" \
        http://169.254.169.254/latest/dynamic/instance-identity/document)" \
        || fail "instance identity document request failed"
    read -r actual_instance actual_region < <(
        python3 -c \
            'import json,sys; d=json.load(sys.stdin); print(d.get("instanceId",""), d.get("region",""))' \
            <<<"${document}"
    )
    unset token document
    [[ "${actual_instance}" == "${expected_instance}" ]] \
        || fail "instance identity does not match the approved target"
    [[ "${actual_region}" == "${expected_region}" ]] \
        || fail "instance region does not match the approved target"
}

acquire_deployment_lock() {
    exec 9>"${LOCK_FILE}"
    flock -n 9 || fail "another production check holds the deployment lock"
}

validate_private_state_directories() {
    local directory metadata expected_owner
    expected_owner="$(id -u):$(id -g)"
    for directory in "${STATE_DIR}" "${ATTEMPT_DIR}"; do
        [[ -d "${directory}" && ! -L "${directory}" ]] \
            || fail "promotion state directory is invalid"
        metadata="$(stat -c '%u:%g:%a:%F' -- "${directory}")" \
            || fail "promotion state directory metadata is unavailable"
        [[ "${metadata}" == "${expected_owner}:700:directory" ]] \
            || fail "promotion state directory must be private and root-owned"
    done
}

reconcile_promotion_attempt() {
    local attempt_id="$1"
    local image="$2"
    local source_sha="$3"
    local common_hash="$4"
    local prod_hash="$5"
    local marker="${ATTEMPT_DIR}/${attempt_id}.json"
    [[ -e "${marker}" ]] || return 1
    [[ -f "${marker}" && ! -L "${marker}" ]] \
        || fail "promotion attempt marker is not a regular file"
    local metadata
    metadata="$(stat -c '%u:%g:%a:%F' -- "${marker}")" \
        || fail "promotion attempt marker metadata is unavailable"
    expected_owner="$(id -u):$(id -g)"
    [[ "${metadata}" == "${expected_owner}:600:regular file" ]] \
        || fail "promotion attempt marker must be private and root-owned"
    if ! python3 - "${marker}" "${attempt_id}" "${image}" "${source_sha}" \
        "${common_hash}" "${prod_hash}" <<'PY'
import json
from pathlib import Path
import sys

path, attempt_id, image, source_sha, common_hash, prod_hash = sys.argv[1:]
marker = json.loads(Path(path).read_text(encoding="utf-8"))
expected_marker = (
    "production check passed: "
    f"source_sha={source_sha} image={image} rollback=false"
)
expected = {
    "schema": 1,
    "promotion_attempt_id": attempt_id,
    "source_sha": source_sha,
    "image_digest": image,
    "compose_sha256": common_hash,
    "compose_prod_sha256": prod_hash,
    "success_marker": expected_marker,
}
if marker != expected:
    raise SystemExit("marker mismatch")
print(expected_marker)
PY
    then
        fail "promotion attempt marker tuple mismatch"
    fi
}

record_promotion_attempt_success() {
    local attempt_id="$1"
    local image="$2"
    local source_sha="$3"
    local common_hash="$4"
    local prod_hash="$5"
    local marker="${ATTEMPT_DIR}/${attempt_id}.json"
    local temporary="${ATTEMPT_DIR}/.${attempt_id}.tmp.$$"
    [[ ! -e "${marker}" ]] || fail "promotion attempt marker already exists"
    python3 - "${marker}" "${temporary}" "${attempt_id}" "${image}" \
        "${source_sha}" "${common_hash}" "${prod_hash}" <<'PY'
import json
import os
from pathlib import Path
import sys

path_value, temporary, attempt_id, image, source_sha, common_hash, prod_hash = (
    sys.argv[1:]
)
success_marker = (
    "production check passed: "
    f"source_sha={source_sha} image={image} rollback=false"
)
payload = {
    "schema": 1,
    "promotion_attempt_id": attempt_id,
    "source_sha": source_sha,
    "image_digest": image,
    "compose_sha256": common_hash,
    "compose_prod_sha256": prod_hash,
    "success_marker": success_marker,
}
encoded = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(encoded)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, path_value)
directory_fd = os.open(str(Path(path_value).parent), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

download_compose_contract() {
    local source_sha="$1"
    local common_hash="$2"
    local prod_hash="$3"
    local destination="$4"
    local filename expected temporary actual
    [[ ! -L "${destination}" ]] || fail "Compose source directory must not be a symlink"
    install -d -m 0700 -- "${destination}"
    for filename in compose.yaml compose.prod.yaml; do
        if [[ "${filename}" == "compose.yaml" ]]; then
            expected="${common_hash}"
        else
            expected="${prod_hash}"
        fi
        temporary="${destination}/.${filename}.tmp.$$"
        if ! timeout "${DOWNLOAD_TIMEOUT_SECONDS}" \
            curl --fail --silent --show-error --location \
            --proto '=https' --tlsv1.2 \
            "https://raw.githubusercontent.com/${EXPECTED_REPOSITORY}/${source_sha}/${filename}" \
            --output "${temporary}"; then
            rm -f -- "${temporary}"
            fail "exact-SHA Compose contract download failed"
        fi
        actual="$(sha256sum "${temporary}" | cut -d' ' -f1)"
        if [[ "${actual}" != "${expected}" ]]; then
            rm -f -- "${temporary}"
            fail "Compose contract hash mismatch"
        fi
        chmod 0600 "${temporary}"
        mv -f -- "${temporary}" "${destination}/${filename}"
    done
}

verify_stored_compose() {
    local directory="$1"
    local common_hash="$2"
    local prod_hash="$3"
    local actual
    [[ -d "${directory}" && ! -L "${directory}" ]] \
        || fail "recorded Compose directory is unavailable"
    [[ ! -L "${directory}/compose.yaml" && ! -L "${directory}/compose.prod.yaml" ]] \
        || fail "recorded Compose files must not be symbolic links"
    actual="$(sha256sum "${directory}/compose.yaml" | cut -d' ' -f1)" \
        || fail "recorded common Compose is unavailable"
    [[ "${actual}" == "${common_hash}" ]] || fail "recorded common Compose hash mismatch"
    actual="$(sha256sum "${directory}/compose.prod.yaml" | cut -d' ' -f1)" \
        || fail "recorded production Compose is unavailable"
    [[ "${actual}" == "${prod_hash}" ]] || fail "recorded production Compose hash mismatch"
}

validate_image_revision() {
    local image="$1"
    local source_sha="$2"
    local revision entrypoint image_user
    revision="$(docker image inspect "${image}" \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" \
        || fail "pulled image metadata is unavailable"
    [[ "${revision}" == "${source_sha}" ]] \
        || fail "pulled image revision does not match source SHA"
    entrypoint="$(docker image inspect "${image}" \
        --format '{{json .Config.Entrypoint}}')" \
        || fail "pulled image entrypoint metadata is unavailable"
    [[ "${entrypoint}" == \
        '["python","/usr/local/bin/kiwoom-runtime-entrypoint.py"]' ]] \
        || fail "pulled image entrypoint does not match the runtime contract"
    image_user="$(docker image inspect "${image}" --format '{{.Config.User}}')" \
        || fail "pulled image user metadata is unavailable"
    [[ "${image_user}" == "10001:10001" ]] \
        || fail "pulled image user does not match the runtime contract"
}

create_placeholder_boundaries() {
    PLACEHOLDER_DIR="$(mktemp -d "${RUNTIME_PARENT}/kiwoom-check-secrets.XXXXXX")"
    CHECK_DATA_DIR="$(mktemp -d "${RUNTIME_PARENT}/kiwoom-check-data.XXXXXX")"
    chmod 0700 "${PLACEHOLDER_DIR}"
    chown 10001:10001 "${CHECK_DATA_DIR}"
    chmod 0500 "${CHECK_DATA_DIR}"
    printf '%s\n' 'CHECK_ONLY_NON_SECRET_APP_KEY' >"${PLACEHOLDER_DIR}/app-key"
    printf '%s\n' 'CHECK_ONLY_NON_SECRET_SECRET_KEY' >"${PLACEHOLDER_DIR}/secret-key"
    chmod 0400 "${PLACEHOLDER_DIR}/app-key" "${PLACEHOLDER_DIR}/secret-key"
}

remove_check_container() {
    local name="${ACTIVE_CONTAINER_NAME}"
    [[ -n "${name}" ]] || return 0
    docker rm -f "${name}" >/dev/null 2>&1 || true
    if docker container inspect "${name}" >/dev/null 2>&1; then
        printf 'production check cleanup failed: exact container remains\n' >&2
        return 1
    fi
    docker info >/dev/null 2>&1 \
        || { printf 'production check cleanup could not verify Docker\n' >&2; return 1; }
    ACTIVE_CONTAINER_NAME=""
}

remove_placeholder_boundaries() {
    if [[ -n "${PLACEHOLDER_DIR}" ]]; then
        chmod 0600 "${PLACEHOLDER_DIR}/app-key" \
            "${PLACEHOLDER_DIR}/secret-key" 2>/dev/null || true
        rm -f -- "${PLACEHOLDER_DIR}/app-key" "${PLACEHOLDER_DIR}/secret-key"
        rmdir -- "${PLACEHOLDER_DIR}" 2>/dev/null || true
        [[ ! -e "${PLACEHOLDER_DIR}" ]] \
            || { printf 'placeholder secret cleanup failed\n' >&2; return 1; }
        PLACEHOLDER_DIR=""
    fi
    if [[ -n "${CHECK_DATA_DIR}" ]]; then
        rmdir -- "${CHECK_DATA_DIR}" 2>/dev/null || true
        [[ ! -e "${CHECK_DATA_DIR}" ]] \
            || { printf 'check data cleanup failed\n' >&2; return 1; }
        CHECK_DATA_DIR=""
    fi
}

cleanup_runtime() {
    local status=0
    set +e
    remove_check_container || status=1
    remove_placeholder_boundaries || status=1
    set -e
    return "${status}"
}

run_container_check() {
    local image="$1"
    local source_sha="$2"
    local digest_hex="${image##*:}"
    ACTIVE_CONTAINER_NAME="kiwoom-check-${source_sha:0:12}-${digest_hex:0:12}"
    trap 'cleanup_runtime' EXIT
    trap 'cleanup_runtime' ERR
    trap 'cleanup_runtime; exit 143' TERM
    create_placeholder_boundaries

    timeout "${PULL_TIMEOUT_SECONDS}" docker pull "${image}" \
        || fail "immutable image pull failed or timed out"
    validate_image_revision "${image}" "${source_sha}"
    timeout --signal=TERM --kill-after="${KILL_AFTER_SECONDS}" \
        "${CHECK_TIMEOUT_SECONDS}" \
        docker run --rm --pull never \
        --name "${ACTIVE_CONTAINER_NAME}" \
        --label io.kiwoom-stock.project=kiwoom-stock \
        --label io.kiwoom-stock.lifecycle=production-check-only \
        --init \
        --no-healthcheck \
        --user 0:0 \
        --network none \
        --read-only \
        --cpus "${CHECK_CPUS}" \
        --memory "${CHECK_MEMORY_BYTES}" \
        --memory-swap "${CHECK_MEMORY_BYTES}" \
        --pids-limit "${CHECK_PIDS_LIMIT}" \
        --cap-drop ALL \
        --cap-add CHOWN \
        --cap-add SETGID \
        --cap-add SETUID \
        --security-opt no-new-privileges:true \
        --tmpfs /tmp:rw,nosuid,nodev,noexec,mode=1777 \
        --tmpfs /run/secrets:rw,nosuid,nodev,noexec,mode=0700 \
        --tmpfs /run/kiwoom-secrets:rw,nosuid,nodev,noexec,mode=0700 \
        --mount \
        "type=bind,source=${CHECK_DATA_DIR},target=/var/lib/kiwoom,readonly" \
        --mount \
        "type=bind,source=${PLACEHOLDER_DIR}/app-key,target=/run/secrets/KIWOOM_APP_KEY,readonly" \
        --mount \
        "type=bind,source=${PLACEHOLDER_DIR}/secret-key,target=/run/secrets/KIWOOM_SECRET_KEY,readonly" \
        --env KIWOOM_API_MODE=prod \
        --env KIWOOM_APP_ENV=prod \
        --env KIWOOM_PROCESS_NAME=paper-monitor \
        --env KIWOOM_CREDENTIALS_DIR=/run/secrets \
        --env KIWOOM_OUTPUT_DIR=/var/lib/kiwoom/output \
        --env KIWOOM_DB_PATH=/var/lib/kiwoom/trades.db \
        "${image}" \
        python -m kiwoom_stock --check-config \
        || fail "one-shot configuration check failed or timed out"
    cleanup_runtime || fail "one-shot cleanup failed"
    trap - EXIT ERR TERM
}

validate_release_values() {
    validate_image "$1"
    validate_source_sha "$2"
    validate_file_sha256 "$3"
    validate_file_sha256 "$4"
}

record_success() {
    local image="$1"
    local source_sha="$2"
    local common_hash="$3"
    local prod_hash="$4"
    local state="${STATE_DIR}/release-state.json"
    local temporary="${state}.tmp.$$"
    python3 - "${state}" "${temporary}" "${image}" "${source_sha}" \
        "${common_hash}" "${prod_hash}" <<'PY'
import json
import os
from pathlib import Path
import re
import sys

state_path, temporary, image, source_sha, common_hash, prod_hash = sys.argv[1:]
digest_pattern = re.compile(
    r"^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$"
)
hex40 = re.compile(r"^[0-9a-f]{40}$")
hex64 = re.compile(r"^[0-9a-f]{64}$")

def validate_release(value):
    if not isinstance(value, dict):
        raise SystemExit("invalid release record")
    expected = {
        "source_sha",
        "image_digest",
        "image_revision",
        "compose_sha256",
    }
    if set(value) != expected:
        raise SystemExit("invalid release record fields")
    hashes = value["compose_sha256"]
    if (
        not digest_pattern.fullmatch(value["image_digest"])
        or not hex40.fullmatch(value["source_sha"])
        or value["image_revision"] != value["source_sha"]
        or not isinstance(hashes, dict)
        or set(hashes) != {"compose.yaml", "compose.prod.yaml"}
        or not all(hex64.fullmatch(item) for item in hashes.values())
    ):
        raise SystemExit("invalid release record values")

current = {
    "source_sha": source_sha,
    "image_digest": image,
    "image_revision": source_sha,
    "compose_sha256": {
        "compose.yaml": common_hash,
        "compose.prod.yaml": prod_hash,
    },
}
validate_release(current)
previous = None
path = Path(state_path)
if path.exists():
    old = json.loads(path.read_text(encoding="utf-8"))
    if set(old) != {"schema", "current", "previous"} or old["schema"] != 1:
        raise SystemExit("invalid release state")
    validate_release(old["current"])
    if old["previous"] is not None:
        validate_release(old["previous"])
    if old["current"] != current:
        previous = old["current"]
    else:
        previous = old["previous"]
new_state = {"schema": 1, "current": current, "previous": previous}
encoded = (json.dumps(new_state, sort_keys=True, indent=2) + "\n").encode()
fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "wb") as stream:
    stream.write(encoded)
    stream.flush()
    os.fsync(stream.fileno())
os.replace(temporary, state_path)
directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory_fd)
finally:
    os.close(directory_fd)
PY
}

load_previous_release() {
    local state="${STATE_DIR}/release-state.json"
    [[ -f "${state}" && ! -L "${state}" ]] || fail "no release state is recorded"
    python3 - "${state}" <<'PY'
import json
from pathlib import Path
import re
import sys

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
release = state.get("previous")
digest = re.compile(
    r"^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$"
)
hex40 = re.compile(r"^[0-9a-f]{40}$")
hex64 = re.compile(r"^[0-9a-f]{64}$")
if not isinstance(release, dict):
    raise SystemExit("no previous release is recorded")
hashes = release.get("compose_sha256")
values = (
    release.get("image_digest"),
    release.get("source_sha"),
    release.get("image_revision"),
    hashes.get("compose.yaml") if isinstance(hashes, dict) else None,
    hashes.get("compose.prod.yaml") if isinstance(hashes, dict) else None,
)
if (
    not isinstance(values[0], str)
    or not digest.fullmatch(values[0])
    or not isinstance(values[1], str)
    or not hex40.fullmatch(values[1])
    or values[2] != values[1]
    or not isinstance(values[3], str)
    or not hex64.fullmatch(values[3])
    or not isinstance(values[4], str)
    or not hex64.fullmatch(values[4])
):
    raise SystemExit("invalid previous release")
print("\n".join((values[0], values[1], values[3], values[4])))
PY
}

cleanup_project_artifacts() {
    local active_image="$1"
    local active_id candidate protected_id recorded_image
    local -a protected_ids=()
    active_id="$(docker image inspect "${active_image}" --format '{{.Id}}')" \
        || fail "active image disappeared before state recording"
    protected_ids+=("${active_id}")
    if [[ -f "${STATE_DIR}/release-state.json" ]]; then
        while IFS= read -r recorded_image; do
            [[ -n "${recorded_image}" ]] || continue
            validate_image "${recorded_image}"
            if protected_id="$(
                docker image inspect "${recorded_image}" --format '{{.Id}}' 2>/dev/null
            )"; then
                protected_ids+=("${protected_id}")
            fi
        done < <(
            python3 - "${STATE_DIR}/release-state.json" <<'PY'
import json
from pathlib import Path
import sys

state = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
for key in ("current", "previous"):
    value = state.get(key)
    if isinstance(value, dict) and isinstance(value.get("image_digest"), str):
        print(value["image_digest"])
PY
        )
    fi
    docker container prune --force \
        --filter 'label=io.kiwoom-stock.lifecycle=production-check-only' \
        >/dev/null
    while IFS= read -r candidate; do
        [[ -n "${candidate}" ]] || continue
        for protected_id in "${protected_ids[@]}"; do
            [[ "${candidate}" != "${protected_id}" ]] || continue 2
        done
        docker image rm "${candidate}" >/dev/null \
            || fail "project-owned dangling image cleanup failed"
    done < <(
        docker image ls --quiet --no-trunc \
            --filter 'dangling=true' \
            --filter 'label=io.kiwoom-stock.project=kiwoom-stock' \
            | sort -u
    )
}

usage() {
    cat >&2 <<'EOF'
usage:
  kiwoom-production-check --image DIGEST --source-sha SHA \
    --promotion-attempt-id POSITIVE_DECIMAL \
    --compose-sha256 HASH --compose-prod-sha256 HASH \
    --expected-instance-id INSTANCE --region REGION
  kiwoom-production-check --rollback-check \
    --expected-instance-id INSTANCE --region REGION
EOF
}

main() {
    local image=""
    local source_sha=""
    local promotion_attempt_id=""
    local common_hash=""
    local prod_hash=""
    local expected_instance=""
    local region=""
    local rollback=false
    while (( $# )); do
        case "$1" in
            --image) image="${2:-}"; shift 2 ;;
            --source-sha) source_sha="${2:-}"; shift 2 ;;
            --promotion-attempt-id) promotion_attempt_id="${2:-}"; shift 2 ;;
            --compose-sha256) common_hash="${2:-}"; shift 2 ;;
            --compose-prod-sha256) prod_hash="${2:-}"; shift 2 ;;
            --expected-instance-id) expected_instance="${2:-}"; shift 2 ;;
            --region) region="${2:-}"; shift 2 ;;
            --rollback-check) rollback=true; shift ;;
            *) usage; fail "unsupported argument" ;;
        esac
    done

    (( EUID == 0 )) || fail "this command must run as root"
    validate_instance_id "${expected_instance}"
    validate_region "${region}"
    if [[ "${rollback}" == true ]]; then
        [[ -z "${image}${source_sha}${promotion_attempt_id}${common_hash}${prod_hash}" ]] \
            || fail "rollback-check does not accept release arguments"
    else
        validate_release_values "${image}" "${source_sha}" "${common_hash}" "${prod_hash}"
        validate_promotion_attempt_id "${promotion_attempt_id}"
    fi
    command -v flock >/dev/null || fail "flock is unavailable"

    install -d -m 0700 -- "${STATE_DIR}" "${SOURCE_DIR}" "${ATTEMPT_DIR}"
    validate_private_state_directories
    acquire_deployment_lock
    if [[ "${rollback}" == false ]]; then
        local reconciled_marker=""
        if [[ -e "${ATTEMPT_DIR}/${promotion_attempt_id}.json" ]]; then
            reconciled_marker="$(
                reconcile_promotion_attempt "${promotion_attempt_id}" "${image}" \
                    "${source_sha}" "${common_hash}" "${prod_hash}"
            )" || fail "promotion attempt reconciliation failed"
            printf '%s\n' "${reconciled_marker}"
            return 0
        fi
    fi
    command -v docker >/dev/null || fail "Docker is unavailable"
    command -v curl >/dev/null || fail "curl is unavailable"
    validate_instance_identity "${expected_instance}" "${region}"
    validate_secret_directory
    validate_resources

    if [[ "${rollback}" == true ]]; then
        local -a previous
        mapfile -t previous < <(load_previous_release)
        (( ${#previous[@]} == 4 )) || fail "previous release record is incomplete"
        image="${previous[0]}"
        source_sha="${previous[1]}"
        common_hash="${previous[2]}"
        prod_hash="${previous[3]}"
        validate_release_values "${image}" "${source_sha}" "${common_hash}" "${prod_hash}"
        local rollback_directory="${SOURCE_DIR}/${source_sha}"
        verify_stored_compose "${rollback_directory}" "${common_hash}" "${prod_hash}"
        run_container_check "${image}" "${source_sha}"
    else
        local compose_directory="${SOURCE_DIR}/${source_sha}"
        download_compose_contract \
            "${source_sha}" "${common_hash}" "${prod_hash}" "${compose_directory}"
        run_container_check "${image}" "${source_sha}"
    fi
    cleanup_project_artifacts "${image}"
    if [[ "${rollback}" == false ]]; then
        record_success "${image}" "${source_sha}" "${common_hash}" "${prod_hash}"
        record_promotion_attempt_success \
            "${promotion_attempt_id}" "${image}" "${source_sha}" \
            "${common_hash}" "${prod_hash}"
    fi
    printf 'production check passed: source_sha=%s image=%s rollback=%s\n' \
        "${source_sha}" "${image}" "${rollback}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
