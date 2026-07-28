"""Tests for the pure post-market report use case and business rules."""

from dataclasses import FrozenInstanceError
import importlib
import math
from pathlib import Path
import socket

import pytest

from kiwoom_stock.application.reporting import (
    DailyReportRequest,
    DailyReportResult,
    DailyReportStats,
    NarrationResult,
    NarrationStatus,
    PostMarketReportUseCase,
    ReportArtifact,
    ReportStageResult,
    analyze_trade_rows,
    calculate_daily_stats,
    classify_trade_judgement,
    default_daily_stats,
    select_primary_driver,
)


TARGET_DATE = "2026-07-17"
REPORT_DATE = "2026-07-18"
_DEFAULT_NARRATION = object()


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    def fail_network(*_args, **_kwargs):
        pytest.fail("pure reporting test attempted network access")

    monkeypatch.setattr(socket, "socket", fail_network)
    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(socket, "getaddrinfo", fail_network)


def _trade_row(*, stock_code="005930", stock_name="Sample", **overrides):
    row = {
        "stock_code": stock_code,
        "stock_name": stock_name,
        "thrust": -1.0,
        "gravity": -1.0,
        "drag": -1.0,
        "magnetic": -1.0,
        "jerk": -1.0,
        "impulse": -1.0,
        "net_force": 0.0,
        "profit_rate": 0.0,
        "status": "CLOSED",
    }
    row.update(overrides)
    return row


def _stage_pairs(result):
    return tuple((stage.stage, stage.status) for stage in result.stages)


class _DataSource:
    def __init__(self, events, rows, error=None):
        self.events = events
        self.rows = rows
        self.error = error

    def load_trades(self, target_date):
        self.events.append(("data", target_date))
        if self.error is not None:
            raise self.error
        return self.rows


class _MinuteSource:
    def __init__(self, events, rows_by_code=None, error=None):
        self.events = events
        self.rows_by_code = rows_by_code or {}
        self.error = error

    def load_minutes(self, stock_code, target_date):
        self.events.append(("minutes", stock_code, target_date))
        if self.error is not None:
            raise self.error
        return self.rows_by_code.get(stock_code, ())


class _ArtifactStore:
    def __init__(
        self,
        events,
        fail_stage=None,
        none_stage=None,
        invalid_stage=None,
    ):
        self.events = events
        self.fail_stage = fail_stage
        self.none_stage = none_stage
        self.invalid_stage = invalid_stage
        self.minute_calls = []
        self.trade_calls = []

    def save_minute_chart(
        self,
        *,
        stock_code,
        stock_name,
        target_date,
        rows,
    ):
        self.events.append(("minute_store", stock_code))
        self.minute_calls.append(
            (stock_code, stock_name, target_date, tuple(rows))
        )
        if self.fail_stage == "minute":
            raise RuntimeError("minute store failed")
        if self.none_stage == "minute":
            return None
        if self.invalid_stage == "minute":
            return object()
        return ReportArtifact(
            kind="minute_chart",
            logical_name=f"{stock_name}_{stock_code}_1min_{target_date}.csv",
            reference=f"minute:{stock_code}",
        )

    def save_trade_analysis(self, *, target_date, rows):
        self.events.append(("trade_store", target_date))
        self.trade_calls.append((target_date, tuple(rows)))
        if self.fail_stage == "trade":
            raise RuntimeError("trade store failed")
        if self.none_stage == "trade":
            return None
        if self.invalid_stage == "trade":
            return object()
        return ReportArtifact(
            kind="trade_analysis",
            logical_name=f"physics_trade_analysis_{target_date}.csv",
            reference="trade:analysis",
        )


class _Narrator:
    def __init__(self, events, result, error=None):
        self.events = events
        self.result = result
        self.error = error
        self.calls = []

    def narrate(self, *, request, stats, trade_artifact):
        self.events.append(("narrative", trade_artifact))
        self.calls.append((request, stats, trade_artifact))
        if self.error is not None:
            raise self.error
        return self.result


