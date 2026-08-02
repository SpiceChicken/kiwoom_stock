#!/usr/bin/env bash
#
# Root-owned EC2 command for one immutable, market-read-only shadow cycle.
# It never enables account, order, revoke, Slack, S3, or Gemini capabilities.

set -Eeuo pipefail

readonly EXPECTED_REPOSITORY="SpiceChicken/kiwoom_stock"
readonly IMAGE_PREFIX="ghcr.io/spicechicken/kiwoom_stock@sha256:"
readonly EXPECTED_INSTANCE_ID="i-02cb0a404794bd43a"
readonly EXPECTED_REGION="ap-northeast-2"
readonly SHADOW_COMPOSE_NAME="compose.shadow.yaml"
readonly STATE_DIR="${KIWOOM_SHADOW_STATE_DIR:-/opt/kiwoom-stock/shadow}"
readonly LOCK_FILE="${KIWOOM_SHADOW_LOCK_FILE:-/run/lock/kiwoom-stock-shadow.lock}"
readonly SECRET_DIR="${KIWOOM_DEPLOY_SECRET_DIR:-/run/kiwoom-stock/credentials}"
readonly APP_KEY_FILE="${SECRET_DIR}/app-key"
readonly SECRET_KEY_FILE="${SECRET_DIR}/secret-key"
readonly RUNTIME_PARENT="${KIWOOM_SHADOW_RUNTIME_PARENT:-/run}"
readonly MAX_CREDENTIAL_BYTES=8192
readonly PULL_TIMEOUT_SECONDS="${KIWOOM_SHADOW_PULL_TIMEOUT_SECONDS:-300}"
readonly RUN_TIMEOUT_SECONDS="${KIWOOM_SHADOW_RUN_TIMEOUT_SECONDS:-900}"
readonly KILL_AFTER_SECONDS="${KIWOOM_SHADOW_KILL_AFTER_SECONDS:-15}"
readonly DOWNLOAD_TIMEOUT_SECONDS="${KIWOOM_SHADOW_DOWNLOAD_TIMEOUT_SECONDS:-45}"
readonly CONTAINER_NAME="kiwoom-shadow-once"

ACTIVE_CONTAINER_NAME=""
WORK_DIR=""

