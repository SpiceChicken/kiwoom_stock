"""Contract tests for the concrete Gemini and Slack reporting adapters."""

from typing import Any

import pytest

from kiwoom_stock.application.reporting import (
    DailyReportRequest,
    DailyReportStats,
    NarrationResult,
    ReportArtifact,
)
from kiwoom_stock.monitoring import notifier as notifier_module
from kiwoom_stock.utils import gemini_client as gemini_module


TARGET_DATE = "2026-07-17"
REPORT_DATE = "2026-07-19"
REQUEST = DailyReportRequest(TARGET_DATE, REPORT_DATE)
STATS = DailyReportStats("50.0% (1승 1패)", "+1.50%", 2)
TRADE_ARTIFACT = ReportArtifact(
    "trade_analysis",
    f"physics_trade_analysis_{TARGET_DATE}.csv",
    "/opaque/trade-analysis.csv",
)


@pytest.fixture(autouse=True)
def _forbid_external_clients(monkeypatch):
    class FakeGeminiClient:
        def __init__(self, *_args, **_kwargs):
            pass

    def fail_request(*_args, **_kwargs):
        pytest.fail("delivery adapter test attempted a real Slack request")

    monkeypatch.setattr(notifier_module, "GeminiClient", FakeGeminiClient)
    monkeypatch.setattr(notifier_module.requests, "post", fail_request)


class _ModelResponse:
    def __init__(self, text: str | None):
        self.text = text


class _LegacyModel:
    def __init__(self, *, text: str = "  모델 총평  ", error=None):
        self.text = text
        self.error = error
        self.calls = []

    def generate_content(self, content):
        self.calls.append(content)
        if self.error is not None:
            raise self.error
        return _ModelResponse(self.text)


class _LegacyGeminiSDK:
    def __init__(self, model):
        self.model = model
        self.configure_calls = []
        self.model_names = []
        self.upload_calls = []
        self.uploaded_file = object()

        class _Models:
            def generate_content(inner_self, **kwargs):
                return self.model.generate_content(kwargs["contents"])

        class _Files:
            def upload(inner_self, **kwargs):
                self.upload_calls.append(
                    (kwargs["file"], kwargs["config"]["mime_type"])
                )
                return self.uploaded_file

        self.models = _Models()
        self.files = _Files()

    def Client(self, *, api_key):
        self.configure_calls.append(api_key)
        return self

class _ModernModels:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _ModernFiles:
    def __init__(self, uploaded_file):
        self.uploaded_file = uploaded_file
        self.calls = []

    def upload(self, **kwargs):
        self.calls.append(kwargs)
        return self.uploaded_file


class _ModernGeminiSDK:
    def __init__(self):
        self.models = _ModernModels(_ModelResponse("  modern 총평  "))
        self.files = _ModernFiles(object())

    def Client(self, *, api_key):
        self.api_key = api_key
        return self


def test_modern_gemini_uploads_and_generates_once(monkeypatch, tmp_path):
    sdk = _ModernGeminiSDK()
    monkeypatch.setattr(gemini_module, "genai", sdk)
    client = gemini_module.GeminiClient(api_key="fake-key")
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text("symbol,pnl\nABC,1\n", encoding="utf-8")

    result = client.generate_content("summarize", file_path=str(csv_path))

    assert result == {"success": True, "output": "modern 총평", "error": None}
    assert sdk.files.calls == [{"file": str(csv_path), "config": {"mime_type": "text/csv"}}]
    assert len(sdk.models.calls) == 1
    assert sdk.models.calls[0]["model"] == "gemini-2.5-flash"
    assert sdk.models.calls[0]["contents"][1] == "summarize"


def test_modern_gemini_rejects_response_without_text(monkeypatch):
    sdk = _ModernGeminiSDK()
    sdk.models.response = _ModelResponse(None)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    client = gemini_module.GeminiClient(api_key="fake-key")

    result = client.generate_content("summarize")

    assert result == {
        "success": False,
        "output": None,
        "error": "Gemini response did not include text",
    }
    assert len(sdk.models.calls) == 1


def test_modern_daily_report_rejects_missing_text_without_retry(
    monkeypatch,
    tmp_path,
):
    sdk = _ModernGeminiSDK()
    sdk.models.response = _ModelResponse(None)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    client = gemini_module.GeminiClient(api_key="fake-key")
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text("symbol,pnl\nABC,1\n", encoding="utf-8")

    result = client.generate_daily_report(
        stats={
            "date": REPORT_DATE,
            "win_rate": STATS.win_rate,
            "total_pnl": STATS.total_pnl,
            "defense_count": STATS.defense_count,
        },
        csv_path=str(csv_path),
    )

    assert result == {
        "success": False,
        "output": None,
        "error": "Gemini response did not include text",
    }
    assert len(sdk.files.calls) == 1
    assert len(sdk.models.calls) == 1


