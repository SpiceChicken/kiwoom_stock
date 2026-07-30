"""Pure post-market reporting rules and application orchestration."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
import math
from typing import Any, Mapping, Optional, Sequence, Tuple, cast

from kiwoom_stock.application.ports import (
    MinuteChartSource,
    ReportArtifactStore,
    ReportDataSource,
    ReportNarrator,
    ReportPublisher,
)


_FORCE_FIELDS = (
    ("thrust", "Thrust"),
    ("gravity", "Gravity"),
    ("drag", "Drag"),
    ("magnetic", "Magnetic"),
    ("jerk", "Jerk"),
    ("impulse", "Impulse"),
)
_STAGE_STATUSES = frozenset({"succeeded", "skipped", "failed"})
_NARRATION_UNAVAILABLE = "AI 분석 환경이 준비되지 않았습니다."


@dataclass(frozen=True)
class DailyReportRequest:
    """Explicit date-only inputs for one post-market report run."""

    target_date: str
    report_date: str

    def __post_init__(self) -> None:
        _validate_iso_date(self.target_date, field_name="target_date")
        _validate_iso_date(self.report_date, field_name="report_date")


@dataclass(frozen=True)
class DailyReportStats:
    """Legacy display-ready report statistics."""

    win_rate: str
    total_pnl: float | str
    defense_count: int


class NarrationStatus(str, Enum):
    """Explicit narrator outcomes before summary presentation."""

    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True)
class NarrationResult:
    """SDK-neutral narration result with validated three-state fields."""

    status: NarrationStatus
    output: Optional[str] = None
    error_detail: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, NarrationStatus):
            raise TypeError("narration status must be NarrationStatus")
        if self.status is NarrationStatus.SUCCEEDED:
            if not isinstance(self.output, str):
                raise ValueError("successful narration requires string output")
            if self.error_detail is not None:
                raise ValueError(
                    "successful narration cannot include an error"
                )
            return
        if self.output is not None:
            raise ValueError("non-success narration cannot include output")
        if self.status is NarrationStatus.UNAVAILABLE:
            if self.error_detail is not None:
                raise ValueError(
                    "unavailable narration cannot include an error"
                )
            return
        if not isinstance(self.error_detail, str) or not self.error_detail:
            raise ValueError("failed narration requires safe error detail")

    @classmethod
    def succeeded(cls, output: str) -> "NarrationResult":
        return cls(status=NarrationStatus.SUCCEEDED, output=output)

    @classmethod
    def unavailable(cls) -> "NarrationResult":
        return cls(status=NarrationStatus.UNAVAILABLE)

    @classmethod
    def failed(cls, error_detail: str) -> "NarrationResult":
        return cls(
            status=NarrationStatus.FAILED,
            error_detail=error_detail,
        )


@dataclass(frozen=True)
class ReportArtifact:
    """Logical artifact whose reference is interpreted only by adapters."""

    kind: str
    logical_name: str
    reference: str

    def __post_init__(self) -> None:
        for field_name, value in (
            ("kind", self.kind),
            ("logical_name", self.logical_name),
            ("reference", self.reference),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"report artifact {field_name} must not be empty"
                )


@dataclass(frozen=True)
class ReportStageResult:
    """Observable status for one application reporting stage."""

    stage: str
    status: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.stage:
            raise ValueError("report stage must not be empty")
        if self.status not in _STAGE_STATUSES:
            raise ValueError("report stage status is invalid")


@dataclass(frozen=True)
class DailyReportResult:
    """Immutable aggregate result; failures are observable, not raised."""

    stages: Tuple[ReportStageResult, ...]
    artifacts: Tuple[ReportArtifact, ...]
    failed_stage: Optional[str]
    requires_attention: bool

    def __post_init__(self) -> None:
        if not isinstance(self.stages, tuple):
            raise TypeError("report stages must be a tuple")
        if not isinstance(self.artifacts, tuple):
            raise TypeError("report artifacts must be a tuple")
        if not all(
            isinstance(stage, ReportStageResult) for stage in self.stages
        ):
            raise TypeError(
                "report stages must contain ReportStageResult values"
            )
        if not all(
            isinstance(item, ReportArtifact) for item in self.artifacts
        ):
            raise TypeError(
                "report artifacts must contain ReportArtifact values"
            )
        first_failure = next(
            (stage.stage for stage in self.stages if stage.status == "failed"),
            None,
        )
        if self.failed_stage != first_failure:
            raise ValueError(
                "failed_stage must identify the first failed stage"
            )
        if self.requires_attention is not (first_failure is not None):
            raise ValueError(
                "requires_attention must match failed stage presence"
            )


def select_primary_driver(row: Mapping[str, Any]) -> str:
    """Return the first maximum positive legacy force label."""

    positive: list[tuple[str, float]] = []
    for field_name, label in _FORCE_FIELDS:
        value = cast(float, row[field_name])
        if value > 0:
            positive.append((label, value))
    if not positive:
        return "None"
    return max(positive, key=lambda item: item[1])[0]


def classify_trade_judgement(*, profit_rate: float, net_force: float) -> str:
    """Return the byte-stable legacy judgement for profit and net force."""

    if profit_rate > 2.0:
        return "🎯 정밀타격" if net_force >= 1.0 else "🤔 요행(가속부족)"
    if profit_rate < -2.0:
        return "❌ 엔진과열(오판)" if net_force >= 1.0 else "⚠️ 억지진입(동력부족)"
    return "➖ 보합(마찰 상쇄)"


def analyze_trade_rows(
    rows: Sequence[Mapping[str, Any]],
) -> Tuple[Mapping[str, Any], ...]:
    """Copy rows and append the two legacy analysis fields in exact order."""

    analyzed = []
    for row in rows:
        item = dict(row)
        item["primary_driver"] = select_primary_driver(row)
        item["judgement"] = classify_trade_judgement(
            profit_rate=cast(float, row["profit_rate"]),
            net_force=cast(float, row["net_force"]),
        )
        analyzed.append(item)
    return tuple(analyzed)


def default_daily_stats() -> DailyReportStats:
    """Return the exact legacy defaults used when no stats are available."""

    return DailyReportStats(
        win_rate="N/A",
        total_pnl=0.0,
        defense_count=0,
    )


def calculate_daily_stats(
    rows: Sequence[Mapping[str, Any]],
) -> DailyReportStats:
    """Calculate the legacy display statistics without external libraries."""

    wins = 0
    total_pnl = 0.0
    defense_count = 0
    for row in rows:
        profit = cast(Optional[float], row["profit_rate"])
        if profit is not None and not _is_nan(profit):
            if profit > 0:
                wins += 1
            total_pnl += profit
        status = row.get("status")
        if isinstance(status, str) and "차단" in status:
            defense_count += 1

    total = len(rows)
    win_rate = wins / total * 100 if total > 0 else 0.0
    return DailyReportStats(
        win_rate=f"{win_rate:.1f}% ({wins}승 {total - wins}패)",
        total_pnl=f"{total_pnl:+.2f}%",
        defense_count=defense_count,
    )


class PostMarketReportUseCase:
    """Coordinate report stages through five narrow infrastructure ports."""

    def __init__(
        self,
        *,
        data_source: ReportDataSource,
        minute_source: MinuteChartSource,
        artifact_store: ReportArtifactStore,
        narrator: ReportNarrator,
        publisher: ReportPublisher,
    ) -> None:
        self._data_source = data_source
        self._minute_source = minute_source
        self._artifact_store = artifact_store
        self._narrator = narrator
        self._publisher = publisher

    def execute(self, request: DailyReportRequest) -> DailyReportResult:
        """Run the deterministic stage policy for an explicit request."""

        stages: list[ReportStageResult] = []
        artifacts: list[ReportArtifact] = []
        minute_artifacts: list[ReportArtifact] = []

        try:
            trades, minute_stage = self._collect_minute_artifacts(
                request,
                artifacts,
                minute_artifacts,
            )
            stages.append(minute_stage)
        except Exception as error:
            stages.append(_failed_stage("minute_chart", error))
            return _result(stages, artifacts)

        try:
            analyzed_rows, trade_artifact, trade_stage = (
                self._create_trade_artifact(
                    request,
                    trades,
                    artifacts,
                )
            )
            stages.append(trade_stage)
        except Exception as error:
            stages.append(_failed_stage("trade_analysis", error))
            return _result(stages, artifacts)

        stats, stats_stage = self._create_stats(
            analyzed_rows,
            trade_artifact,
        )
        stages.append(stats_stage)

        summary_stages, continue_to_telemetry = self._run_summary(
            request,
            stats,
            trade_artifact,
        )
        stages.extend(summary_stages)
        if not continue_to_telemetry:
            return _result(stages, artifacts)

        stages.append(
            self._run_telemetry(
                request,
                trade_artifact,
                minute_artifacts,
            )
        )

        return _result(stages, artifacts)

    def _collect_minute_artifacts(
        self,
        request: DailyReportRequest,
        artifacts: list[ReportArtifact],
        minute_artifacts: list[ReportArtifact],
    ) -> tuple[Tuple[Mapping[str, Any], ...], ReportStageResult]:
        trades = tuple(
            dict(row)
            for row in self._data_source.load_trades(request.target_date)
        )
        seen_targets: set[tuple[str, str]] = set()
        for trade in trades:
            stock_code = cast(str, trade["stock_code"])
            stock_name = cast(str, trade["stock_name"])
            if not stock_code or not stock_name:
                continue
            target = (stock_code, stock_name)
            if target in seen_targets:
                continue
            seen_targets.add(target)
            minute_rows = tuple(
                dict(row)
                for row in self._minute_source.load_minutes(
                    stock_code,
                    request.target_date,
                )
            )
            if not minute_rows:
                continue
            artifact = self._artifact_store.save_minute_chart(
                stock_code=stock_code,
                stock_name=stock_name,
                target_date=request.target_date,
                rows=minute_rows,
            )
            if artifact is not None:
                _require_artifact(artifact)
                minute_artifacts.append(artifact)
                artifacts.append(artifact)
        stage = _stage(
            "minute_chart",
            "succeeded" if minute_artifacts else "skipped",
            None if minute_artifacts else "no minute artifacts",
        )
        return trades, stage

    def _create_trade_artifact(
        self,
        request: DailyReportRequest,
        trades: Sequence[Mapping[str, Any]],
        artifacts: list[ReportArtifact],
    ) -> tuple[
        Tuple[Mapping[str, Any], ...],
        Optional[ReportArtifact],
        ReportStageResult,
    ]:
        analyzed_rows = analyze_trade_rows(trades)
        trade_artifact: Optional[ReportArtifact] = None
        if analyzed_rows:
            trade_artifact = self._artifact_store.save_trade_analysis(
                target_date=request.target_date,
                rows=analyzed_rows,
            )
            if trade_artifact is not None:
                _require_artifact(trade_artifact)
                artifacts.append(trade_artifact)
        stage = _stage(
            "trade_analysis",
            "succeeded" if trade_artifact is not None else "skipped",
            None if trade_artifact is not None else "no trade artifact",
        )
        return analyzed_rows, trade_artifact, stage

    def _create_stats(
        self,
        analyzed_rows: Sequence[Mapping[str, Any]],
        trade_artifact: Optional[ReportArtifact],
    ) -> tuple[DailyReportStats, ReportStageResult]:
        if trade_artifact is None:
            return (
                default_daily_stats(),
                _stage("stats", "skipped", "using default statistics"),
            )
        try:
            return calculate_daily_stats(analyzed_rows), _stage(
                "stats",
                "succeeded",
            )
        except Exception as error:
            return default_daily_stats(), _failed_stage("stats", error)

    def _run_summary(
        self,
        request: DailyReportRequest,
        stats: DailyReportStats,
        trade_artifact: Optional[ReportArtifact],
    ) -> tuple[Tuple[ReportStageResult, ...], bool]:
        try:
            summary_enabled = self._publisher.summary_enabled()
        except Exception as error:
            return (_failed_stage("summary", error),), False

        if not summary_enabled:
            return (
                _stage("narrative", "skipped", "summary disabled"),
                _stage("summary", "skipped", "summary disabled"),
            ), True

        narrative, narrative_stage = self._resolve_narrative(
            request,
            stats,
            trade_artifact,
        )
        try:
            published = self._publisher.publish_summary(
                request=request,
                stats=stats,
                narrative=narrative,
                trade_artifact=trade_artifact,
            )
        except Exception as error:
            return (
                narrative_stage,
                _failed_stage("summary", error),
            ), False
        return (
            narrative_stage,
            _stage(
                "summary",
                "succeeded" if published else "skipped",
                None if published else "summary publisher skipped",
            ),
        ), True

    def _resolve_narrative(
        self,
        request: DailyReportRequest,
        stats: DailyReportStats,
        trade_artifact: Optional[ReportArtifact],
    ) -> tuple[str, ReportStageResult]:
        try:
            narration = self._narrator.narrate(
                request=request,
                stats=stats,
                trade_artifact=trade_artifact,
            )
            if not isinstance(narration, NarrationResult):
                raise TypeError("report narrator must return NarrationResult")
            if narration.status is NarrationStatus.UNAVAILABLE:
                return _NARRATION_UNAVAILABLE, _stage(
                    "narrative",
                    "skipped",
                    _NARRATION_UNAVAILABLE,
                )
            if narration.status is NarrationStatus.FAILED:
                assert narration.error_detail is not None
                return (
                    f"AI 분석 중 오류 발생: {narration.error_detail}",
                    _stage("narrative", "failed", narration.error_detail),
                )
            assert narration.output is not None
            return narration.output, _stage("narrative", "succeeded")
        except Exception as error:
            safe_detail = (
                "unexpected narrator error "
                f"({type(error).__name__})"
            )
            return (
                f"AI 분석 중 오류 발생: {safe_detail}",
                _stage("narrative", "failed", safe_detail),
            )

    def _run_telemetry(
        self,
        request: DailyReportRequest,
        trade_artifact: Optional[ReportArtifact],
        minute_artifacts: Sequence[ReportArtifact],
    ) -> ReportStageResult:
        try:
            published = self._publisher.publish_telemetry(
                request=request,
                trade_artifact=trade_artifact,
                minute_artifacts=tuple(minute_artifacts),
            )
            return _stage(
                "telemetry",
                "succeeded" if published else "skipped",
                None if published else "telemetry publisher skipped",
            )
        except Exception as error:
            return _failed_stage("telemetry", error)


def _stage(
    stage: str,
    status: str,
    detail: Optional[str] = None,
) -> ReportStageResult:
    return ReportStageResult(stage=stage, status=status, detail=detail)


def _failed_stage(stage: str, error: Exception) -> ReportStageResult:
    detail = str(error) or type(error).__name__
    return _stage(stage, "failed", detail)


def _result(
    stages: Sequence[ReportStageResult],
    artifacts: Sequence[ReportArtifact],
) -> DailyReportResult:
    first_failure = next(
        (stage.stage for stage in stages if stage.status == "failed"),
        None,
    )
    return DailyReportResult(
        stages=tuple(stages),
        artifacts=tuple(artifacts),
        failed_stage=first_failure,
        requires_attention=first_failure is not None,
    )


def _require_artifact(artifact: ReportArtifact) -> None:
    if not isinstance(artifact, ReportArtifact):
        raise TypeError(
            "report artifact store must return ReportArtifact or None"
        )


def _is_nan(value: float) -> bool:
    try:
        return math.isnan(value)
    except TypeError:
        return False


def _validate_iso_date(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
