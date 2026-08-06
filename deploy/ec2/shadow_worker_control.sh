#!/usr/bin/env bash
#
# Root-owned EC2 command for bounded immutable market-read-only shadow execution.
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
readonly FIRST_TICK_TIMEOUT_SECONDS="${KIWOOM_SHADOW_FIRST_TICK_TIMEOUT_SECONDS:-240}"
readonly CONTAINER_NAME="kiwoom-shadow-once"
readonly SHADOW_EVIDENCE_SCHEMA_VERSION=1

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
    grep -Fq 'KIWOOM_EXECUTION_MODE: ${KIWOOM_SHADOW_EXECUTION_MODE:-shadow-once}' "${compose_file}" \
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
        if docker container inspect "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1; then
            docker stop --time 30 "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1 || status=1
            docker rm "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1 || status=1
        fi
        docker container inspect "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1 && status=1
    fi
    set -e
    return "${status}"
}

validate_safe_evidence() {
    local expected_mode="$1"
    local expected_event="$2"
    local expected_source_sha="$3"
    local expected_image="$4"
    local expected_activation_id="$5"
    python3 -c '
import json, sys
mode, event, source_sha, image, activation_id = sys.argv[1:]
records = []
for raw in sys.stdin:
    line = raw.strip()
    if not line.startswith("{"):
        continue
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    if item.get("mode") == mode and item.get("event") == event:
        records.append(item)
if not records:
    raise SystemExit("safe shadow evidence was not found")
item = records[-1]
if (
    item.get("source_sha") != source_sha
    or item.get("image_digest") != image
    or item.get("activation_id") != activation_id
):
    raise SystemExit("shadow evidence activation tuple mismatch")
side = item.get("side_effects")
required = ("broker_orders", "account", "oauth_revoke", "slack", "gemini", "s3", "reports")
if not isinstance(side, dict) or any(side.get(name) is not False for name in required):
    raise SystemExit("shadow evidence side effects are unsafe")
if item.get("resources_closed") is not True:
    raise SystemExit("shadow evidence does not prove resource closure")
if event == "cycle":
    attempts = item.get("http_attempts")
    counts = item.get("api_counts")
    local_counts = item.get("local_counts")
    expected_api = {"token": 1, "stock_basic": 1, "stock_chart_5m": 1, "proxy_chart_60m": 1, "stock_strength": 1, "stock_orderbook": 1}
    expected_local_keys = {"status", "paper_buy", "paper_sell", "error", "critical"}
    integer_fields = (item.get("schema_version"), item.get("cycle_index"), item.get("cycles"), attempts)
    local_valid = (
        isinstance(local_counts, dict)
        and set(local_counts) == expected_local_keys
        and all(type(value) is int for value in local_counts.values())
        and local_counts["status"] == 1
        and local_counts["error"] == 0
        and local_counts["critical"] == 0
        and local_counts["paper_buy"] in (0, 1)
        and local_counts["paper_sell"] in (0, 1)
        and local_counts["paper_buy"] + local_counts["paper_sell"] <= 1
    )
    if (
        any(type(value) is not int for value in integer_fields)
        or item.get("schema_version") != 1
        or item.get("status") != "PASS"
        or item.get("cycle_index") != 1
        or item.get("cycles") != 1
        or attempts != 6
        or item.get("db_identity") != "/var/lib/kiwoom/shadow-trades.db"
        or not isinstance(counts, dict)
        or set(counts) != set(expected_api)
        or any(type(value) is not int for value in counts.values())
        or counts != expected_api
        or not local_valid
        or item.get("interval_seconds") != 60.0
    ):
        raise SystemExit("first continuous tick is invalid")
elif event == "terminal":
    if (item.get("status"), item.get("reason")) not in {
        ("STOPPED", "stop-requested"),
        ("DEADLINE", "run-deadline"),
    }:
        raise SystemExit("continuous stop terminal evidence is invalid")
print(json.dumps(item, sort_keys=True, separators=(",", ":")))
' "${expected_mode}" "${expected_event}" "${expected_source_sha}" "${expected_image}" "${expected_activation_id}"
}

