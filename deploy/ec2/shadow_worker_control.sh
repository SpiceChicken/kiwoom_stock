#!/usr/bin/env bash
#
# Root-owned EC2 command for bounded immutable market-read-only shadow execution.
# It never enables account, order, revoke, Slack, S3, or Gemini capabilities.

set -Eeuo pipefail

readonly EXPECTED_REPOSITORY="SpiceChicken/kiwoom_stock"
readonly IMAGE_PREFIX="ghcr.io/spicechicken/kiwoom_stock@sha256:"
readonly EXPECTED_INSTANCE_ID="i-0e42e09d6c087ba29"
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
readonly FIRST_TICK_TIMEOUT_SECONDS="${KIWOOM_SHADOW_FIRST_TICK_TIMEOUT_SECONDS:-720}"
readonly CONTAINER_NAME="kiwoom-shadow-once"
readonly TELEMETRY_VOLUME_NAME="${KIWOOM_SHADOW_TELEMETRY_VOLUME:-kiwoom-stock-shadow_kiwoom-shadow-data}"
# Evidence schema authority is the fixed standalone validator artifact.
readonly ROLLOUT_BINDING_FILE="${KIWOOM_SHADOW_BINDING_FILE:-/var/lib/kiwoom-stock/shadow-rollout-current.json}"
readonly VALIDATOR_PATH="/usr/local/libexec/kiwoom-shadow-runtime-evidence.py"

ACTIVE_CONTAINER_NAME=""
WORK_DIR=""
PULL_LOG=""

fail() {
    printf 'shadow worker failed: %s\n' "$1" >&2
    printf 'shadow worker failed: %s\n' "$1"
    exit 1
}

emit_runtime_failure_sentinel() {
    local logs="$1"
    local failure_details error_type error_kind error_operation
    failure_details="$(printf '%s\n' "${logs}" | python3 -c '
import json
import re
import sys

safe_kinds = {"empty", "fetch", "timeout", "parse", "malformed"}
safe_operations = {
    "auth_preflight", "top_trading_value", "stock_basic",
    "minute_chart_1m", "minute_chart_5m", "minute_chart_60m",
    "tick_strength", "program_trade", "foreign_window_trade",
    "order_book", "recent_ticks", "market_snapshot", "market_regime_60m",
    "chart_true_range",
}
fallback = None
found = False
for raw in sys.stdin:
    line = raw.split("|", 1)[-1].strip()
    if not line.startswith("{"):
        continue
    try:
        item = json.loads(line)
    except json.JSONDecodeError:
        continue
    value = item.get("error_type")
    if (
        item.get("status") == "FAILED"
        and isinstance(value, str)
        and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value)
    ):
        if (
            value == "MarketDataCollectionError"
            and isinstance(item.get("error_kind"), str)
            and item.get("error_kind") in safe_kinds
            and isinstance(item.get("error_operation"), str)
            and item.get("error_operation") in safe_operations
        ):
            print("|".join((value, item["error_kind"], item["error_operation"])))
            found = True
            break
        if fallback is None:
            fallback = value
if not found and fallback is not None:
    print(fallback + "||")
')"
    if [[ -n "${failure_details}" ]]; then
        IFS='|' read -r error_type error_kind error_operation <<< "${failure_details}"
        if [[ "${error_type}" == "MarketDataCollectionError" \
            && -n "${error_kind}" && -n "${error_operation}" ]]; then
            printf 'shadow worker failed: error_type=%s error_kind=%s error_operation=%s\n' \
                "${error_type}" "${error_kind}" "${error_operation}"
            return 0
        fi
        printf 'shadow worker failed: error_type=%s\n' "${error_type}"
    fi
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
    [[ "$1" =~ ^[0-9a-f]{64}$ ]] || fail "hash must be 64 lowercase hex characters"
}