class _Publisher:
    def __init__(
        self,
        events,
        *,
        enabled=True,
        summary_result=True,
        telemetry_result=True,
        summary_error=None,
        telemetry_error=None,
    ):
        self.events = events
        self.enabled = enabled
        self.summary_result = summary_result
        self.telemetry_result = telemetry_result
        self.summary_error = summary_error
        self.telemetry_error = telemetry_error
        self.summary_calls = []
        self.telemetry_calls = []

    def summary_enabled(self):
        self.events.append(("summary_enabled", self.enabled))
        return self.enabled

    def publish_summary(
        self,
        *,
        request,
        stats,
        narrative,
        trade_artifact,
    ):
        self.events.append(("summary", narrative))
        self.summary_calls.append(
            (request, stats, narrative, trade_artifact)
        )
        if self.summary_error is not None:
            raise self.summary_error
        return self.summary_result

    def publish_telemetry(
        self,
        *,
        request,
        trade_artifact,
        minute_artifacts,
    ):
        self.events.append(("telemetry", len(minute_artifacts)))
        self.telemetry_calls.append(
            (request, trade_artifact, tuple(minute_artifacts))
        )
        if self.telemetry_error is not None:
            raise self.telemetry_error
        return self.telemetry_result


def _use_case(
    *,
    rows=None,
    minute_rows=None,
    data_error=None,
    minute_error=None,
    store_failure=None,
    store_none=None,
    store_invalid=None,
    narration_result=_DEFAULT_NARRATION,
    narrator_error=None,
    publisher_options=None,
):
    events = []
    data_source = _DataSource(events, rows or (), data_error)
    minute_source = _MinuteSource(events, minute_rows, minute_error)
    store = _ArtifactStore(
        events,
        store_failure,
        store_none,
        store_invalid,
    )
    if narration_result is _DEFAULT_NARRATION:
        narration_result = NarrationResult.succeeded("모델 총평")
    narrator = _Narrator(events, narration_result, narrator_error)
    publisher = _Publisher(events, **(publisher_options or {}))
    use_case = PostMarketReportUseCase(
        data_source=data_source,
        minute_source=minute_source,
        artifact_store=store,
        narrator=narrator,
        publisher=publisher,
    )
    return use_case, events, store, narrator, publisher


def test_report_dtos_are_immutable_and_dates_are_explicit():
    request = DailyReportRequest(TARGET_DATE, REPORT_DATE)
    with pytest.raises(FrozenInstanceError):
        request.target_date = "2026-07-19"

    with pytest.raises(ValueError, match="target_date must use YYYY-MM-DD"):
        DailyReportRequest("20260717", REPORT_DATE)
    with pytest.raises(ValueError, match="report_date must use YYYY-MM-DD"):
        DailyReportRequest(TARGET_DATE, "2026-7-18")

    artifact = ReportArtifact("trade", "trade.csv", "opaque-reference")
    stage = ReportStageResult("trade_analysis", "succeeded")
    result = DailyReportResult((stage,), (artifact,), None, False)
    with pytest.raises(FrozenInstanceError):
        result.requires_attention = True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": NarrationStatus.SUCCEEDED},
        {
            "status": NarrationStatus.SUCCEEDED,
            "output": "ok",
            "error_detail": "error",
        },
        {
            "status": NarrationStatus.UNAVAILABLE,
            "output": "not allowed",
        },
        {
            "status": NarrationStatus.UNAVAILABLE,
            "error_detail": "not allowed",
        },
        {"status": NarrationStatus.FAILED},
        {"status": NarrationStatus.FAILED, "error_detail": ""},
    ],
)
def test_narration_result_rejects_illegal_field_combinations(kwargs):
    with pytest.raises(ValueError):
        NarrationResult(**kwargs)


def test_narration_result_keeps_empty_string_as_explicit_success():
    result = NarrationResult.succeeded("")

    assert result == NarrationResult(
        status=NarrationStatus.SUCCEEDED,
        output="",
    )