validate_container_identity() {
    local expected_source_sha="$1"
    local expected_image="$2"
    local expected_activation_id="$3"
    local expected_mode="$4"
    local actual expected_command
    docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1 \
        || fail "shadow container is absent"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{.Config.Image}}')"
    [[ "${actual}" == "${expected_image}" ]] || fail "shadow container image mismatch"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.source-sha"}}')"
    [[ "${actual}" == "${expected_source_sha}" ]] || fail "shadow container source SHA mismatch"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.image-digest"}}')"
    [[ "${actual}" == "${expected_image}" ]] || fail "shadow container digest label mismatch"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.activation-id"}}')"
    [[ "${actual}" == "${expected_activation_id}" ]] || fail "shadow container activation ID mismatch"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.mode"}}')"
    [[ "${actual}" == "${expected_mode}" ]] || fail "shadow container mode mismatch"
    expected_command="[\"python\",\"-m\",\"kiwoom_stock\",\"shadow-worker\",\"--source-sha\",\"${expected_source_sha}\",\"--image-digest\",\"${expected_image}\",\"--activation-id\",\"${expected_activation_id}\"]"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{json .Config.Cmd}}')"
    [[ "${actual}" == "${expected_command}" ]] || fail "shadow container command tuple mismatch"
}

confirm_continuous_tick() {
    local logs="$1"
    local source_sha="$2"
    local image="$3"
    local activation_id="$4"
    local evidence running exit_code
    evidence="$(validate_safe_evidence shadow-continuous cycle \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")" \
        || fail "continuous first safe tick is invalid"
    validate_container_identity "${source_sha}" "${image}" "${activation_id}" shadow-continuous
    running="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Running}}')"
    exit_code="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}')"
    [[ "${running}" == true && "${exit_code}" != 137 ]] \
        || fail "continuous shadow is not running after its first tick"
    printf '%s\n' "${evidence}"
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
    KIWOOM_SHADOW_EXECUTION_MODE=shadow-once \
    KIWOOM_SHADOW_PROCESS_NAME=kiwoom-shadow-once \
    KIWOOM_SHADOW_CLI_COMMAND=shadow-once \
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

run_shadow_continuous() {
    local image="$1"
    local source_sha="$2"
    local activation_id="$3"
    local compose_hash="$4"
    local compose_file first_tick="" deadline now
    WORK_DIR="$(mktemp -d "${RUNTIME_PARENT}/kiwoom-shadow.XXXXXX")"
    compose_file="${WORK_DIR}/${SHADOW_COMPOSE_NAME}"
    download_compose "${source_sha}" "${compose_hash}" "${WORK_DIR}"
    validate_compose_contract "${compose_file}" "${source_sha}" "${compose_hash}"
    ACTIVE_CONTAINER_NAME="${CONTAINER_NAME}"
    if docker container inspect "${ACTIVE_CONTAINER_NAME}" >/dev/null 2>&1; then
        fail "shadow container already exists; use stop before a new activation"
    fi
    trap 'cleanup' EXIT TERM
    timeout "${PULL_TIMEOUT_SECONDS}" docker pull "${image}" \
        || fail "immutable shadow image pull failed or timed out"
    validate_image_revision "${image}" "${source_sha}"
    KIWOOM_IMAGE="${image}" \
    KIWOOM_IMAGE_DIGEST="${image}" \
    KIWOOM_SOURCE_SHA="${source_sha}" \
    KIWOOM_ACTIVATION_ID="${activation_id}" \
    KIWOOM_SHADOW_APP_KEY_FILE="${APP_KEY_FILE}" \
    KIWOOM_SHADOW_SECRET_KEY_FILE="${SECRET_KEY_FILE}" \
    KIWOOM_SHADOW_EXECUTION_MODE=shadow-continuous \
    KIWOOM_SHADOW_PROCESS_NAME=kiwoom-shadow-worker \
    KIWOOM_SHADOW_CLI_COMMAND=shadow-worker \
        docker compose \
        --project-name kiwoom-stock-shadow \
        --project-directory "${WORK_DIR}" \
        --file "${compose_file}" up --detach --no-build app \
        || fail "continuous shadow container failed to start"
    deadline=$(( $(date +%s) + FIRST_TICK_TIMEOUT_SECONDS ))
    while (( $(date +%s) < deadline )); do
        first_tick="$(confirm_continuous_tick \
            "$(docker logs "${CONTAINER_NAME}" 2>&1)" \
            "${source_sha}" "${image}" "${activation_id}" 2>/dev/null || true)"
        if [[ -n "${first_tick}" ]]; then
            printf '%s\n' "${first_tick}"
            cleanup_work_dir || fail "shadow work directory cleanup failed"
            WORK_DIR=""
            ACTIVE_CONTAINER_NAME=""
            trap - EXIT TERM
            printf 'shadow continuous started: source_sha=%s image=%s activation_id=%s side_effects=none\n' \
                "${source_sha}" "${image}" "${activation_id}"
            return 0
        fi
        now="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Running}}' 2>/dev/null || true)"
        [[ "${now}" == true ]] || fail "continuous shadow exited before its first safe tick"
        sleep 2
    done
    fail "continuous shadow first safe tick timed out"
}

