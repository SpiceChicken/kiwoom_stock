"""Static contract tests for the repository secret scanner configuration."""

from pathlib import Path
import tomllib


CONFIG_PATH = Path(".gitleaks.toml")


def _config():
    return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_gitleaks_extends_defaults_with_one_focused_kiwoom_rule():
    config = _config()

    assert config["extend"] == {"useDefault": True}
    assert len(config["rules"]) == 1
    rule = config["rules"][0]
    assert rule["id"] == "kiwoom-credential-assignment"
    assert rule["secretGroup"] == 1
    assert rule["entropy"] == 3.5
    assert rule["keywords"] == [
        "kiwoom",
        "appkey",
        "app_key",
        "app-key",
        "secretkey",
        "secret_key",
        "secret-key",
    ]
    assert "{24,}" in rule["regex"]


def test_gitleaks_config_has_no_broad_allowlist():
    text = CONFIG_PATH.read_text(encoding="utf-8").lower()

    assert "allowlist" not in text
    assert "stopwords" not in text