def test_other_dto_invariants_reject_invalid_values():
    with pytest.raises(ValueError, match="status is invalid"):
        ReportStageResult("stats", "unknown")
    with pytest.raises(ValueError, match="reference must not be empty"):
        ReportArtifact("trade", "trade.csv", "")
    failed = ReportStageResult("stats", "failed", "parse failed")
    with pytest.raises(ValueError, match="failed_stage"):
        DailyReportResult((failed,), (), None, False)


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("thrust", "Thrust"),
        ("gravity", "Gravity"),
        ("drag", "Drag"),
        ("magnetic", "Magnetic"),
        ("jerk", "Jerk"),
        ("impulse", "Impulse"),
    ],
)
def test_primary_driver_preserves_force_labels_and_candidate_order(
    field,
    expected,
):
    assert select_primary_driver(_trade_row(**{field: 3.0})) == expected


def test_primary_driver_uses_first_tie_and_none_without_positive_force():
    assert select_primary_driver(
        _trade_row(thrust=2.0, gravity=2.0, drag=1.0)
    ) == "Thrust"
    assert select_primary_driver(_trade_row()) == "None"


@pytest.mark.parametrize(
    ("profit", "net_force", "expected"),
    [
        (2.0, 9.0, "➖ 보합(마찰 상쇄)"),
        (-2.0, 9.0, "➖ 보합(마찰 상쇄)"),
        (2.01, 1.0, "🎯 정밀타격"),
        (2.01, 0.99, "🤔 요행(가속부족)"),
        (-2.01, 1.0, "❌ 엔진과열(오판)"),
        (-2.01, 0.99, "⚠️ 억지진입(동력부족)"),
    ],
)
def test_judgement_preserves_strict_boundaries_and_exact_strings(
    profit,
    net_force,
    expected,
):
    assert classify_trade_judgement(
        profit_rate=profit,
        net_force=net_force,
    ) == expected


def test_analyzed_rows_copy_original_order_then_append_analysis_fields():
    source = _trade_row(thrust=3.0, profit_rate=2.01, net_force=1.0)
    analyzed = analyze_trade_rows([source])

    assert tuple(analyzed[0]) == tuple(source) + (
        "primary_driver",
        "judgement",
    )
    assert analyzed[0]["primary_driver"] == "Thrust"
    assert analyzed[0]["judgement"] == "🎯 정밀타격"
    assert "primary_driver" not in source


def test_stats_preserve_win_pnl_defense_formula_and_defaults():
    rows = [
        _trade_row(profit_rate=2.0, status="진입"),
        _trade_row(profit_rate=0.0, status="수급 차단"),
        _trade_row(profit_rate=-1.25, status=None),
        _trade_row(profit_rate=3.375, status="차단 후 해제"),
        _trade_row(profit_rate=math.nan, status="진입"),
    ]

    assert calculate_daily_stats(rows) == DailyReportStats(
        win_rate="40.0% (2승 3패)",
        total_pnl="+4.12%",
        defense_count=2,
    )
    assert calculate_daily_stats([]) == DailyReportStats(
        win_rate="0.0% (0승 0패)",
        total_pnl="+0.00%",
        defense_count=0,
    )
    assert default_daily_stats() == DailyReportStats("N/A", 0.0, 0)


def test_use_case_success_uses_explicit_dates_and_five_ports():
    first = _trade_row(thrust=3.0, profit_rate=2.5, status="진입")
    duplicate = dict(first)
    second = _trade_row(
        stock_code="000660",
        stock_name="Second",
        profit_rate=-1.0,
        status="차단",
    )
    use_case, events, store, narrator, publisher = _use_case(
        rows=[first, duplicate, second],
        minute_rows={
            "005930": [{"time": "1000", "price": 100}],
            "000660": [],
        },
    )
    request = DailyReportRequest(TARGET_DATE, REPORT_DATE)

    result = use_case.execute(request)

    assert _stage_pairs(result) == (
        ("minute_chart", "succeeded"),
        ("trade_analysis", "succeeded"),
        ("stats", "succeeded"),
        ("narrative", "succeeded"),
        ("summary", "succeeded"),
        ("telemetry", "succeeded"),
    )
    assert result.failed_stage is None
    assert result.requires_attention is False
    assert tuple(item.kind for item in result.artifacts) == (
        "minute_chart",
        "trade_analysis",
    )
    trade_artifact = ReportArtifact(
        kind="trade_analysis",
        logical_name="physics_trade_analysis_2026-07-17.csv",
        reference="trade:analysis",
    )
    assert events == [
        ("data", TARGET_DATE),
        ("minutes", "005930", TARGET_DATE),
        ("minute_store", "005930"),
        ("minutes", "000660", TARGET_DATE),
        ("trade_store", TARGET_DATE),
        ("summary_enabled", True),
        ("narrative", trade_artifact),
        ("summary", "모델 총평"),
        ("telemetry", 1),
    ]
    assert store.minute_calls[0][2] == TARGET_DATE
    assert store.trade_calls[0][0] == TARGET_DATE
    assert narrator.calls[0][0] is request
    assert narrator.calls[0][1] == DailyReportStats(
        win_rate="66.7% (2승 1패)",
        total_pnl="+4.00%",
        defense_count=1,
    )
    assert publisher.summary_calls[0][0] is request
    assert publisher.telemetry_calls[0][0] is request
    assert request.report_date == REPORT_DATE


