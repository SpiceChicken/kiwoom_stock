"""Stable application contracts for post-market reporting.

This module contains only value objects and validation.  It deliberately does
not import reporting ports, infrastructure adapters, clocks, or SDKs so that
reporting contracts can be shared without creating an import cycle.
"""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional, Tuple


_STAGE_STATUSES = frozenset({"succeeded", "skipped", "failed"})


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
                raise ValueError("successful narration cannot include an error")
            return
        if self.output is not None:
            raise ValueError("non-success narration cannot include output")
        if self.status is NarrationStatus.UNAVAILABLE:
            if self.error_detail is not None:
                raise ValueError("unavailable narration cannot include an error")
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
        return cls(status=NarrationStatus.FAILED, error_detail=error_detail)


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
                raise ValueError(f"report artifact {field_name} must not be empty")


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
        if not all(isinstance(stage, ReportStageResult) for stage in self.stages):
            raise TypeError("report stages must contain ReportStageResult values")
        if not all(isinstance(item, ReportArtifact) for item in self.artifacts):
            raise TypeError("report artifacts must contain ReportArtifact values")
        first_failure = next(
            (stage.stage for stage in self.stages if stage.status == "failed"),
            None,
        )
        if self.failed_stage != first_failure:
            raise ValueError("failed_stage must identify the first failed stage")
        if self.requires_attention is not (first_failure is not None):
            raise ValueError("requires_attention must match failed stage presence")


def _validate_iso_date(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must use YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{field_name} must use YYYY-MM-DD")
