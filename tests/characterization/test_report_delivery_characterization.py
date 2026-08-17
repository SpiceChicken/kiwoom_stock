"""Golden contracts for legacy Gemini and Slack report delivery adapters."""

from datetime import datetime
import hashlib
from importlib import resources

import pytest

from kiwoom_stock.monitoring import notifier as notifier_module
from kiwoom_stock.monitoring import reporter as reporter_module
from kiwoom_stock.utils import gemini_client as gemini_module


SYSTEM_PROMPT_SHA256 = (
    "e34eb63d11af3cb3fe3cce49bb20b54d0dec7827a34041f29153c14d3ddf428b"
)
USER_PROMPT_SHA256 = (
    "7ed64846a297d0c73069e2e877ab58ca12a8d7c9ee20b87c832b6aca9c19c0d4"
)


@pytest.fixture(autouse=True)
def _forbid_unpatched_external_calls(monkeypatch):
    def fail_request(*_args, **_kwargs):
        pytest.fail(
            "a characterization test attempted a real Slack webhook request"
        )

    class ForbiddenGeminiSDK:
        def __getattr__(self, name):
            pytest.fail(
                f"a characterization test used the real Gemini SDK: {name}"
            )

    monkeypatch.setattr(notifier_module.requests, "post", fail_request)
    monkeypatch.setattr(gemini_module, "genai", ForbiddenGeminiSDK())


class _Response:
    def __init__(self, text):
        self.text = text


class _FakeModel:
    def __init__(self, *, response_text="  분석 결과  \n", error=None):
        self.response_text = response_text
        self.error = error
        self.calls = []

    def generate_content(self, content):
        self.calls.append(content)
        if self.error is not None:
            raise self.error
        return _Response(self.response_text)


class _FakeLegacyGeminiSDK:
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

def _prompt_bytes(name):
    prompt = resources.files("kiwoom_stock.resources.prompts").joinpath(name)
    return prompt.read_bytes()


def _normalized_prompt_text(name):
    return _prompt_bytes(name).decode("utf-8").replace("\r\n", "\n")


def test_packaged_prompt_bytes_and_daily_report_rendering_are_golden(
    monkeypatch,
    tmp_path,
):
    system_bytes = _prompt_bytes("daily_postmortem_system.md")
    user_bytes = _prompt_bytes("daily_postmortem_user.md")
    assert hashlib.sha256(system_bytes).hexdigest() == SYSTEM_PROMPT_SHA256
    assert hashlib.sha256(user_bytes).hexdigest() == USER_PROMPT_SHA256

    model = _FakeModel(response_text=" \n 냉정한 분석 결과 \t")
    sdk = _FakeLegacyGeminiSDK(model)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    csv_path = tmp_path / "physics_trade_analysis_2026-07-17.csv"
    csv_path.write_text("profit_rate\n1.0\n", encoding="utf-8")
    stats = {
        "date": "2026-07-17",
        "win_rate": "50.0% (1승 1패)",
        "total_pnl": "+1.50%",
        "defense_count": 2,
    }

    client = gemini_module.GeminiClient(api_key="fake-gemini-key")
    result = client.generate_daily_report(stats=stats, csv_path=str(csv_path))

    expected_stats = (
        "- 날짜: 2026-07-17\n"
        "- 승률: 50.0% (1승 1패)\n"
        "- 총 수익률: +1.50%\n"
        "- 쉴드 방어 횟수: 2건"
    )
    expected_user = _normalized_prompt_text("daily_postmortem_user.md")
    expected_user = expected_user.replace("{stats}", expected_stats)
    expected_user = expected_user.replace(
        "{logs}",
        "첨부된 CSV 파일 참조",
    )
    expected_system = _normalized_prompt_text("daily_postmortem_system.md")
    expected_prompt = (
        f"{expected_system}\n\n{expected_user}"
    )

    assert client.model_name == "gemini-2.5-flash"
    assert sdk.configure_calls == ["fake-gemini-key"]
    assert sdk.upload_calls == [(str(csv_path), "text/csv")]
    assert model.calls == [[sdk.uploaded_file, expected_prompt]]
    assert result == {"success": True, "output": "냉정한 분석 결과", "error": None}


def test_gemini_unprepared_and_failure_results_are_exact(monkeypatch):
    unused_model = _FakeModel()
    sdk = _FakeLegacyGeminiSDK(unused_model)
    monkeypatch.setattr(gemini_module, "genai", sdk)
    unprepared = gemini_module.GeminiClient(api_key=None)

    assert unprepared.check_availability() is False
    assert unprepared.generate_daily_report(stats={}) == {
        "success": False,
        "output": None,
        "error": "Gemini 엔진 미초기화",
    }
    assert unprepared.generate_content("prompt") == {
        "success": False,
        "output": None,
        "error": "Gemini 엔진 미초기화",
    }
    assert sdk.configure_calls == []
    assert sdk.model_names == []
    assert sdk.upload_calls == []

    failing_model = _FakeModel(error=RuntimeError("legacy sdk failed"))
    failing_sdk = _FakeLegacyGeminiSDK(failing_model)
    monkeypatch.setattr(gemini_module, "genai", failing_sdk)
    failing = gemini_module.GeminiClient(api_key="fake-key")

    assert failing.generate_daily_report(stats={}) == {
        "success": False,
        "output": None,
        "error": "legacy sdk failed",
    }