def test_minute_targets_use_truthy_code_name_pair_deduplication():
    rows = [
        _trade_row(stock_code="005930", stock_name="Original"),
        _trade_row(stock_code="005930", stock_name="Original"),
        _trade_row(stock_code="005930", stock_name="Alias"),
        _trade_row(stock_code="", stock_name="Blank code"),
        _trade_row(stock_code="000660", stock_name=""),
    ]
    use_case, _events, store, _narrator, _publisher = _use_case(
        rows=rows,
        minute_rows={"005930": [{"time": "1000"}]},
    )

    use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert [call[:2] for call in store.minute_calls] == [
        ("005930", "Original"),
        ("005930", "Alias"),
    ]


def test_data_source_failure_stops_every_following_port_call():
    use_case, events, store, narrator, publisher = _use_case(
        data_error=RuntimeError("data failed"),
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result) == (("minute_chart", "failed"),)
    assert result.stages[0].detail == "data failed"
    assert events == [("data", TARGET_DATE)]
    assert store.minute_calls == []
    assert store.trade_calls == []
    assert narrator.calls == []
    assert publisher.summary_calls == []
    assert publisher.telemetry_calls == []


def test_minute_artifact_failure_stops_every_later_stage():
    use_case, events, store, narrator, publisher = _use_case(
        rows=[_trade_row()],
        minute_rows={"005930": [{"time": "1000"}]},
        store_failure="minute",
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result) == (("minute_chart", "failed"),)
    assert result.stages[0].detail == "minute store failed"
    assert [event[0] for event in events] == [
        "data",
        "minutes",
        "minute_store",
    ]
    assert len(store.minute_calls) == 1
    assert store.trade_calls == []
    assert narrator.calls == []
    assert publisher.summary_calls == []
    assert publisher.telemetry_calls == []


@pytest.mark.parametrize(
    ("invalid_stage", "expected_stages"),
    [
        ("minute", (("minute_chart", "failed"),)),
        (
            "trade",
            (
                ("minute_chart", "succeeded"),
                ("trade_analysis", "failed"),
            ),
        ),
    ],
)
def test_invalid_artifact_return_is_typed_and_stops_following_calls(
    invalid_stage,
    expected_stages,
):
    use_case, _events, store, narrator, publisher = _use_case(
        rows=[_trade_row()],
        minute_rows={"005930": [{"time": "1000"}]},
        store_invalid=invalid_stage,
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result) == expected_stages
    assert result.stages[-1].detail == (
        "report artifact store must return ReportArtifact or None"
    )
    if invalid_stage == "minute":
        assert store.trade_calls == []
    else:
        assert len(store.trade_calls) == 1
    assert narrator.calls == []
    assert publisher.summary_calls == []
    assert publisher.telemetry_calls == []


