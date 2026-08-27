from pathlib import Path


SCRIPT = Path("deploy/ec2/cloud-init-ubuntu-24.04.sh")


def test_cloud_init_script_is_strict_and_noninteractive():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -Eeuo pipefail" in text
    assert "DEBIAN_FRONTEND=noninteractive" in text
    assert "apt-get install --yes" in text


def test_cloud_init_converts_apt_sources_to_https_before_update():
    text = SCRIPT.read_text(encoding="utf-8")
    conversion = text.index("sed -i 's|http://|https://|g'")
    update = text.index("apt-get update")
    assert conversion < update
    assert "CA certificate bundle is required" in text
    assert "insecure apt source remains" in text


def test_cloud_init_installs_required_host_packages_and_docker():
    text = SCRIPT.read_text(encoding="utf-8")
    packages = (
        "python3-venv", "ca-certificates", "curl", "openssh-server", "docker.io",
        "docker-compose-v2",
    )
    for package in packages:
        assert package in text
    assert "systemctl enable docker.service" in text
    assert "systemctl start docker.service" in text


def test_cloud_init_requires_and_starts_either_ssm_agent_unit():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "amazon-ssm-agent.service" in text
    assert "snap.amazon-ssm-agent.amazon-ssm-agent.service" in text
    assert "amazon-ssm-agent unit is missing" in text
    assert 'systemctl enable "$ssm_unit"' in text
    assert 'systemctl start "$ssm_unit"' in text


def test_cloud_init_hardens_and_starts_ssh():
    text = SCRIPT.read_text(encoding="utf-8")
    for setting in (
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitRootLogin no",
        "PubkeyAuthentication yes",
        "X11Forwarding no",
        "AllowUsers ubuntu",
    ):
        assert setting in text
    run_sshd_dir = text.index("install -d -m 0755 /run/sshd")
    validate_sshd = text.index("sshd -t")
    assert run_sshd_dir < validate_sshd
    assert "systemctl enable ssh.service" in text
    assert "systemctl restart ssh.service" in text


def test_cloud_init_is_host_only_and_has_completion_marker():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "/opt/kiwoom-stock" in text
    assert "cloud-init-complete" in text
    assert 'rm -f "$COMPLETE_MARKER"' in text
    assert "git clone" not in text
    assert "docker compose up" not in text
    assert "ssh_config" not in text
    assert "KIWOOM_SECRET" not in text
    assert "KIWOOM_APP_KEY" not in text


def test_cloud_init_limits_journald_and_docker_logs():
    text = SCRIPT.read_text(encoding="utf-8")
    assert "SystemMaxUse=200M" in text
    assert '"max-size": "10m"' in text
    assert '"max-file": "3"' in text


def test_cloud_init_checks_host_services_active_before_completion():
    text = SCRIPT.read_text(encoding="utf-8")
    marker = text.rindex('touch "$COMPLETE_MARKER"')
    docker_active = text.index("systemctl is-active --quiet docker.service")
    ssm_active = text.index('systemctl is-active --quiet "$ssm_unit"')
    assert docker_active < marker
    assert ssm_active < marker