validate_rollout_binding() {
    local expected_source_sha="$1"
    local expected_worker_hash="$2"
    local expected_validator_hash="$3"
    local expected_document_hash="$4"
    local actual_worker_hash actual_validator_hash metadata
    validate_source_sha "${expected_source_sha}"
    validate_hash "${expected_worker_hash}"
    validate_hash "${expected_validator_hash}"
    validate_hash "${expected_document_hash}"
    [[ -f "${BASH_SOURCE[0]}" && ! -L "${BASH_SOURCE[0]}" ]] \
        || fail "installed worker metadata is invalid"
    metadata="$(stat -c '%u:%g:%a:%h:%F' -- "${BASH_SOURCE[0]}")" \
        || fail "installed worker metadata is unavailable"
    [[ "${metadata}" == "0:0:750:1:regular file" ]] \
        || fail "installed worker must be root:root, mode 0750, one regular link"
    actual_worker_hash="$(sha256sum "${BASH_SOURCE[0]}" | cut -d' ' -f1)"
    [[ "${actual_worker_hash}" == "${expected_worker_hash}" ]] \
        || fail "installed worker hash does not match the approved artifact set"
    [[ -f "${VALIDATOR_PATH}" && ! -L "${VALIDATOR_PATH}" ]] \
        || fail "installed validator metadata is invalid"
    metadata="$(stat -c '%u:%g:%a:%h:%F' -- "${VALIDATOR_PATH}")" \
        || fail "installed validator metadata is unavailable"
    [[ "${metadata}" == "0:0:750:1:regular file" ]] \
        || fail "installed validator must be root:root, mode 0750, one regular link"
    actual_validator_hash="$(sha256sum "${VALIDATOR_PATH}" | cut -d' ' -f1)"
    [[ "${actual_validator_hash}" == "${expected_validator_hash}" ]] \
        || fail "installed validator hash does not match the approved artifact set"
    [[ -f "${ROLLOUT_BINDING_FILE}" && ! -L "${ROLLOUT_BINDING_FILE}" ]] \
        || fail "rollout binding marker is absent or invalid"
    metadata="$(stat -c '%u:%g:%a:%h:%F' -- "${ROLLOUT_BINDING_FILE}")" \
        || fail "rollout binding metadata is unavailable"
    [[ "${metadata}" == "0:0:600:1:regular file" ]] \
        || fail "rollout binding must be root:root, mode 0600, one regular link"
    python3 - "${ROLLOUT_BINDING_FILE}" "${expected_source_sha}" \
        "${expected_worker_hash}" "${expected_validator_hash}" \
        "${expected_document_hash}" <<'PY' \
        || fail "rollout binding does not match the approved artifact set"
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    binding = json.load(stream)
expected_keys = {
    "source_sha", "worker_sha256", "validator_sha256",
    "shadow_document_sha256", "rollout_attempt_id"
}
valid = (
    set(binding) == expected_keys
    and binding.get("source_sha") == sys.argv[2]
    and binding.get("worker_sha256") == sys.argv[3]
    and binding.get("validator_sha256") == sys.argv[4]
    and binding.get("shadow_document_sha256") == sys.argv[5]
)
raise SystemExit(0 if valid else 1)
PY
}

