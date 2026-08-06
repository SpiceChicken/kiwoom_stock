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
    assert app["labels"] == {
        "io.kiwoom.shadow.source-sha": "${KIWOOM_SOURCE_SHA:?set the exact source SHA}",
        "io.kiwoom.shadow.image-digest": "${KIWOOM_IMAGE_DIGEST:?set the exact image digest}",
        "io.kiwoom.shadow.activation-id": "${KIWOOM_ACTIVATION_ID:?set a unique activation ID}",
        "io.kiwoom.shadow.mode": "${KIWOOM_SHADOW_EXECUTION_MODE:-shadow-once}",
    }
    assert "ALL" in app["cap_drop"]
    assert set(app["cap_add"]) == {"CHOWN", "SETGID", "SETUID"}
    assert app["environment"]["KIWOOM_EXECUTION_MODE"] == "${KIWOOM_SHADOW_EXECUTION_MODE:-shadow-once}"
    assert app["environment"]["KIWOOM_PROCESS_NAME"] == "${KIWOOM_SHADOW_PROCESS_NAME:-kiwoom-shadow-once}"
    assert app["environment"]["KIWOOM_DB_PATH"] == "/var/lib/kiwoom/shadow-trades.db"
    assert app["volumes"] == ["kiwoom-shadow-data:/var/lib/kiwoom"]
    assert "/tmp" in app["tmpfs"]
    assert "/run/secrets:mode=0700" in app["tmpfs"]
    assert app["command"][3] == "${KIWOOM_SHADOW_CLI_COMMAND:-shadow-once}"
    assert "deploy" not in document


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
    assert "entrypoint:" not in text
    assert "/bin/sh" not in text
    assert "bash -c" not in text