def test_gemini_narrator_maps_success_date_and_artifact_reference(monkeypatch):
    model = _LegacyModel()
    sdk = _LegacyGeminiSDK(model)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    client = gemini_module.GeminiClient(api_key="fake-key")

    result = client.narrate(
        request=REQUEST,
        stats=STATS,
        trade_artifact=TRADE_ARTIFACT,
    )

    assert result == NarrationResult.succeeded("모델 총평")
    assert sdk.configure_calls == ["fake-key"]
    assert sdk.upload_calls == [
        (TRADE_ARTIFACT.reference, "text/csv"),
    ]
    uploaded_file, prompt = model.calls[0]
    assert uploaded_file is sdk.uploaded_file
    assert f"- 날짜: {REPORT_DATE}" in prompt
    assert f"- 날짜: {TARGET_DATE}" not in prompt
    assert f"- 승률: {STATS.win_rate}" in prompt


def test_gemini_narrator_maps_unavailable_and_legacy_failure(monkeypatch):
    unused_sdk = _LegacyGeminiSDK(_LegacyModel())
    monkeypatch.setattr(gemini_module, "genai", unused_sdk)
    unavailable = gemini_module.GeminiClient(api_key=None)
    assert unavailable.narrate(
        request=REQUEST,
        stats=STATS,
        trade_artifact=None,
    ) == NarrationResult.unavailable()
    assert unused_sdk.configure_calls == []
    assert unused_sdk.upload_calls == []

    model = _LegacyModel(error=RuntimeError("quota exceeded"))
    sdk = _LegacyGeminiSDK(model)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    failing = gemini_module.GeminiClient(api_key="fake-key")
    result = failing.narrate(
        request=REQUEST,
        stats=STATS,
        trade_artifact=None,
    )
    assert result == NarrationResult.failed("Gemini request failed")
    assert "quota exceeded" not in repr(result)


class _HTTPResponse:
    def raise_for_status(self):
        return None


def _notifier(config, *, uploader_factory=None):
    return notifier_module.Notifier(
        {},
        config,
        uploader_factory=uploader_factory,
    )


def _expected_blocks(narrative: str):
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📈 일일 마감 부검 리포트",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📅 날짜:*\n{REPORT_DATE}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*✅ 승률:*\n{STATS.win_rate}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*💰 총 수익률:*\n*{STATS.total_pnl}*",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        "*🛡️ 쉴드 방어 (수급 락 등):*\n"
                        f"{STATS.defense_count}건 차단"
                    ),
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📝 아키텍트 AI 총평*\n{narrative}",
            },
        },
    ]


def test_notifier_typed_summary_uses_explicit_date_and_exact_blocks(
    monkeypatch,
):
    calls = []

    def fake_post(url, *, json, timeout):
        calls.append((url, json, timeout))
        return _HTTPResponse()

    monkeypatch.setattr(notifier_module.requests, "post", fake_post)
    notifier = _notifier(
        {
            "webhook_url": "https://unused.invalid/webhook",
            "slack_token": "fake-token",
            "slack_channel": "C123",
        }
    )

    assert notifier.webhook_url == "https://unused.invalid/webhook"
    assert notifier.slack_token == "fake-token"
    assert notifier.slack_channel == "C123"
    assert notifier.summary_enabled() is True
    assert notifier.publish_summary(
        request=REQUEST,
        stats=STATS,
        narrative="typed narrative",
        trade_artifact=TRADE_ARTIFACT,
    ) is True
    assert calls == [
        (
            "https://unused.invalid/webhook",
            {"blocks": _expected_blocks("typed narrative")},
            5,
        )
    ]