acquire_activation_lock() {
    local inherited_fd="$1"
    local expected_identity actual_identity
    if [[ -n "${inherited_fd}" ]]; then
        [[ "${inherited_fd}" =~ ^[3-9][0-9]?$ ]] \
            || fail "inherited lock FD has an invalid bounded format"
        [[ -e "/proc/self/fd/${inherited_fd}" ]] \
            || fail "inherited lock FD is not open"
        [[ "$(readlink -f "/proc/self/fd/${inherited_fd}")" == "$(readlink -f "${LOCK_FILE}")" ]] \
            || fail "inherited lock FD does not reference the approved lock"
        expected_identity="$(stat -Lc '%d:%i:%F' -- "${LOCK_FILE}")" \
            || fail "approved lock metadata is unavailable"
        actual_identity="$(stat -Lc '%d:%i:%F' -- "/proc/self/fd/${inherited_fd}")" \
            || fail "inherited lock metadata is unavailable"
        [[ "${actual_identity}" == "${expected_identity}" ]] \
            || fail "inherited lock FD inode mismatch"
        flock -n "${inherited_fd}" \
            || fail "inherited activation lock is not exclusively held"
        return
    fi
    exec {KIWOOM_SHADOW_LOCK_FD}>"${LOCK_FILE}"
    flock -n "${KIWOOM_SHADOW_LOCK_FD}" \
        || fail "another shadow activation owns the lock"
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

pull_image() {
    local image="$1"
    local attempt category
    PULL_LOG="${WORK_DIR}/.image-pull.log"
    for attempt in 1 2; do
        # SSM stdout is an evidence channel. Docker progress (for example
        # Compose's `[+]` records) is not evidence and can look like malformed
        # JSON to the strict remote validator, so capture it without exposing
        # the raw registry response.
        if timeout "${PULL_TIMEOUT_SECONDS}" docker pull "${image}" \
            >"${PULL_LOG}" 2>&1; then
            rm -f -- "${PULL_LOG}"
            PULL_LOG=""
            return 0
        fi
        if docker image inspect "${image}" >/dev/null 2>&1; then
            rm -f -- "${PULL_LOG}"
            PULL_LOG=""
            return 0
        fi
        if [[ "${attempt}" == 1 ]]; then
            sleep 5
        fi
    done
    if grep -Eqi 'no space left on device|disk quota exceeded' "${PULL_LOG}"; then
        category=image_pull_no_space
    elif grep -Eqi 'unauthorized|authentication required|denied' "${PULL_LOG}"; then
        category=image_pull_auth
    elif grep -Eqi 'manifest unknown|not found' "${PULL_LOG}"; then
        category=image_pull_not_found
    elif grep -Eqi \
        'network is unreachable|no route to host|connection timed out|i/o timeout|TLS handshake timeout|temporary failure in name resolution|no such host' \
        "${PULL_LOG}"; then
        category=image_pull_network
    else
        category=image_pull_failed
    fi
    printf 'shadow worker failed: image_pull_category=%s\n' "${category}" >&2
    return 1
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
    python3 "${VALIDATOR_PATH}" \
        --mode "${expected_mode}" \
        --event "${expected_event}" \
        --source-sha "${expected_source_sha}" \
        --image-digest "${expected_image}" \
        --activation-id "${expected_activation_id}" \
        --input-format json-lines \
        --output accepted-record
}

validate_safe_cycle_sequence() {
    local expected_source_sha="$1"
    local expected_image="$2"
    local expected_activation_id="$3"
    python3 "${VALIDATOR_PATH}" \
        --mode shadow-continuous \
        --event cycle-sequence \
        --source-sha "${expected_source_sha}" \
        --image-digest "${expected_image}" \
        --activation-id "${expected_activation_id}" \
        --input-format json-lines \
        --output accepted-record
}

validate_safe_terminal_diagnostic() {
    local expected_source_sha="$1"
    local expected_image="$2"
    local expected_activation_id="$3"
    python3 "${VALIDATOR_PATH}" \
        --mode shadow-continuous \
        --event terminal \
        --source-sha "${expected_source_sha}" \
        --image-digest "${expected_image}" \
        --activation-id "${expected_activation_id}" \
        --input-format json-lines \
        --output accepted-record \
        --terminal-policy diagnostic
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

validate_container_identity_safe() {
    local expected_source_sha="$1"
    local expected_image="$2"
    local expected_activation_id="$3"
    local expected_mode="$4"
    local actual expected_command
    docker container inspect "${CONTAINER_NAME}" >/dev/null 2>&1 || return 1
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{.Config.Image}}')" || return 1
    [[ "${actual}" == "${expected_image}" ]] || return 1
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.source-sha"}}')" || return 1
    [[ "${actual}" == "${expected_source_sha}" ]] || return 1
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.image-digest"}}')" || return 1
    [[ "${actual}" == "${expected_image}" ]] || return 1
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.activation-id"}}')" || return 1
    [[ "${actual}" == "${expected_activation_id}" ]] || return 1
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{index .Config.Labels "io.kiwoom.shadow.mode"}}')" || return 1
    [[ "${actual}" == "${expected_mode}" ]] || return 1
    expected_command="[\"python\",\"-m\",\"kiwoom_stock\",\"shadow-worker\",\"--source-sha\",\"${expected_source_sha}\",\"--image-digest\",\"${expected_image}\",\"--activation-id\",\"${expected_activation_id}\"]"
    actual="$(docker inspect "${CONTAINER_NAME}" --format '{{json .Config.Cmd}}')" || return 1
    [[ "${actual}" == "${expected_command}" ]]
}