def test_nonempty_rows_with_no_trade_artifact_keep_default_stats():
    use_case, _events, _store, narrator, publisher = _use_case(
        rows=[_trade_row(profit_rate=3.0, thrust=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        store_none="trade",
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[1:3] == (
        ("trade_analysis", "skipped"),
        ("stats", "skipped"),
    )
    assert narrator.calls[0][1] == default_daily_stats()
    assert narrator.calls[0][2] is None
    assert publisher.summary_calls[0][1] == default_daily_stats()
    assert publisher.summary_calls[0][3] is None
    assert publisher.telemetry_calls[0][1] is None


@pytest.mark.parametrize(
    ("failure", "expected_stages"),
    [
        ("minute", (("minute_chart", "failed"),)),
        (
            "trade",
            (
                ("minute_chart", "succeeded"),
                ("trade_analysis", "failed"),
            ),
        ),
        (
            "summary",
            (
                ("minute_chart", "succeeded"),
                ("trade_analysis", "succeeded"),
                ("stats", "succeeded"),
                ("narrative", "succeeded"),
                ("summary", "failed"),
            ),
        ),
    ],
)
def test_unexpected_minute_trade_or_summary_failure_stops_later_stages(
    failure,
    expected_stages,
):
    options = (
        {"summary_error": RuntimeError("summary failed")}
        if failure == "summary"
        else None
    )
    use_case, events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        minute_error=(
            RuntimeError("minute failed") if failure == "minute" else None
        ),
        store_failure="trade" if failure == "trade" else None,
        publisher_options=options,
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result) == expected_stages
    assert result.failed_stage == {
        "minute": "minute_chart",
        "trade": "trade_analysis",
        "summary": "summary",
    }[failure]
    assert result.requires_attention is True
    assert publisher.telemetry_calls == []
    assert not any(event[0] == "telemetry" for event in events)


def test_stats_failure_uses_defaults_and_continues(monkeypatch):
    reporting_module = importlib.import_module(
        "kiwoom_stock.application.reporting"
    )

    def fail_stats(_rows):
        raise RuntimeError("stats failed")

    monkeypatch.setattr(reporting_module, "calculate_daily_stats", fail_stats)
    use_case, _events, _store, narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[2:] == (
        ("stats", "failed"),
        ("narrative", "succeeded"),
        ("summary", "succeeded"),
        ("telemetry", "succeeded"),
    )
    assert result.failed_stage == "stats"
    assert result.requires_attention is True
    assert narrator.calls[0][0].report_date == REPORT_DATE
    assert narrator.calls[0][1] == default_daily_stats()
    assert publisher.summary_calls[0][1] == default_daily_stats()


def test_narrator_failure_uses_exact_fallback_and_continues():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        narration_result=NarrationResult.failed("quota exceeded"),
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[3:] == (
        ("narrative", "failed"),
        ("summary", "succeeded"),
        ("telemetry", "succeeded"),
    )
    assert result.failed_stage == "narrative"
    assert publisher.summary_calls[0][2] == (
        "AI 분석 중 오류 발생: quota exceeded"
    )


def test_narrator_unavailable_uses_exact_skip_fallback_and_continues():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        narration_result=NarrationResult.unavailable(),
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[3:] == (
        ("narrative", "skipped"),
        ("summary", "succeeded"),
        ("telemetry", "succeeded"),
    )
    assert result.stages[3].detail == (
        "AI 분석 환경이 준비되지 않았습니다."
    )
    assert publisher.summary_calls[0][2] == (
        "AI 분석 환경이 준비되지 않았습니다."
    )
    assert result.failed_stage is None
    assert result.requires_attention is False


def test_empty_narration_output_remains_success_and_is_published():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        narration_result=NarrationResult.succeeded(""),
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[3] == ("narrative", "succeeded")
    assert publisher.summary_calls[0][2] == ""


def test_invalid_none_narration_cannot_be_misclassified_as_unavailable():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        narration_result=None,
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[3] == ("narrative", "failed")
    assert result.stages[3].detail == (
        "unexpected narrator error (TypeError)"
    )
    assert publisher.summary_calls[0][2] == (
        "AI 분석 중 오류 발생: unexpected narrator error (TypeError)"
    )


def test_unexpected_narrator_exception_does_not_leak_raw_detail():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        narrator_error=RuntimeError("SECRET raw provider response"),
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert result.stages[3].detail == (
        "unexpected narrator error (RuntimeError)"
    )
    assert publisher.summary_calls[0][2] == (
        "AI 분석 중 오류 발생: unexpected narrator error (RuntimeError)"
    )
    assert "SECRET" not in repr(result)
    assert "SECRET" not in repr(publisher.summary_calls)


def test_summary_false_is_typed_skip_and_telemetry_continues():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        publisher_options={"summary_result": False},
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[-2:] == (
        ("summary", "skipped"),
        ("telemetry", "succeeded"),
    )
    assert len(publisher.summary_calls) == 1
    assert len(publisher.telemetry_calls) == 1
    assert result.requires_attention is False


def test_disabled_summary_skips_narrator_but_continues_telemetry():
    use_case, _events, _store, narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        publisher_options={"enabled": False},
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[3:] == (
        ("narrative", "skipped"),
        ("summary", "skipped"),
        ("telemetry", "succeeded"),
    )
    assert narrator.calls == []
    assert publisher.summary_calls == []
    assert len(publisher.telemetry_calls) == 1
    assert result.requires_attention is False


def test_telemetry_skip_is_typed_without_attention():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        publisher_options={"telemetry_result": False},
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[-1] == ("telemetry", "skipped")
    assert len(publisher.telemetry_calls) == 1
    assert result.failed_stage is None
    assert result.requires_attention is False


def test_telemetry_exception_is_a_typed_terminal_stage_failure():
    use_case, _events, _store, _narrator, publisher = _use_case(
        rows=[_trade_row(thrust=1.0, profit_rate=1.0)],
        minute_rows={"005930": [{"time": "1000"}]},
        publisher_options={
            "telemetry_error": RuntimeError("telemetry failed"),
        },
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[-1] == ("telemetry", "failed")
    assert result.stages[-1].detail == "telemetry failed"
    assert result.failed_stage == "telemetry"
    assert result.requires_attention is True
    assert len(publisher.telemetry_calls) == 1


def test_summary_configuration_exception_is_contained_as_summary_failure():
    class FailingPublisher(_Publisher):
        def summary_enabled(self):
            raise RuntimeError("summary config failed")

    events = []
    publisher = FailingPublisher(events)
    use_case = PostMarketReportUseCase(
        data_source=_DataSource(
            events,
            [_trade_row(thrust=1.0, profit_rate=1.0)],
        ),
        minute_source=_MinuteSource(
            events,
            {"005930": [{"time": "1000"}]},
        ),
        artifact_store=_ArtifactStore(events),
        narrator=_Narrator(
            events,
            NarrationResult.succeeded("모델 총평"),
        ),
        publisher=publisher,
    )

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result)[-1] == ("summary", "failed")
    assert result.stages[-1].detail == "summary config failed"
    assert result.failed_stage == "summary"
    assert publisher.summary_calls == []
    assert publisher.telemetry_calls == []


def test_no_data_uses_defaults_without_artifacts_and_still_reports():
    use_case, events, store, narrator, publisher = _use_case(rows=[])

    result = use_case.execute(DailyReportRequest(TARGET_DATE, REPORT_DATE))

    assert _stage_pairs(result) == (
        ("minute_chart", "skipped"),
        ("trade_analysis", "skipped"),
        ("stats", "skipped"),
        ("narrative", "succeeded"),
        ("summary", "succeeded"),
        ("telemetry", "succeeded"),
    )
    assert result.artifacts == ()
    assert store.minute_calls == []
    assert store.trade_calls == []
    assert narrator.calls == [(
        DailyReportRequest(TARGET_DATE, REPORT_DATE),
        default_daily_stats(),
        None,
    )]
    assert publisher.summary_calls[0][1] == default_daily_stats()
    assert publisher.telemetry_calls[0][1:] == (None, ())
    assert events[0] == ("data", TARGET_DATE)


def test_application_reporting_has_no_infrastructure_or_clock_imports():
    source = Path(
        "src/kiwoom_stock/application/reporting.py"
    ).read_text(encoding="utf-8")
    forbidden = (
        "requests",
        "boto3",
        "slack_sdk",
        "google",
        "pandas",
        "sqlite3",
        "TradeLogger",
        "pathlib",
        "os.getenv",
        "datetime.now",
        "date.today",
    )
    assert [name for name in forbidden if name in source] == []