class _FakeAIClient:
    def __init__(self, *, available, result=None):
        self.available = available
        self.result = result
        self.availability_calls = 0
        self.report_calls = []

    def check_availability(self):
        self.availability_calls += 1
        return self.available

    def generate_daily_report(self, *, stats, csv_path):
        self.report_calls.append((stats, csv_path))
        return self.result


def _notifier_without_constructor(*, webhook_url, ai_client):
    notifier = notifier_module.Notifier.__new__(notifier_module.Notifier)
    notifier.stock_names = {}
    notifier.webhook_url = webhook_url
    notifier.ai_client = ai_client
    notifier.status_data = []
    return notifier


def _expected_summary_blocks(stats, ai_comment):
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📈 일일 마감 부검 리포트"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*📅 날짜:*\n{stats['date']}"},
                {"type": "mrkdwn", "text": f"*✅ 승률:*\n{stats['win_rate']}"},
                {
                    "type": "mrkdwn",
                    "text": f"*💰 총 수익률:*\n*{stats['total_pnl']}*",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        "*🛡️ 쉴드 방어 (수급 락 등):*\n"
                        f"{stats['defense_count']}건 차단"
                    ),
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📝 아키텍트 AI 총평*\n{ai_comment}",
            },
        },
    ]


@pytest.mark.parametrize(
    ("available", "result", "expected_comment"),
    [
        (False, None, "AI 분석 환경이 준비되지 않았습니다."),
        (True, {"success": True, "output": "모델 총평", "error": None}, "모델 총평"),
        (
            True,
            {"success": False, "output": None, "error": "quota exceeded"},
            "AI 분석 중 오류 발생: quota exceeded",
        ),
    ],
    ids=["unprepared", "success", "failure"],
)
def test_daily_summary_blocks_and_ai_fallback_strings(
    available,
    result,
    expected_comment,
):
    stats = {
        "date": "2026-07-17",
        "win_rate": "50.0% (1승 1패)",
        "total_pnl": "+1.50%",
        "defense_count": 2,
    }
    ai_client = _FakeAIClient(available=available, result=result)
    notifier = _notifier_without_constructor(
        webhook_url="https://unused.invalid/webhook",
        ai_client=ai_client,
    )
    captured_blocks = []
    notifier.send_slack_blocks = lambda blocks: captured_blocks.append(blocks)

    assert notifier.send_daily_post_mortem(stats, "/fake/trade.csv") is None

    assert ai_client.availability_calls == 1
    assert ai_client.report_calls == (
        [(stats, "/fake/trade.csv")] if available else []
    )
    assert captured_blocks == [
        _expected_summary_blocks(stats, expected_comment)
    ]


class _RecordingUploader:
    instances = []

    def __init__(self, *, token, channel_id):
        self.token = token
        self.channel_id = channel_id
        self.csv_calls = []
        self.multiple_calls = []
        self.__class__.instances.append(self)

    def upload_csv(self, file_path, comment):
        self.csv_calls.append((file_path, comment))
        return True

    def upload_multiple_files(self, file_paths, comment):
        self.multiple_calls.append((list(file_paths), comment))
        return True


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 7, 17, 16, 5, 0)
        if tz is None:
            return value
        return value.replace(tzinfo=tz)


def test_webhook_missing_skips_summary_ai_but_pipeline_still_decides_telemetry(
    monkeypatch,
    tmp_path,
):
    events = []
    trade_csv = tmp_path / "trade.csv"
    minute_csv = tmp_path / "minute.csv"
    trade_csv.write_text("profit_rate,status\n1.0,진입\n", encoding="utf-8")
    minute_csv.write_text("time,price\n1000,100\n", encoding="utf-8")
    ai_client = _FakeAIClient(
        available=True,
        result={"success": True, "output": "must not be used", "error": None},
    )
    notifier = _notifier_without_constructor(
        webhook_url=None,
        ai_client=ai_client,
    )
    notifier.send_slack_blocks = lambda _blocks: pytest.fail(
        "a missing webhook must skip daily summary publication"
    )
    reporter = reporter_module.DailyReporter(notifier)

    _RecordingUploader.instances = []
    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(reporter_module, "SlackUploader", _RecordingUploader)
    monkeypatch.setattr(
        reporter_module.config,
        "CONFIG",
        {"slack_token": "fake-token", "slack_channel": "fake-channel"},
    )
    monkeypatch.setattr(
        reporter_module,
        "extract_and_save_1min_chart",
        lambda _date: events.append("minute") or [str(minute_csv)],
    )
    monkeypatch.setattr(
        reporter_module,
        "analyze_trade_efficiency",
        lambda _date: events.append("trade") or str(trade_csv),
    )

    reporter.run_pipeline("2026-07-17")

    assert events == ["minute", "trade"]
    assert ai_client.availability_calls == 0
    assert ai_client.report_calls == []
    uploader = _RecordingUploader.instances[0]
    assert (uploader.token, uploader.channel_id) == (
        "fake-token",
        "fake-channel",
    )
    assert uploader.csv_calls == [
        (
            str(trade_csv),
            "📊 *[20260717] V3.0 엔진 매매 분석 리포트*",
        )
    ]
    assert uploader.multiple_calls == [
        (
            [str(minute_csv)],
            "📈 *[20260717] 1분봉 백업 데이터 일괄 업로드 (1개 종목)*",
        )
    ]


