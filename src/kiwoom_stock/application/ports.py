"""Application ports that isolate core logic from infrastructure."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    TYPE_CHECKING,
)

from kiwoom_stock.domain.models import Position

if TYPE_CHECKING:
    from kiwoom_stock.application.reporting import (
        DailyReportRequest,
        DailyReportStats,
        NarrationResult,
        ReportArtifact,
    )


class ArchiveStatus(str, Enum):
    """Outcome of archiving every required post-market output."""

    NOT_CONFIGURED = "not_configured"
    SOURCE_MISSING = "source_missing"
    NO_TARGETS = "no_targets"
    SUCCEEDED = "succeeded"
    PARTIAL_FAILURE = "partial_failure"
    FAILED = "failed"
    ERROR = "error"


class CleanupState(str, Enum):
    """How far receipt-scoped cleanup is known to have progressed."""

    NOT_STARTED = "not_started"
    COMPLETED = "completed"
    PARTIAL = "partial"
    UNKNOWN_AFTER_ATTEMPT = "unknown_after_attempt"


PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production-like"})


def is_production_environment(environment: str) -> bool:
    """Return whether ``environment`` must use fail-closed archive policy."""
    if not isinstance(environment, str):
        raise TypeError("environment must be a string")
    return environment in PRODUCTION_ENVIRONMENTS


@dataclass(frozen=True)
class FilesystemIdentity:
    """Stable POSIX identity and mutation metadata for one opened filesystem node."""

    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int

    def __post_init__(self) -> None:
        for field_name, value in (
            ("device", self.device),
            ("inode", self.inode),
            ("size", self.size),
            ("modified_ns", self.modified_ns),
            ("changed_ns", self.changed_ns),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"filesystem identity {field_name} must be non-negative")


@dataclass(frozen=True)
class ArchiveTargetReceipt:
    """Immutable result for one required archive target."""

    local_path: str
    object_key: str
    succeeded: bool
    source_identity: FilesystemIdentity
    failure: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.local_path, str) or not self.local_path:
            raise ValueError("archive target local_path must not be empty")
        if not isinstance(self.object_key, str) or not self.object_key:
            raise ValueError("archive target object_key must not be empty")
        if not isinstance(self.succeeded, bool):
            raise TypeError("archive target succeeded must be a bool")
        if not isinstance(self.source_identity, FilesystemIdentity):
            raise TypeError("archive target source_identity must be FilesystemIdentity")
        if self.succeeded and self.failure is not None:
            raise ValueError("successful archive target must not include failure detail")
        if not self.succeeded and (
            not isinstance(self.failure, str) or not self.failure
        ):
            raise ValueError("failed archive target must include failure detail")


@dataclass(frozen=True)
class ArchiveReceipt:
    """Immutable aggregate archive result used by lifecycle policy."""

    status: ArchiveStatus
    target_date: str
    source_dir: str
    targets: Tuple[ArchiveTargetReceipt, ...] = ()
    detail: Optional[str] = None
    source_identity: Optional[FilesystemIdentity] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ArchiveStatus):
            raise TypeError("archive status must be ArchiveStatus")
        _validate_iso_date(self.target_date)
        if not isinstance(self.source_dir, str) or not self.source_dir:
            raise ValueError("archive source_dir must not be empty")
        if not isinstance(self.targets, tuple):
            raise TypeError("archive targets must be a tuple")
        if not all(isinstance(target, ArchiveTargetReceipt) for target in self.targets):
            raise TypeError("archive targets must contain ArchiveTargetReceipt values")
        if self.source_identity is not None and not isinstance(
            self.source_identity,
            FilesystemIdentity,
        ):
            raise TypeError("archive source_identity must be FilesystemIdentity")

        local_paths = tuple(target.local_path for target in self.targets)
        object_keys = tuple(target.object_key for target in self.targets)
        if len(local_paths) != len(set(local_paths)):
            raise ValueError("archive targets must have unique local paths")
        if len(object_keys) != len(set(object_keys)):
            raise ValueError("archive targets must have unique object keys")
        expected_order = tuple(
            sorted(self.targets, key=lambda target: Path(target.local_path).name)
        )
        if self.targets != expected_order:
            raise ValueError("archive targets must be sorted by filename")

        successes = sum(target.succeeded for target in self.targets)
        target_count = len(self.targets)
        if self.status is ArchiveStatus.SUCCEEDED:
            valid_status = target_count > 0 and successes == target_count
        elif self.status is ArchiveStatus.PARTIAL_FAILURE:
            valid_status = target_count > 1 and 0 < successes < target_count
        elif self.status is ArchiveStatus.FAILED:
            valid_status = target_count > 0 and successes == 0
        else:
            valid_status = target_count == 0
        if not valid_status:
            raise ValueError("archive status does not match target outcomes")
        if target_count and self.source_identity is None:
            raise ValueError("archive targets require source directory identity")

        detail_required = {
            ArchiveStatus.NOT_CONFIGURED,
            ArchiveStatus.SOURCE_MISSING,
            ArchiveStatus.NO_TARGETS,
            ArchiveStatus.ERROR,
        }
        if self.status in detail_required and not self.detail:
            raise ValueError(f"{self.status.value} archive receipt requires detail")

    @property
    def succeeded_targets(self) -> Tuple[ArchiveTargetReceipt, ...]:
        return tuple(target for target in self.targets if target.succeeded)

    @property
    def failed_targets(self) -> Tuple[ArchiveTargetReceipt, ...]:
        return tuple(target for target in self.targets if not target.succeeded)

    @property
    def cleanup_paths(self) -> Tuple[str, ...]:
        return tuple(target.local_path for target in self.succeeded_targets)

    @property
    def cleanup_allowed(self) -> bool:
        return self.status is ArchiveStatus.SUCCEEDED


@dataclass(frozen=True)
class CleanupReceipt:
    """Immutable result of deleting an already archived exact file set."""

    requested_paths: Tuple[str, ...]
    deleted_paths: Tuple[str, ...]
    failed_paths: Tuple[str, ...]

    def __post_init__(self) -> None:
        for field_name, paths in (
            ("requested_paths", self.requested_paths),
            ("deleted_paths", self.deleted_paths),
            ("failed_paths", self.failed_paths),
        ):
            if not isinstance(paths, tuple):
                raise TypeError(f"cleanup {field_name} must be a tuple")
            if any(not isinstance(path, str) or not path for path in paths):
                raise ValueError(f"cleanup {field_name} must contain non-empty paths")
            if len(paths) != len(set(paths)):
                raise ValueError(f"cleanup {field_name} must contain unique paths")
        if not self.requested_paths:
            raise ValueError("cleanup requested_paths must not be empty")

        requested = set(self.requested_paths)
        deleted = set(self.deleted_paths)
        failed = set(self.failed_paths)
        if deleted & failed:
            raise ValueError("cleanup deleted and failed paths must not overlap")
        if deleted | failed != requested:
            raise ValueError("cleanup outcomes must cover every requested path")
        expected_deleted = tuple(path for path in self.requested_paths if path in deleted)
        expected_failed = tuple(path for path in self.requested_paths if path in failed)
        if self.deleted_paths != expected_deleted or self.failed_paths != expected_failed:
            raise ValueError("cleanup outcomes must preserve requested path order")

    @property
    def state(self) -> CleanupState:
        if self.failed_paths:
            return CleanupState.PARTIAL
        return CleanupState.COMPLETED


class CleanupNotStartedError(ValueError):
    """Scope or identity rejection proven to occur before the first deletion."""


@dataclass(frozen=True)
class PostMarketResult:
    """Observable outcome of the post-market archive and cleanup policy."""

    environment: str
    archive_receipt: Optional[ArchiveReceipt]
    cleanup_receipt: Optional[CleanupReceipt]
    outputs_preserved: bool
    cleanup_state: CleanupState = CleanupState.NOT_STARTED

    def __post_init__(self) -> None:
        if not self.environment or self.environment != self.environment.lower():
            raise ValueError("post-market environment must be normalized")

        if not isinstance(self.cleanup_state, CleanupState):
            raise TypeError("cleanup_state must be CleanupState")

        if not is_production_environment(self.environment):
            if self.archive_receipt is not None or self.cleanup_receipt is not None:
                raise ValueError("non-prod result must not contain archive cleanup receipts")
            if self.outputs_preserved:
                raise ValueError("non-prod result does not use prod preservation state")
            if self.cleanup_state is not CleanupState.NOT_STARTED:
                raise ValueError("non-prod cleanup state must be not_started")
            return

        if self.archive_receipt is None:
            raise ValueError("production result requires an archive receipt")
        if self.cleanup_receipt is not None:
            if not self.archive_receipt.cleanup_allowed:
                raise ValueError("cleanup receipt requires successful archive")
            if self.cleanup_receipt.requested_paths != self.archive_receipt.cleanup_paths:
                raise ValueError("cleanup receipt paths must match archived paths")
            if self.outputs_preserved:
                raise ValueError("started cleanup cannot be marked wholly preserved")
            if self.cleanup_state is not self.cleanup_receipt.state:
                raise ValueError("cleanup state must match cleanup receipt")
        elif self.cleanup_state is CleanupState.NOT_STARTED:
            if not self.outputs_preserved:
                raise ValueError("not-started cleanup must preserve outputs")
        elif self.cleanup_state is CleanupState.UNKNOWN_AFTER_ATTEMPT:
            if self.outputs_preserved:
                raise ValueError("unknown cleanup state cannot claim preserved outputs")
        else:
            raise ValueError("completed or partial cleanup requires a receipt")

    @property
    def requires_attention(self) -> bool:
        if not is_production_environment(self.environment):
            return False
        if self.archive_receipt is None:
            return True
        if self.archive_receipt.status is not ArchiveStatus.SUCCEEDED:
            return True
        if self.cleanup_state is not CleanupState.COMPLETED:
            return True
        return False


class ArchiveStore(Protocol):
    """Archive boundary used by post-market orchestration."""

    def sync_daily_outputs(self, target_date: str, source_dir: str) -> ArchiveReceipt:
        """Archive every direct daily CSV target and return its receipt."""


class ScopedCleanup(Protocol):
    """Filesystem boundary that removes only receipt-confirmed archive targets."""

    def __call__(
        self,
        *,
        target_date: str,
        source_dir: str,
        allowed_root: str,
        source_identity: FilesystemIdentity,
        archived_targets: Sequence[ArchiveTargetReceipt],
    ) -> CleanupReceipt:
        """Delete identity-bound targets after validating the full scope."""


def _validate_iso_date(value: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("target_date must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise ValueError("target_date must use YYYY-MM-DD")


class MarketDataGateway(Protocol):
    """Market-data boundary used by monitoring and analysis orchestration."""

    def get_top_trading_value(self, market_tp: str = "001") -> Sequence[Mapping[str, Any]]:
        """Return ranked stocks by trading value."""

    def get_stock_basic_info(self, stock_code: str) -> Mapping[str, Any]:
        """Return raw basic stock information."""

    def get_minute_chart(self, stock_code: str, tic: str) -> Sequence[Mapping[str, Any]]:
        """Return raw minute-chart rows."""

    def get_tick_strength(self, stock_code: str) -> Sequence[Mapping[str, Any]]:
        """Return raw tick-strength rows."""

    def get_program_trade(self) -> Sequence[Mapping[str, Any]]:
        """Return raw program-trade rows."""

    def get_foreign_window_total(self) -> Sequence[Mapping[str, Any]]:
        """Return raw foreign-window trade rows."""

    def get_order_book(self, stock_code: str) -> Mapping[str, Any]:
        """Return raw order-book information."""

    def get_recent_ticks(self, stock_code: str) -> Sequence[Mapping[str, Any]]:
        """Return raw recent-tick rows."""


class PaperTradeLedger(Protocol):
    """Paper-trade persistence used by the engine and stock manager."""

    def load_open_positions(self) -> Dict[str, Dict[str, Any]]:
        """Return the exact ``OPEN`` rows keyed by stock code."""

    def record_buy(self, data: Dict[str, Any]) -> int:
        """Persist one paper buy and return its ledger identifier."""

    def record_sell(self, position: Position) -> None:
        """Close the paper-trade row represented by ``position``."""

    def get_today_realized_pnl(self) -> float:
        """Return today's realized percentage PnL using the existing formula."""

    def flush(self) -> None:
        """Wait for every accepted physical-state task to finish."""

    def close(self) -> None:
        """Drain accepted work and release owned persistence resources."""


