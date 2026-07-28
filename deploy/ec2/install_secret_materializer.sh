#!/usr/bin/env bash
set -euo pipefail

# Install the host materializer without embedding values. Default is a safe
# check-only mode; --apply performs local filesystem changes and must run as root.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PREFIX=/usr/local
VENV=/opt/kiwoom-stock/.venv
CONFIG_DIR=/etc/kiwoom
CHECK_ONLY=1
if [[ "${1:-}" == "--apply" ]]; then
  CHECK_ONLY=0
elif [[ "${1:-}" != "--check" && $# -gt 0 ]]; then
  echo 'usage: install_secret_materializer.sh [--check|--apply]' >&2
  exit 2
fi

if [[ "$CHECK_ONLY" == 1 ]]; then
  command -v python3 >/dev/null
  test -f "$ROOT_DIR/deploy/ec2/materialize_kiwoom_secrets.py"
  test -f "$ROOT_DIR/deploy/ec2/kiwoom-secrets.service"
  test -f "$ROOT_DIR/deploy/ec2/kiwoom-secrets.conf.example"
  grep -Fq \
    "ExecStart=$VENV/bin/python $PREFIX/libexec/materialize_kiwoom_secrets.py" \
    "$ROOT_DIR/deploy/ec2/kiwoom-secrets.service"
  grep -Fq 'KIWOOM_AWS_REGION=' \
    "$ROOT_DIR/deploy/ec2/kiwoom-secrets.conf.example"
  echo "check passed (no changes made)"
  exit 0
fi

if [[ "$(id -u)" != 0 ]]; then
  echo 'apply requires root' >&2
  exit 1
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  echo 'python venv support is required; install the matching python3-venv package' >&2
  exit 1
fi

install -d -m 0755 /opt/kiwoom-stock "$PREFIX/libexec"
install -d -m 0750 "$CONFIG_DIR"
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install "boto3>=1.34.0,<2"
install -m 0750 -o root -g root \
  "$ROOT_DIR/deploy/ec2/materialize_kiwoom_secrets.py" \
  "$PREFIX/libexec/materialize_kiwoom_secrets.py"
install -m 0640 -o root -g root \
  "$ROOT_DIR/deploy/ec2/kiwoom-secrets.conf.example" \
  "$CONFIG_DIR/kiwoom-secrets.conf.example"
if [[ ! -e "$CONFIG_DIR/kiwoom-secrets.conf" ]]; then
  install -m 0640 -o root -g root \
    "$ROOT_DIR/deploy/ec2/kiwoom-secrets.conf.example" \
    "$CONFIG_DIR/kiwoom-secrets.conf"
fi
install -m 0644 -o root -g root \
  "$ROOT_DIR/deploy/ec2/kiwoom-secrets.service" \
  /etc/systemd/system/kiwoom-secrets.service
systemd-analyze verify /etc/systemd/system/kiwoom-secrets.service
systemctl daemon-reload
systemctl enable kiwoom-secrets.service
echo 'installed; run systemctl start kiwoom-secrets.service after reviewing the config'