@pytest.mark.parametrize(
    "system_config",
    [
        {},
        {"slack_token": "fake-token"},
        {"slack_channel": "fake-channel"},
    ],
    ids=["both-missing", "channel-missing", "token-missing"],
)
def test_telemetry_skips_when_token_or_channel_is_missing(
    monkeypatch,
    system_config,
):
    reporter = reporter_module.DailyReporter(object())
    monkeypatch.setattr(reporter_module.config, "CONFIG", system_config)
    monkeypatch.setattr(
        reporter_module,
        "SlackUploader",
        lambda **_kwargs: pytest.fail("Slack client must not be created"),
    )

    result = reporter.execute_slack_telemetry(
        "/fake/trade.csv",
        ["/fake/minute.csv"],
    )
    assert result is None


def test_telemetry_comments_and_date_are_exact(monkeypatch, tmp_path):
    trade_csv = tmp_path / "trade.csv"
    minute_files = [tmp_path / "minute-a.csv", tmp_path / "minute-b.csv"]
    trade_csv.write_text("trade\n", encoding="utf-8")
    for path in minute_files:
        path.write_text("minute\n", encoding="utf-8")

    _RecordingUploader.instances = []
    monkeypatch.setattr(reporter_module, "datetime", _FrozenDateTime)
    monkeypatch.setattr(reporter_module, "SlackUploader", _RecordingUploader)
    monkeypatch.setattr(
        reporter_module.config,
        "CONFIG",
        {"slack_token": "fake-token", "slack_channel": "fake-channel"},
    )
    reporter = reporter_module.DailyReporter(object())

    reporter.execute_slack_telemetry(
        str(trade_csv),
        [str(path) for path in minute_files],
    )

    uploader = _RecordingUploader.instances[0]
    assert uploader.csv_calls == [
        (str(trade_csv), "📊 *[20260717] V3.0 엔진 매매 분석 리포트*")
    ]
    assert uploader.multiple_calls == [
        (
            [str(path) for path in minute_files],
            "📈 *[20260717] 1분봉 백업 데이터 일괄 업로드 (2개 종목)*",
        )
    ]


class _FakeSlackWebClient:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def files_upload_v2(self, **kwargs):
        self.calls.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return {"ok": True}


def test_slack_uploader_missing_files_and_single_file_shape(
    monkeypatch,
    tmp_path,
):
    client = _FakeSlackWebClient()
    monkeypatch.setattr(notifier_module, "WebClient", lambda *, token: client)
    uploader = notifier_module.SlackUploader(
        token="fake-token",
        channel_id="C123",
    )
    missing = tmp_path / "missing.csv"

    assert uploader.upload_csv(str(missing), "comment") is False
    assert uploader.upload_multiple_files([str(missing)], "comment") is False
    assert client.calls == []

    present = tmp_path / "present.csv"
    present.write_text("a,b\n1,2\n", encoding="utf-8")
    assert uploader.upload_csv(str(present), "exact comment") is True
    assert client.calls == [
        {
            "channel": "C123",
            "initial_comment": "exact comment",
            "file": str(present),
            "title": "present.csv",
        }
    ]


def test_slack_multiple_upload_filters_missing_and_chunks_at_ten(
    monkeypatch,
    tmp_path,
):
    client = _FakeSlackWebClient(
        responses=[{"ok": True}, {"ok": False}, {"ok": True}],
    )
    monkeypatch.setattr(notifier_module, "WebClient", lambda *, token: client)
    uploader = notifier_module.SlackUploader(
        token="fake-token",
        channel_id="C123",
    )
    present = []
    for index in range(23):
        path = tmp_path / f"minute-{index:02d}.csv"
        path.write_text(f"row\n{index}\n", encoding="utf-8")
        present.append(path)
    requested = [
        str(tmp_path / "missing-before.csv"),
        *(str(path) for path in present),
    ]
    requested.append(str(tmp_path / "missing-after.csv"))

    assert uploader.upload_multiple_files(requested, "daily charts") is False

    assert [len(call["file_uploads"]) for call in client.calls] == [10, 10, 3]
    assert all(len(call["file_uploads"]) <= 10 for call in client.calls)
    assert [call["initial_comment"] for call in client.calls] == [
        "daily charts",
        "daily charts (이어서 계속...)",
        "daily charts (이어서 계속...)",
    ]
    flattened = [
        upload
        for call in client.calls
        for upload in call["file_uploads"]
    ]
    assert flattened == [
        {"file": str(path), "title": path.name}
        for path in present
    ]
    assert {call["channel"] for call in client.calls} == {"C123"}