verify_shadow_telemetry() {
    local image="$1" source_sha="$2" activation_id="$3" logs="$4" terminal="$5"
    local volume session manifest hashes expected
    if ! grep -q '"event": *"cycle"' <<<"$logs"; then
        volume="$(docker inspect "${CONTAINER_NAME}" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/kiwoom"}}{{.Name}}{{end}}{{end}}' 2>/dev/null || true)"
        if [[ -n "${volume}" ]]; then
            docker run --rm --network none --read-only -v "${volume}:/var/lib/kiwoom:ro" "${image}" \
                python -c 'import sqlite3, pathlib, sys; p=pathlib.Path("/var/lib/kiwoom/shadow-telemetry.db"); sys.exit(0) if not p.exists() else None; c=sqlite3.connect("file:"+str(p)+"?mode=ro", uri=True); n=c.execute("select count(*) from shadow_cycle_telemetry_v1").fetchone()[0]; c.close(); raise SystemExit("orphan telemetry rows") if n else None' \
                || fail "shadow telemetry has rows without cycle evidence"
        fi
        printf '{"event":"telemetry_manifest","source_sha":"%s","activation_id":"%s","row_count":0,"legacy_terminal_only":true}\n' \
            "$source_sha" "$activation_id"
        return 0
    fi
    volume="$(docker inspect "${CONTAINER_NAME}" --format '{{range .Mounts}}{{if eq .Destination "/var/lib/kiwoom"}}{{.Name}}{{end}}{{end}}')"
    [[ -n "${volume}" && "${volume}" != *$'\n'* ]] || fail "shadow telemetry named volume is missing"
    session="$(python3 -c '
import json,sys
for raw in sys.stdin:
    try: item=json.loads(raw.split("|",1)[-1].strip())
    except json.JSONDecodeError: continue
    if item.get("event") == "cycle" and isinstance(item.get("kst_date"),str):
        print(item["kst_date"]); break
' <<<"${logs}")"
    [[ "${session}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "shadow telemetry session identity is missing"
    expected="$(validate_safe_cycle_sequence "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")" \
        || fail "shadow log telemetry evidence is incomplete"
    manifest="$(docker run --rm --network none --read-only \
        -v "${volume}:/var/lib/kiwoom:ro" "${image}" \
        python -m kiwoom_stock shadow-telemetry-export \
        --database-path /var/lib/kiwoom/shadow-telemetry.db \
        --activation-id "${activation_id}" --session-date-kst "${session}" --manifest-only)" \
        || fail "shadow telemetry manifest export failed"
    hashes="$(docker run --rm --network none --read-only \
        -v "${volume}:/var/lib/kiwoom:ro" "${image}" \
        python -m kiwoom_stock shadow-telemetry-export \
        --database-path /var/lib/kiwoom/shadow-telemetry.db \
        --activation-id "${activation_id}" --session-date-kst "${session}" --row-hashes-only)" \
        || fail "shadow telemetry hash export failed"
    python3 - "${source_sha}" "${image}" "${activation_id}" "${session}" "${terminal}" "${expected}" "${manifest}" "${hashes}" <<'PY'
import json,sys
source,image,activation,session,terminal,expected,manifest,hashes=sys.argv[1:]
term=json.loads(terminal); logged=json.loads(expected); exported=json.loads(manifest); db=json.loads(hashes)
if term.get("cycles") != logged.get("cycles"):
    raise SystemExit(1)
if exported.get("activation_id") != activation or exported.get("session_date_kst") != session:
    raise SystemExit(1)
if logged["cycles"] != exported.get("row_count") or logged["hashes"] != db.get("row_hashes"):
    raise SystemExit(1)
if exported.get("first_cycle") != (1 if logged["cycles"] else None) or exported.get("last_cycle") != logged["cycles"]:
    raise SystemExit(1)
if exported.get("source_sha") != source or exported.get("image_digest") != image:
    raise SystemExit(1)
if not isinstance(exported.get("config_sha256"), str) or len(exported["config_sha256"]) != 64:
    raise SystemExit(1)
if exported.get("database_bytes", 0) > 32 * 1024 * 1024 or exported.get("finalized_session_count", 0) > 20:
    raise SystemExit(1)
print(json.dumps({"event":"telemetry_manifest","source_sha":source,**exported},
                 sort_keys=True,separators=(",",":")))
PY
}

export_shadow_telemetry_page() {
    local image="$1" activation_id="$2" session="$3" offset="$4" length="$5"
    local volume
    [[ "${offset}" =~ ^[0-9]+$ && "${length}" =~ ^[0-9]+$ ]] || fail "telemetry page bounds are invalid"
    (( length > 0 && length <= 12288 )) || fail "telemetry page length exceeds bound"
    volume="$(docker volume inspect "${TELEMETRY_VOLUME_NAME}" --format '{{.Name}}')" || fail "shadow telemetry named volume is absent"
    docker run --rm --network none --read-only \
        -v "${volume}:/var/lib/kiwoom:ro" "${image}" \
        python -m kiwoom_stock shadow-telemetry-export \
        --database-path /var/lib/kiwoom/shadow-telemetry.db \
        --activation-id "${activation_id}" --session-date-kst "${session}" \
        --offset "${offset}" --length "${length}"
}

confirm_continuous_tick() {
    local logs="$1"
    local source_sha="$2"
    local image="$3"
    local activation_id="$4"
    local evidence running exit_code
    if ! evidence="$(validate_safe_evidence shadow-continuous cycle \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")"; then
        printf 'continuous first safe tick is invalid\n' >&2
        return 1
    fi
    validate_container_identity_safe "${source_sha}" "${image}" "${activation_id}" shadow-continuous \
        || return 1
    running="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.Running}}')"
    exit_code="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}')"
    if [[ "${running}" != true || "${exit_code}" == 137 ]]; then
        printf 'continuous shadow is not running after its first tick\n' >&2
        return 1
    fi
    printf '%s\n' "${evidence}"
}

