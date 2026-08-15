#!/usr/bin/env bash
set -Eeuo pipefail

# Direct SSH entry point for the single shadow host. The private key is kept
# outside the repository and is never printed or uploaded.

readonly expected_host="54.116.97.199"
readonly expected_user="ubuntu"
readonly default_identity="/home/pc/.ssh/kiwoom-recovery"

host="${KIWOOM_EC2_HOST:-${expected_host}}"
user="${KIWOOM_EC2_USER:-${expected_user}}"
identity="${KIWOOM_SSH_IDENTITY_FILE:-${default_identity}}"

[[ "${host}" == "${expected_host}" ]] || {
  printf 'refusing unexpected EC2 host: %s\n' "${host}" >&2
  exit 64
}
[[ "${user}" == "${expected_user}" ]] || {
  printf 'refusing unexpected EC2 user: %s\n' "${user}" >&2
  exit 64
}
[[ -f "${identity}" ]] || {
  printf 'SSH private key is missing: %s\n' "${identity}" >&2
  exit 65
}
[[ "$(stat -c '%a' "${identity}")" == 600 ]] || {
  printf 'SSH private key must have mode 600: %s\n' "${identity}" >&2
  exit 66
}

exec ssh \
  -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=accept-new \
  -o ConnectTimeout=10 \
  -i "${identity}" \
  "${user}@${host}"