def test_notifier_summary_missing_is_skip_and_failure_is_sanitized(
    monkeypatch,
):
    missing = _notifier({})
    assert missing.summary_enabled() is False
    assert missing.publish_summary(
        request=REQUEST,
        stats=STATS,
        narrative="unused",
        trade_artifact=None,
    ) is False

    secret = "SECRET-SENTINEL-webhook"

    def fail_post(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(notifier_module.requests, "post", fail_post)
    configured = _notifier({"webhook_url": "https://unused.invalid"})
    with pytest.raises(RuntimeError) as raised:
        configured.publish_summary(
            request=REQUEST,
            stats=STATS,
            narrative="unused",
            trade_artifact=None,
        )
    assert str(raised.value) == "report summary publication failed"
    assert secret not in repr(raised.value)


class _FakeSlackWebClient:
    def __init__(self):
        self.calls = []

    def files_upload_v2(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def test_typed_telemetry_reuses_trade_then_chunked_slack_uploader(
    monkeypatch,
    tmp_path,
):
    client = _FakeSlackWebClient()
    monkeypatch.setattr(
        notifier_module,
        "WebClient",
        lambda *, token: client,
    )
    trade_path = tmp_path / "trade.csv"
    trade_path.write_text("profit_rate\n1.0\n", encoding="utf-8")
    minute_artifacts = []
    for index in range(23):
        path = tmp_path / f"minute-{index:02d}.csv"
        path.write_text(f"time\n{index}\n", encoding="utf-8")
        minute_artifacts.append(
            ReportArtifact("minute_chart", path.name, str(path))
        )
    trade_artifact = ReportArtifact(
        "trade_analysis",
        trade_path.name,
        str(trade_path),
    )
    notifier = _notifier(
        {
            "slack_token": "fake-token",
            "slack_channel": "C123",
        }
    )

    assert notifier.publish_telemetry(
        request=REQUEST,
        trade_artifact=trade_artifact,
        minute_artifacts=minute_artifacts,
    ) is True

    assert client.calls[0] == {
        "channel": "C123",
        "initial_comment": (
            "📊 *[20260719] V3.0 엔진 매매 분석 리포트*"
        ),
        "file": str(trade_path),
        "title": "trade.csv",
    }
    minute_calls = client.calls[1:]
    assert [len(call["file_uploads"]) for call in minute_calls] == [
        10,
        10,
        3,
    ]
    comment = "📈 *[20260719] 1분봉 백업 데이터 일괄 업로드 (23개 종목)*"
    assert [call["initial_comment"] for call in minute_calls] == [
        comment,
        f"{comment} (이어서 계속...)",
        f"{comment} (이어서 계속...)",
    ]


class _RecordingUploader:
    def __init__(self, *, trade_result=True, minute_result=True):
        self.trade_result = trade_result
        self.minute_result = minute_result
        self.calls = []

    def upload_csv(self, path, comment):
        self.calls.append(("trade", path, comment))
        return self.trade_result

    def upload_multiple_files(self, paths, comment):
        self.calls.append(("minute", list(paths), comment))
        return self.minute_result


def test_telemetry_missing_credentials_and_empty_artifacts_are_skips():
    factory_calls = []

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return _RecordingUploader()

    for config in ({}, {"slack_token": "token"}, {"slack_channel": "C1"}):
        notifier = _notifier(config, uploader_factory=factory)
        assert notifier.publish_telemetry(
            request=REQUEST,
            trade_artifact=TRADE_ARTIFACT,
            minute_artifacts=(),
        ) is False

    configured = _notifier(
        {"slack_token": "token", "slack_channel": "C1"},
        uploader_factory=factory,
    )
    assert configured.publish_telemetry(
        request=REQUEST,
        trade_artifact=None,
        minute_artifacts=(),
    ) is False
    assert factory_calls == []


def test_telemetry_false_still_attempts_minute_then_raises_safe_failure():
    uploader = _RecordingUploader(trade_result=False, minute_result=True)
    notifier = _notifier(
        {"slack_token": "token", "slack_channel": "C1"},
        uploader_factory=lambda **_kwargs: uploader,
    )
    minute = ReportArtifact("minute_chart", "minute.csv", "/opaque/minute.csv")

    with pytest.raises(RuntimeError) as raised:
        notifier.publish_telemetry(
            request=REQUEST,
            trade_artifact=TRADE_ARTIFACT,
            minute_artifacts=(minute,),
        )

    assert [call[0] for call in uploader.calls] == ["trade", "minute"]
    assert str(raised.value) == "report telemetry publication failed"
    assert "trade artifact upload failed" not in repr(raised.value)


def test_telemetry_unexpected_factory_error_does_not_expose_secret():
    secret = "SECRET-SENTINEL-token"

    def fail_factory(**_kwargs: Any):
        raise RuntimeError(secret)

    notifier = _notifier(
        {"slack_token": "token", "slack_channel": "C1"},
        uploader_factory=fail_factory,
    )
    with pytest.raises(RuntimeError) as raised:
        notifier.publish_telemetry(
            request=REQUEST,
            trade_artifact=TRADE_ARTIFACT,
            minute_artifacts=(),
        )
    assert str(raised.value) == "report telemetry publication failed"
    assert secret not in repr(raised.value)
