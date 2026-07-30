#!/usr/bin/env bash
set -Eeuo pipefail

# Ubuntu 24.04 EC2 bootstrap for the Kiwoom host. This script deliberately
# contains no credentials, does not clone or start the application, and makes
# only host package/configuration changes.
export DEBIAN_FRONTEND=noninteractive
readonly STATE_DIR=/var/lib/kiwoom-stock
readonly COMPLETE_MARKER="$STATE_DIR/cloud-init-complete"
readonly APP_DIR=/opt/kiwoom-stock

on_error() {
  printf 'kiwoom cloud-init failed at line %s\n' "$1" >&2
}
trap 'on_error "$LINENO"' ERR

[[ "$(id -u)" == 0 ]] || { echo 'must run as root' >&2; exit 1; }
mkdir -p "$STATE_DIR" "$APP_DIR" /etc/systemd/journald.conf.d /etc/docker
chmod 0755 "$APP_DIR"
rm -f "$COMPLETE_MARKER"

# The dedicated security group permits outbound TCP 443 only. Ubuntu 24.04
# may ship HTTP apt URIs, so switch every configured apt source to HTTPS before
# the first network operation. The stock AMI must already contain the CA bundle.
test -r /etc/ssl/certs/ca-certificates.crt || {
  echo 'CA certificate bundle is required before HTTPS apt bootstrap' >&2
  exit 1
}
while IFS= read -r -d '' apt_source; do
  sed -i 's|http://|https://|g' "$apt_source"
done < <(find /etc/apt -maxdepth 2 -type f \
  \( -name '*.list' -o -name '*.sources' \) -print0)
if grep -R --include='*.list' --include='*.sources' -n 'http://' /etc/apt; then
  echo 'insecure apt source remains after HTTPS conversion' >&2
  exit 1
fi

apt-get update
apt-get install --yes --no-install-recommends \
  python3-venv ca-certificates curl docker.io docker-compose-v2

systemctl enable docker.service
systemctl start docker.service
systemctl is-active --quiet docker.service

# Ubuntu AMIs may provide the agent as a deb unit or as the snap unit. Never
# leave an instance unmanaged when neither unit is present.
ssm_unit=""
for candidate in amazon-ssm-agent.service snap.amazon-ssm-agent.amazon-ssm-agent.service; do
  if systemctl list-unit-files "$candidate" --no-legend 2>/dev/null \
    | awk '{print $1}' | grep -Fxq "$candidate"; then
    ssm_unit="$candidate"
    break
  fi
done
if [[ -z "$ssm_unit" ]]; then
  echo 'amazon-ssm-agent unit is missing (expected deb or snap unit)' >&2
  exit 1
fi
systemctl enable "$ssm_unit"
systemctl start "$ssm_unit"
systemctl is-active --quiet "$ssm_unit"

cat > /etc/systemd/journald.conf.d/kiwoom.conf <<'EOF'
[Journal]
SystemMaxUse=200M
RuntimeMaxUse=100M
MaxRetentionSec=14day
EOF

cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "3"}
}
EOF

systemctl restart systemd-journald.service
systemctl restart docker.service
touch "$COMPLETE_MARKER"
chmod 0644 "$COMPLETE_MARKER"
printf 'kiwoom cloud-init completed\n'
