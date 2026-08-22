#!/usr/bin/env bash
#
# Install the C* fence and its pure contract as inert, root-owned host artifacts.
# This script never arms the authority, starts a worker, or enables a schedule.
set -Eeuo pipefail

readonly MODE="${1:-}"
readonly PREFIX="/usr/local/libexec"
readonly STATE_DIR="/var/lib/kiwoom-stock/shadow-schedule"
readonly SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "${MODE}" in
  --check)
    test -f "${SOURCE_DIR}/shadow_schedule_fence.py"
    test -f "${SOURCE_DIR}/shadow_evidence_export.py"
    test -f "${SOURCE_DIR}/../shadow_cstar_contract.py"
    printf '%s\n' "C* artifacts available; no host changes made"
    exit 0
    ;;
  --apply)
    [[ "${EUID}" -eq 0 ]] || { printf '%s\n' "root is required" >&2; exit 77; }
    install -d -o root -g root -m 0755 "${PREFIX}"
    install -d -o root -g root -m 0700 "${STATE_DIR}"
    install -o root -g root -m 0750 \
      "${SOURCE_DIR}/shadow_schedule_fence.py" \
      "${PREFIX}/kiwoom-shadow-schedule-fence.py"
    install -o root -g root -m 0750 \
      "${SOURCE_DIR}/shadow_evidence_export.py" \
      "${PREFIX}/kiwoom-shadow-evidence-export.py"
    install -o root -g root -m 0640 \
      "${SOURCE_DIR}/../shadow_cstar_contract.py" \
      "${PREFIX}/shadow_cstar_contract.py"
    printf '%s\n' "C* artifacts installed inert; authority remains disarmed"
    ;;
  *)
    printf 'usage: %s --check|--apply\n' "$0" >&2
    exit 64
    ;;
esac