confirm_continuous_calendar_closed() {
    local logs="$1"
    local source_sha="$2"
    local image="$3"
    local activation_id="$4"
    local evidence exit_code
    if ! evidence="$(validate_safe_evidence shadow-continuous terminal \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")"; then
        printf 'continuous calendar-closed terminal is invalid\n' >&2
        return 1
    fi
    if ! python3 -c '
import json
import sys

item = json.load(sys.stdin)
raise SystemExit(
    0
    if item.get("status") == "CLOSED"
    and item.get("reason") == "calendar-closed"
    and item.get("cycles") == 0
    else 1
)
' <<<"${evidence}"
    then
        printf 'continuous terminal is not calendar-closed\n' >&2
        return 1
    fi
    exit_code="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}')" \
        || return 1
    [[ "${exit_code}" == 0 ]] || return 1
    printf '%s\n' "${evidence}"
}

cleanup_work_dir() {
    if [[ -n "${WORK_DIR}" ]]; then
        rm -f -- "${PULL_LOG}" 2>/dev/null || true
        PULL_LOG=""
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
    local compose_file logs evidence
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
    pull_image "${image}" \
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
        >/dev/null 2>&1 \
        || {
            emit_runtime_failure_sentinel "$(docker logs "${CONTAINER_NAME}" 2>&1 || true)" || true
            fail "shadow-once container failed or timed out"
        }
    logs="$(docker logs "${CONTAINER_NAME}" 2>&1)"
    evidence="$(validate_safe_evidence shadow-once oneshot \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")" \
        || fail "shadow one-shot safe evidence is missing or invalid"
    printf '%s\n' "${evidence}"
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
    pull_image "${image}" \
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
        >/dev/null 2>&1 \
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
        if [[ "${now}" != true ]]; then
            logs="$(docker logs "${CONTAINER_NAME}" 2>&1 || true)"
            if closed="$(confirm_continuous_calendar_closed \
                "${logs}" "${source_sha}" "${image}" "${activation_id}" \
                2>/dev/null)"; then
                printf '%s\n' "${closed}"
                cleanup || fail "shadow calendar-closed cleanup failed"
                WORK_DIR=""
                ACTIVE_CONTAINER_NAME=""
                trap - EXIT TERM
                printf 'shadow continuous closed: source_sha=%s image=%s activation_id=%s side_effects=none\n' \
                    "${source_sha}" "${image}" "${activation_id}"
                return 0
            fi
            emit_runtime_failure_sentinel "${logs}" || true
            fail "continuous shadow exited before its first safe tick"
        fi
        sleep 2
    done
    emit_runtime_failure_sentinel "$(docker logs "${CONTAINER_NAME}" 2>&1 || true)" || true
    fail "continuous shadow first safe tick timed out"
}