stop_shadow() {
    local image="$1"
    local source_sha="$2"
    local activation_id="$3"
    local logs exit_code terminal running expected_status expected_reason
    if ! docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1; then
        fail "shadow container is absent; stop identity cannot be proven"
    fi
    validate_container_identity "${source_sha}" "${image}" "${activation_id}" shadow-continuous
    running="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Running}}')"
    if [[ "${running}" == true ]]; then
        expected_status="STOPPED"
        expected_reason="stop-requested"
        docker stop --time 30 "${CONTAINER_NAME}" >/dev/null \
            || fail "shadow worker did not stop within the graceful budget"
    else
        expected_status="DEADLINE"
        expected_reason="run-deadline"
    fi
    logs="$(docker logs "${CONTAINER_NAME}" 2>&1)"
    terminal="$(validate_safe_evidence shadow-continuous terminal \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")" \
        || fail "continuous terminal safe evidence is missing"
    exit_code="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}')"
    [[ "${exit_code}" == 0 ]] \
        || fail "shadow worker did not exit cleanly"
    python3 -c 'import json,sys; item=json.loads(sys.argv[1]); raise SystemExit(0 if (item.get("status"), item.get("reason")) == (sys.argv[2], sys.argv[3]) else 1)' \
        "${terminal}" "${expected_status}" "${expected_reason}" \
        || fail "shadow terminal state does not match container transition"
    printf '%s\n' "${terminal}"
    docker rm "${CONTAINER_NAME}" >/dev/null || fail "shadow container removal failed"
    docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1 \
        && fail "shadow container remains after stop"
    printf 'shadow worker cleaned: terminal_status=%s terminal_reason=%s exit_code=%s volume=preserved side_effects=none\n' \
        "${expected_status}" "${expected_reason}" "${exit_code}"
}

usage() {
    cat >&2 <<'EOF'
usage:
  kiwoom-shadow-worker --desired-state oneshot|continuous --image DIGEST --source-sha SHA --activation-id ID \
    --compose-shadow-sha256 HASH --expected-instance-id INSTANCE --region REGION
  kiwoom-shadow-worker --desired-state stop --image DIGEST --source-sha SHA \
    --activation-id ID --expected-instance-id INSTANCE --region REGION
EOF
}

main() {
    local image="" source_sha="" activation_id="" compose_hash=""
    local expected_instance="" region="" desired_state=""
    while (( $# )); do
        case "$1" in
            --image) image="${2:-}"; shift 2 ;;
            --source-sha) source_sha="${2:-}"; shift 2 ;;
            --activation-id) activation_id="${2:-}"; shift 2 ;;
            --compose-shadow-sha256) compose_hash="${2:-}"; shift 2 ;;
            --expected-instance-id) expected_instance="${2:-}"; shift 2 ;;
            --region) region="${2:-}"; shift 2 ;;
            --desired-state) desired_state="${2:-}"; shift 2 ;;
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
    case "${desired_state}" in
      oneshot|continuous|stop) ;;
      *) fail "desired state must be exactly oneshot, continuous, or stop" ;;
    esac
    if [[ "${desired_state}" == stop ]]; then
        [[ -z "${compose_hash}" ]] || fail "stop does not accept a Compose hash"
        validate_image "${image}"
        validate_source_sha "${source_sha}"
        validate_activation_id "${activation_id}"
        stop_shadow "${image}" "${source_sha}" "${activation_id}"
        return 0
    fi
    validate_image "${image}"
    validate_source_sha "${source_sha}"
    validate_activation_id "${activation_id}"
    validate_hash "${compose_hash}"
    validate_secret_directory
    if [[ "${desired_state}" == oneshot ]]; then
        run_shadow_once "${image}" "${source_sha}" "${activation_id}" "${compose_hash}"
    else
        run_shadow_continuous "${image}" "${source_sha}" "${activation_id}" "${compose_hash}"
    fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
