from pathlib import Path

import yaml


def test_shadow_compose_is_single_process_no_restart_and_isolated_volume():
    document = yaml.safe_load(Path("compose.shadow.yaml").read_text(encoding="utf-8"))
    app = document["services"]["app"]
    assert app["container_name"] == "kiwoom-shadow-once"
    assert app["restart"] == "no"
    assert app["user"] == "0:0"
    assert app["read_only"] is True
    assert app["stop_grace_period"] == "30s"
    assert "ALL" in app["cap_drop"]
    assert set(app["cap_add"]) == {"CHOWN", "SETGID", "SETUID"}
    assert app["environment"]["KIWOOM_EXECUTION_MODE"] == "shadow-once"
    assert app["environment"]["KIWOOM_PROCESS_NAME"] == "kiwoom-shadow-once"
    assert app["environment"]["KIWOOM_DB_PATH"] == "/var/lib/kiwoom/shadow-trades.db"
    assert app["volumes"] == ["kiwoom-shadow-data:/var/lib/kiwoom"]
    assert "/tmp" in app["tmpfs"]
    assert "/run/secrets:mode=0700" in app["tmpfs"]


def test_shadow_compose_requires_immutable_tuple_and_external_secret_files():
    text = Path("compose.shadow.yaml").read_text(encoding="utf-8")
    for marker in (
        "KIWOOM_IMAGE:?",
        "KIWOOM_SOURCE_SHA:?",
        "KIWOOM_IMAGE_DIGEST:?",
        "KIWOOM_ACTIVATION_ID:?",
        "KIWOOM_SHADOW_APP_KEY_FILE:?",
        "KIWOOM_SHADOW_SECRET_KEY_FILE:?",
    ):
        assert marker in text
    assert "KIWOOM_APP_KEY:" not in text
    assert "KIWOOM_SECRET_KEY:" not in text