fail() {
    printf 'shadow worker failed: %s\n' "$1" >&2
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

validate_hash() {
    [[ "$1" =~ ^[0-9a-f]{64}$ ]] || fail "Compose hash must be 64 lowercase hex characters"
}

validate_activation_id() {
    [[ "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] \
        || fail "activation ID has an invalid bounded format"
}

reject_symlink_components() {
    local path="$1"
    local current="/"
    local component
    IFS='/' read -r -a components <<<"${path#/}"
    for component in "${components[@]}"; do
        [[ -n "${component}" ]] || continue
        current="${current%/}/${component}"
        [[ ! -L "${current}" ]] || fail "path must not contain symbolic links"
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
        || fail "credential file must be root:root, mode 0400, one regular link"
    [[ "${size}" =~ ^[0-9]+$ ]] || fail "credential file size is invalid"
    (( size > 0 && size <= MAX_CREDENTIAL_BYTES )) \
        || fail "credential file must be between 1 and 8192 bytes"
}

validate_secret_directory() {
    local metadata
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

validate_instance_identity() {
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
    [[ "${actual_instance}" == "${EXPECTED_INSTANCE_ID}" ]] \
        || fail "instance identity does not match the approved target"
    [[ "${actual_region}" == "${EXPECTED_REGION}" ]] \
        || fail "instance region does not match the approved target"
}

validate_compose_contract() {
    local compose_file="$1"
    local source_sha="$2"
    local compose_hash="$3"
    local actual
    [[ -f "${compose_file}" && ! -L "${compose_file}" ]] \
        || fail "shadow Compose contract is unavailable"
    actual="$(sha256sum "${compose_file}" | cut -d' ' -f1)"
    [[ "${actual}" == "${compose_hash}" ]] || fail "shadow Compose hash mismatch"
    grep -Fq 'KIWOOM_EXECUTION_MODE: shadow-once' "${compose_file}" \
        || fail "shadow Compose execution mode contract is missing"
    grep -Fq 'restart: "no"' "${compose_file}" \
        || fail "shadow Compose restart contract is missing"
    grep -Fq 'container_name: kiwoom-shadow-once' "${compose_file}" \
        || fail "shadow Compose container identity is missing"
    grep -Fq -- '--source-sha' "${compose_file}" \
        || fail "shadow Compose source tuple is missing"
    grep -Fq -- '--image-digest' "${compose_file}" \
        || fail "shadow Compose image tuple is missing"
}

download_compose() {
    local source_sha="$1"
    local expected_hash="$2"
    local destination="$3"
    local temporary actual
    install -d -m 0700 -- "${destination}"
    temporary="${destination}/.${SHADOW_COMPOSE_NAME}.tmp.$$"
    if ! timeout "${DOWNLOAD_TIMEOUT_SECONDS}" curl --fail --silent --show-error \
        --location --proto '=https' --proto-redir '=https' \
        "https://raw.githubusercontent.com/${EXPECTED_REPOSITORY}/${source_sha}/${SHADOW_COMPOSE_NAME}" \
        --output "${temporary}"; then
        rm -f -- "${temporary}"
        fail "exact-SHA shadow Compose download failed"
    fi
    actual="$(sha256sum "${temporary}" | cut -d' ' -f1)"
    [[ "${actual}" == "${expected_hash}" ]] || {
        rm -f -- "${temporary}"
        fail "downloaded shadow Compose hash mismatch"
    }
    chmod 0600 "${temporary}"
    mv -f -- "${temporary}" "${destination}/${SHADOW_COMPOSE_NAME}"
}

validate_image_revision() {
    local image="$1"
    local source_sha="$2"
    local revision entrypoint image_user
    revision="$(docker image inspect "${image}" \
        --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" \
        || fail "image revision metadata is unavailable"
    [[ "${revision}" == "${source_sha}" ]] || fail "image revision does not match source SHA"
    entrypoint="$(docker image inspect "${image}" --format '{{json .Config.Entrypoint}}')" \
        || fail "image entrypoint metadata is unavailable"
    [[ "${entrypoint}" == '["python","/usr/local/bin/kiwoom-runtime-entrypoint.py"]' ]] \
        || fail "image entrypoint does not match the runtime contract"
    image_user="$(docker image inspect "${image}" --format '{{.Config.User}}')" \
        || fail "image user metadata is unavailable"
    [[ "${image_user}" == "10001:10001" ]] || fail "image user does not match the runtime contract"
}

cleanup_container() {
    local status=0
    set +e
    if [[ -n "${ACTIVE_CONTAINER_NAME}" ]]; then
        docker rm -f "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1 || true
        docker container inspect "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1 && status=1
    fi
    set -e
    return "${status}"
}

cleanup_work_dir() {
    if [[ -n "${WORK_DIR}" ]]; then
        rm -f -- "${WORK_DIR}/${SHADOW_COMPOSE_NAME}" 2>/dev/null || true
        rmdir -- "${WORK_DIR}" 2>/dev/null || true
        [[ ! -e "${WORK_DIR}" ]] || return 1
        WORK_DIR=""
    fi
}

cleanup() {
    local status=0
    cleanup_container || status=1
    cleanup_work_dir || status=1
    return "${status}"
}

run_shadow_once() {
    local image="$1"
    local source_sha="$2"
    local activation_id="$3"
    local compose_hash="$4"
    local compose_file
    WORK_DIR="$(mktemp -d "${RUNTIME_PARENT}/kiwoom-shadow.XXXXXX")"
    compose_file="${WORK_DIR}/${SHADOW_COMPOSE_NAME}"
    download_compose "${source_sha}" "${compose_hash}" "${WORK_DIR}"
    validate_compose_contract "${compose_file}" "${source_sha}" "${compose_hash}"
    ACTIVE_CONTAINER_NAME="${CONTAINER_NAME}"
    if docker container inspect "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1; then
        fail "shadow container already exists; use --stop before a new activation"
    fi
    trap 'cleanup' EXIT
    trap 'cleanup; exit 143' TERM
    timeout "${PULL_TIMEOUT_SECONDS}" docker pull "${image}" \
        || fail "immutable shadow image pull failed or timed out"
    validate_image_revision "${image}" "${source_sha}"
    KIWOOM_IMAGE="${image}" \
    KIWOOM_IMAGE_DIGEST="${image}" \
    KIWOOM_SOURCE_SHA="${source_sha}" \
    KIWOOM_ACTIVATION_ID="${activation_id}" \
    KIWOOM_SHADOW_APP_KEY_FILE="${APP_KEY_FILE}" \
    KIWOOM_SHADOW_SECRET_KEY_FILE="${SECRET_KEY_FILE}" \
        timeout --signal=TERM --kill-after="${KILL_AFTER_SECONDS}" \
        "${RUN_TIMEOUT_SECONDS}" docker compose \
        --project-name kiwoom-stock-shadow \
        --project-directory "${WORK_DIR}" \
        --file "${compose_file}" up --abort-on-container-exit --exit-code-from app \
        || fail "shadow-once container failed or timed out"
    cleanup || fail "shadow container cleanup failed"
    trap - EXIT TERM
    printf 'shadow-once passed: source_sha=%s image=%s activation_id=%s side_effects=none\n' \
        "${source_sha}" "${image}" "${activation_id}"
}

usage() {
    cat >&2 <<'EOF'
usage:
  kiwoom-shadow-worker --image DIGEST --source-sha SHA --activation-id ID \
    --compose-shadow-sha256 HASH --expected-instance-id INSTANCE --region REGION
  kiwoom-shadow-worker --stop --expected-instance-id INSTANCE --region REGION
EOF
}

main() {
    local image="" source_sha="" activation_id="" compose_hash=""
    local expected_instance="" region="" stop=false
    while (( $# )); do
        case "$1" in
            --image) image="${2:-}"; shift 2 ;;
            --source-sha) source_sha="${2:-}"; shift 2 ;;
            --activation-id) activation_id="${2:-}"; shift 2 ;;
            --compose-shadow-sha256) compose_hash="${2:-}"; shift 2 ;;
            --expected-instance-id) expected_instance="${2:-}"; shift 2 ;;
            --region) region="${2:-}"; shift 2 ;;
            --stop) stop=true; shift ;;
            *) usage; fail "unsupported argument" ;;
        esac
    done
    (( EUID == 0 )) || fail "this command must run as root"
    [[ "${expected_instance}" == "${EXPECTED_INSTANCE_ID}" ]] \
        || fail "expected instance ID is not approved"
    [[ "${region}" == "${EXPECTED_REGION}" ]] || fail "region is not approved"
    command -v flock >/dev/null || fail "flock is unavailable"
    command -v docker >/dev/null || fail "Docker is unavailable"
    exec 9>"${LOCK_FILE}"
    flock -n 9 || fail "another shadow activation owns the lock"
    validate_instance_identity
    if [[ "${stop}" == true ]]; then
        [[ -z "${image}${source_sha}${activation_id}${compose_hash}" ]] \
            || fail "stop does not accept release arguments"
        docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
        docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1 \
            && fail "shadow container remains after stop"
        printf 'shadow-once stopped: side_effects=none volume=preserved\n'
        return 0
    fi
    validate_image "${image}"
    validate_source_sha "${source_sha}"
    validate_activation_id "${activation_id}"
    validate_hash "${compose_hash}"
    validate_secret_directory
    run_shadow_once "${image}" "${source_sha}" "${activation_id}" "${compose_hash}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