stop_shadow() {
    local image="$1"
    local source_sha="$2"
    local activation_id="$3"
    local logs exit_code terminal diagnostic running expected_status expected_reason
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
    if ! terminal="$(validate_safe_evidence shadow-continuous terminal \
        "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")"; then
        diagnostic="$(validate_safe_terminal_diagnostic \
            "${source_sha}" "${image}" "${activation_id}" <<<"${logs}")" \
            || fail "continuous terminal safe evidence is missing"
        printf '%s\n' "${diagnostic}"
        fail "shadow runtime terminal state is non-operational; container and logs preserved"
    fi
    exit_code="$(docker inspect "${CONTAINER_NAME}" --format '{{.State.ExitCode}}')"
    [[ "${exit_code}" == 0 ]] \
        || fail "shadow worker did not exit cleanly"
    python3 -c 'import json,sys; item=json.loads(sys.argv[1]); raise SystemExit(0 if (item.get("status"), item.get("reason")) == (sys.argv[2], sys.argv[3]) else 1)' \
        "${terminal}" "${expected_status}" "${expected_reason}" \
        || fail "shadow terminal state does not match container transition"
    manifest="$(verify_shadow_telemetry "${image}" "${source_sha}" "${activation_id}" "${logs}" "${terminal}")"
    [[ -n "${manifest}" ]] || fail "shadow telemetry manifest is empty"
    printf '%s\n' "${terminal}"
    printf '%s\n' "${manifest}"
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
    --compose-shadow-sha256 HASH --expected-worker-sha256 HASH --expected-validator-sha256 HASH \
    --expected-shadow-document-sha256 HASH \
    --expected-instance-id INSTANCE --region REGION
  kiwoom-shadow-worker --desired-state stop --image DIGEST --source-sha SHA \
    --activation-id ID --expected-worker-sha256 HASH --expected-validator-sha256 HASH \
    --expected-shadow-document-sha256 HASH \
    --expected-instance-id INSTANCE --region REGION
  kiwoom-shadow-worker --desired-state telemetry-export-page --image DIGEST --source-sha SHA \
    --activation-id ID --compose-shadow-sha256 HASH --telemetry-session-date-kst YYYY-MM-DD --telemetry-offset N --telemetry-length N \
    --expected-worker-sha256 HASH --expected-validator-sha256 HASH \
    --expected-shadow-document-sha256 HASH --expected-instance-id INSTANCE --region REGION
  The fixed SSM wrapper additionally passes --inherited-lock-fd 9.
EOF
}

main() {
    local image="" source_sha="" activation_id="" compose_hash=""
    local expected_instance="" region="" desired_state=""
    local expected_worker_hash="" expected_validator_hash="" expected_document_hash=""
    local inherited_lock_fd="" telemetry_session="" telemetry_offset="" telemetry_length=""
    while (( $# )); do
        case "$1" in
            --image) image="${2:-}"; shift 2 ;;
            --source-sha) source_sha="${2:-}"; shift 2 ;;
            --activation-id) activation_id="${2:-}"; shift 2 ;;
            --compose-shadow-sha256) compose_hash="${2:-}"; shift 2 ;;
            --expected-worker-sha256) expected_worker_hash="${2:-}"; shift 2 ;;
            --expected-validator-sha256) expected_validator_hash="${2:-}"; shift 2 ;;
            --expected-shadow-document-sha256) expected_document_hash="${2:-}"; shift 2 ;;
            --inherited-lock-fd) inherited_lock_fd="${2:-}"; shift 2 ;;
            --expected-instance-id) expected_instance="${2:-}"; shift 2 ;;
            --region) region="${2:-}"; shift 2 ;;
            --desired-state) desired_state="${2:-}"; shift 2 ;;
            --telemetry-session-date-kst) telemetry_session="${2:-}"; shift 2 ;;
            --telemetry-offset) telemetry_offset="${2:-}"; shift 2 ;;
            --telemetry-length) telemetry_length="${2:-}"; shift 2 ;;
            *) usage; fail "unsupported argument" ;;
        esac
    done
    (( EUID == 0 )) || fail "this command must run as root"
    [[ "${expected_instance}" == "${EXPECTED_INSTANCE_ID}" ]] \
        || fail "expected instance ID is not approved"
    [[ "${region}" == "${EXPECTED_REGION}" ]] || fail "region is not approved"
    command -v flock >/dev/null || fail "flock is unavailable"
    command -v docker >/dev/null || fail "Docker is unavailable"
    acquire_activation_lock "${inherited_lock_fd}"
    validate_instance_identity
    validate_rollout_binding "${source_sha}" "${expected_worker_hash}" \
        "${expected_validator_hash}" "${expected_document_hash}"
    case "${desired_state}" in
      oneshot|continuous|stop|telemetry-export-page) ;;
      *) fail "desired state must be exactly oneshot, continuous, stop, or telemetry-export-page" ;;
    esac
    if [[ "${desired_state}" == telemetry-export-page ]]; then
        validate_image "${image}"
        validate_source_sha "${source_sha}"
        validate_activation_id "${activation_id}"
        [[ "${compose_hash}" == "$(printf '0%.0s' {1..64})" ]] || fail "telemetry page requires the zero Compose hash sentinel"
        [[ "${telemetry_session}" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "telemetry session date is invalid"
        export_shadow_telemetry_page "${image}" "${activation_id}" "${telemetry_session}" "${telemetry_offset:-0}" "${telemetry_length:-12288}"
        return 0
    fi
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