class PhysicalStateRepository(Protocol):
    """Persistence boundary for physics-state recovery and snapshots."""

    def get_last_physical_state(self, stock_code: str) -> Optional[Mapping[str, Any]]:
        """Return the last persisted physics state for ``stock_code`` if available."""

    def submit_physical_state(self, stock_code: str, forces: Mapping[str, Any]) -> None:
        """Submit a physics-state snapshot without blocking tick processing."""

    def close(self) -> None:
        """Reject new snapshots while the owning ledger drains accepted work."""


class ReportDataSource(Protocol):
    """Read copied trade rows for one explicit market date."""

    def load_trades(self, target_date: str) -> Sequence[Mapping[str, Any]]:
        """Return materialized trade rows without exposing a DB connection."""


class MinuteChartSource(Protocol):
    """Read minute-chart rows without exposing a Kiwoom client."""

    def load_minutes(
        self,
        stock_code: str,
        target_date: str,
    ) -> Sequence[Mapping[str, Any]]:
        """Return copied rows for one stock and explicit market date."""


class ReportArtifactStore(Protocol):
    """Persist logical report artifacts behind an opaque string reference."""

    def save_minute_chart(
        self,
        *,
        stock_code: str,
        stock_name: str,
        target_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> Optional["ReportArtifact"]:
        """Persist one non-empty minute chart, or return no artifact."""

    def save_trade_analysis(
        self,
        *,
        target_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> Optional["ReportArtifact"]:
        """Persist analyzed trade rows, or return no artifact."""


class ReportNarrator(Protocol):
    """Generate an optional narrative without leaking an SDK result type."""

    def narrate(
        self,
        *,
        request: "DailyReportRequest",
        stats: "DailyReportStats",
        trade_artifact: Optional["ReportArtifact"],
    ) -> "NarrationResult":
        """Return explicit success, unavailable, or safe failure detail."""


class ReportPublisher(Protocol):
    """Publish summaries and telemetry without exposing Slack SDK types."""

    def summary_enabled(self) -> bool:
        """Return whether summary publication and narration are configured."""

    def publish_summary(
        self,
        *,
        request: "DailyReportRequest",
        stats: "DailyReportStats",
        narrative: str,
        trade_artifact: Optional["ReportArtifact"],
    ) -> bool:
        """Publish the daily summary; false means deliberately skipped."""

    def publish_telemetry(
        self,
        *,
        request: "DailyReportRequest",
        trade_artifact: Optional["ReportArtifact"],
        minute_artifacts: Sequence["ReportArtifact"],
    ) -> bool:
        """Publish report artifacts; false means deliberately skipped."""
