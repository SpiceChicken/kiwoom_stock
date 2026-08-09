"""Production runtime composition helpers."""

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import os
from pathlib import Path
import threading
import time
from types import TracebackType
from typing import Any, Callable, Dict, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.application.credential_preflight import (
    CredentialProviderFactory,
    preflight_environment,
    preflight_settings,
)
from kiwoom_stock.application.credentials import CredentialProvider
from kiwoom_stock.application.ports import (
    MarketDataGateway,
    PaperTradeLedger,
    PhysicalStateRepository,
)
from kiwoom_stock.application.execution import ExecutionMode, ExecutionPolicy
from kiwoom_stock.application.shadow_worker import (
    CalendarDecision,
    ShadowAdmission,
    ShadowCycleTerminated,
    ShadowExecutionReceipt,
    ShadowTerminalReason,
)
from kiwoom_stock.application.shadow_lifecycle import (
    ShadowRunDeadlineExceeded,
    ShadowShutdownDeadlineExceeded,
    ShadowStopRequested,
)
from kiwoom_stock.core import config as default_config
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.domain.models import PhysicalContinuityEvidence
from kiwoom_stock.domain.strategy import TargetStopPolicy
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    AllowlistedReadOnlySession,
    CachedMarketGateway,
    KiwoomMarketDataGatewayAdapter,
    MarketOnlyClient,
    fetch_market_snapshot,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.local_shadow_notifier import LocalShadowNotifier
from kiwoom_stock.settings import KiwoomApiMode, Settings
from kiwoom_stock.utils.market_cal import seoul_now


logger = logging.getLogger(__name__)

NORMAL_SHUTDOWN_TIMEOUT_SECONDS = 30.0


def _start_normal_shutdown_deadline(
    timeout_seconds: Optional[float] = None,
) -> tuple[float, Callable[[], float]]:
    """Create the shared normal-shutdown deadline and clamped budget reader."""

    budget = (
        NORMAL_SHUTDOWN_TIMEOUT_SECONDS
        if timeout_seconds is None
        else timeout_seconds
    )
    started_at = time.monotonic()
    deadline = started_at + budget

    def remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    return started_at, remaining


class RuntimeDisabledError(RuntimeError):
    """The config-check-only mode cannot construct a trading runtime."""


class ConfigModule(Protocol):
    CONFIG: Mapping[str, Any]
    STRATEGY_CONFIG: Mapping[str, Any]
    OUTPUT_DIR_STR: str

    def configure_from_environment(self, today: date) -> Settings:
        """Validate environment and publish legacy config mappings."""

    def validate_environment_settings(self) -> Settings:
        """Validate settings without publishing or creating directories."""

    def activate_runtime_settings(self, settings: Settings, today: date) -> Settings:
        """Create runtime output and publish an already validated settings object."""


ClientFactory = Callable[..., Any]


def _default_credential_provider_factory(path: Path) -> CredentialProvider:
    return StrictFileCredentialProvider(
        path,
        repository_root=credential_repository_boundary(),
    )


class LedgerFactory(Protocol):
    def __call__(
        self,
        db_path: Path,
        clock: Callable[[], datetime],
    ) -> TradeLogger:
        """Construct the configured paper ledger and physical queue owner."""


class PhysicalStateRepositoryFactory(Protocol):
    def __call__(self, ledger: TradeLogger) -> PhysicalStateRepository:
        """Wrap the ledger-owned physical-state queue."""


class EngineFactory(Protocol):
    def __call__(
        self,
        client: Any,
        app_config: Dict[str, Any],
        *,
        ledger: PaperTradeLedger,
        physical_state_repository: PhysicalStateRepository,
        market_gateway: MarketDataGateway,
        target_stop_policy: TargetStopPolicy,
        wall_clock: Callable[[], datetime],
    ) -> Any:
        """Build an engine from already constructed persistence dependencies."""


def _default_ledger_factory(
    db_path: Path,
    clock: Callable[[], datetime],
) -> TradeLogger:
    return TradeLogger(db_path, clock=clock)


def _default_shadow_ledger_factory(
    db_path: Path,
    clock: Callable[[], datetime],
) -> TradeLogger:
    return TradeLogger(db_path, clock=clock)


def _default_physical_state_repository_factory(
    ledger: TradeLogger,
) -> PhysicalStateRepository:
    return AsyncPhysicalStateRepository(ledger)


def _default_engine_factory(
    client: Any,
    app_config: Dict[str, Any],
    *,
    ledger: PaperTradeLedger,
    physical_state_repository: PhysicalStateRepository,
    market_gateway: MarketDataGateway,
    target_stop_policy: Optional[TargetStopPolicy] = None,
    wall_clock: Callable[[], datetime] = seoul_now,
) -> Any:
    return TradingEngine(
        client,
        app_config,
        ledger=ledger,
        physical_state_repository=physical_state_repository,
        market_gateway=market_gateway,
        target_stop_policy=target_stop_policy,
        wall_clock=wall_clock,
    )


@dataclass
class TradingRuntime:
    """Fully wired runtime objects needed by the process entrypoint."""

    settings: Settings
    app_config: Dict[str, Any] = field(repr=False)
    output_dir_str: str
    monitor: Any
    _market_owner: Any = field(repr=False)
    _ledger: PaperTradeLedger = field(repr=False)
    _shutdown_budget_seconds: float = field(
        default_factory=lambda: NORMAL_SHUTDOWN_TIMEOUT_SECONDS,
        repr=False,
    )
    _shutdown_started_at: Optional[float] = field(default=None, init=False, repr=False)
    _shutdown_remaining: Optional[Callable[[], float]] = field(
        default=None, init=False, repr=False
    )
    _shutdown_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )
    _shutdown_complete: threading.Event = field(
        default_factory=threading.Event, init=False, repr=False
    )
    _shutdown_error: Optional[BaseException] = field(default=None, init=False, repr=False)
    _shutdown_traceback: Optional[TracebackType] = field(
        default=None, init=False, repr=False
    )

    def shutdown_engine(self) -> None:
        """Let one caller own close while peers share its bounded result."""

        selected_remaining: Optional[Callable[[], float]]
        with self._shutdown_lock:
            owner = self._shutdown_remaining is None
            if owner:
                started_at, selected_remaining = _start_normal_shutdown_deadline(
                    self._shutdown_budget_seconds
                )
                self._shutdown_started_at = started_at
                self._shutdown_remaining = selected_remaining
            else:
                selected_remaining = self._shutdown_remaining
        assert selected_remaining is not None

        if owner:
            try:
                set_deadline = getattr(self._ledger, "set_shutdown_deadline", None)
                if callable(set_deadline):
                    set_deadline(selected_remaining)
                self.monitor._deadline_remaining = selected_remaining
                stop_event = getattr(self.monitor, "_stop_event", None)
                if stop_event is None:
                    stop_event = threading.Event()
                    self.monitor._stop_event = stop_event
                stop_event.set()
                self.monitor.close()
            except BaseException as error:
                with self._shutdown_lock:
                    self._shutdown_error = error
                    self._shutdown_traceback = error.__traceback__
                raise
            finally:
                self._shutdown_complete.set()
            return

        if not self._shutdown_complete.wait(timeout=selected_remaining()):
            raise RuntimeError("normal runtime shutdown deadline exceeded")
        with self._shutdown_lock:
            shutdown_error = self._shutdown_error
            error_traceback = self._shutdown_traceback
        if shutdown_error is not None:
            raise shutdown_error.with_traceback(error_traceback)

    def close(self) -> None:
        """Close the private market/auth lifecycle owner without network revoke."""

        self._market_owner.close()


class ShadowExecutionFailure(ShadowCycleTerminated):
    """Redacted execution/cleanup failure preserving only safe type names."""

    def __init__(
        self,
        reason: ShadowTerminalReason,
        primary_type: str | None,
        cleanup_types: tuple[str, ...],
    ):
        self.primary_type = primary_type
        self.cleanup_types = cleanup_types
        super().__init__(
            reason,
            resources_closed=not cleanup_types,
            error_type=primary_type or (cleanup_types[0] if cleanup_types else None),
        )

    @property
    def cleanup_type(self) -> str | None:
        return self.cleanup_types[0] if self.cleanup_types else None


class ShadowRuntime:
    """Atomic owner of the complete one-shot capability graph."""

    def __init__(
        self,
        *,
        policy: ExecutionPolicy,
        client: MarketOnlyClient,
        monitor: TradingEngine,
        notifier: LocalShadowNotifier,
        db_path: Path,
        session: AllowlistedReadOnlySession,
        stop_event: threading.Event | None = None,
        deadline_remaining: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy
        self._client = client
        self._monitor = monitor
        self._notifier = notifier
        self._db_path = db_path
        self._session = session
        self._stop_event = stop_event
        self._deadline_remaining = deadline_remaining
        self._state = "not-started"
        self._state_lock = threading.Lock()

    def execute_once(self) -> ShadowExecutionReceipt:
        with self._state_lock:
            if self._state != "not-started":
                raise RuntimeError("shadow runtime capability is already consumed")
            self._state = "running"

        primary: BaseException | None = None
        cycle: Mapping[str, Any] = {}
        attempts = 0
        api_counts: Mapping[str, int] = {}
        local_counts: Mapping[str, int] = {}
        try:
            self._checkpoint_lifecycle()
            cycle = self._monitor.run_shadow_cycle(self._policy.stock_code)
            self._checkpoint_lifecycle()
            if cycle.get("cycles") != self._policy.max_cycles:
                raise RuntimeError("shadow monitor violated the cycle budget")
            attempts = self._client.attempt_count
            api_counts = self._client.safe_counts()
            local_counts = self._notifier.safe_counts()
        except BaseException as error:
            primary = error
        cleanup_types = _close_with_safe_types(
            self._monitor,
            self._client,
            self._session,
        )
        with self._state_lock:
            self._state = "terminal"
        if primary is not None or cleanup_types:
            reason = _terminal_reason_after_cleanup(
                primary,
                self._deadline_remaining,
            )
            raise ShadowExecutionFailure(
                reason,
                type(primary).__name__ if primary is not None else None,
                cleanup_types,
            ) from None
        if self._db_path != self._policy.shadow_database_path:
            raise RuntimeError("shadow runtime database identity drifted")
        if type(attempts) is not int or attempts < 0:
            raise RuntimeError("shadow runtime reported invalid HTTP evidence")
        if not isinstance(api_counts, Mapping) or any(
            type(value) is not int or value < 0
            for value in api_counts.values()
        ):
            raise RuntimeError("shadow runtime reported invalid API evidence")
        expected_local_keys = {
            "status",
            "paper_buy",
            "paper_sell",
            "error",
            "critical",
        }
        if (
            not isinstance(local_counts, Mapping)
            or set(local_counts) != expected_local_keys
            or any(type(value) is not int for value in local_counts.values())
            or local_counts["status"] != 1
            or local_counts["error"] != 0
            or local_counts["critical"] != 0
            or local_counts["paper_buy"] not in (0, 1)
            or local_counts["paper_sell"] not in (0, 1)
            or local_counts["paper_buy"] + local_counts["paper_sell"] > 1
        ):
            raise RuntimeError("shadow runtime reported invalid local evidence")
        continuity = cycle.get("continuity")
        if not isinstance(continuity, PhysicalContinuityEvidence):
            raise RuntimeError("shadow runtime reported invalid continuity evidence")
        return ShadowExecutionReceipt(
            cycles=1,
            http_attempts=attempts,
            api_counts=api_counts,
            db_identity=str(self._db_path),
            resources_closed=True,
            local_counts=local_counts,
            continuity=continuity,
        )

    def _checkpoint_lifecycle(self) -> None:
        if self._deadline_remaining is not None:
            self._deadline_remaining()
        if self._stop_event is not None and self._stop_event.is_set():
            raise ShadowStopRequested("shadow stop requested")


def create_trading_runtime(
    *,
    today: date,
    config_module: ConfigModule = default_config,
    client_factory: ClientFactory = KiwoomClient,
    credential_provider_factory: CredentialProviderFactory = (
        _default_credential_provider_factory
    ),
    engine_factory: EngineFactory = _default_engine_factory,
    ledger_factory: LedgerFactory = _default_ledger_factory,
    physical_state_repository_factory: PhysicalStateRepositoryFactory = (
        _default_physical_state_repository_factory
    ),
    prevalidated_settings: Optional[Settings] = None,
    clock: Callable[[], datetime] = seoul_now,
) -> TradingRuntime:
    """Validate settings and build the production monitor graph."""
    preflight = (
        preflight_environment(config_module, credential_provider_factory)
        if prevalidated_settings is None
        else preflight_settings(
            prevalidated_settings,
            credential_provider_factory,
        )
    )
    settings = preflight.settings
    if settings.kiwoom.api_mode == "disabled":
        raise RuntimeDisabledError(
            "KIWOOM_API_MODE=disabled permits configuration checks only"
        )
    settings = config_module.activate_runtime_settings(settings, today=today)
    app_config = {**config_module.CONFIG, **config_module.STRATEGY_CONFIG}

    ledger: Optional[TradeLogger] = None
    physical_state_repository: Optional[PhysicalStateRepository] = None
    client: Any = None
    try:
        ledger = ledger_factory(settings.database.path, clock)
        physical_state_repository = physical_state_repository_factory(ledger)
        endpoint = settings.kiwoom.endpoint
        credentials = preflight.credentials
        if endpoint is None or credentials is None:
            raise ValueError("enabled credential preflight is incomplete")
        client = client_factory(credentials=credentials, endpoint=endpoint)
        market_gateway = KiwoomMarketDataGatewayAdapter.from_client(client)
        market_gateway.preflight()
        monitor = engine_factory(
            client,
            app_config,
            ledger=ledger,
            physical_state_repository=physical_state_repository,
            market_gateway=market_gateway,
            target_stop_policy=settings.strategy.target_stop_policy,
            wall_clock=clock,
        )
    except BaseException:
        try:
            _, deadline_remaining = _start_normal_shutdown_deadline()
            _close_failed_runtime_resources(
                client,
                physical_state_repository,
                ledger,
                deadline_remaining,
            )
        except BaseException as error:
            logger.error(
                "Runtime construction rollback setup failed (type=%s).",
                type(error).__name__,
            )
        raise

    return TradingRuntime(
        settings=settings,
        app_config=app_config,
        output_dir_str=config_module.OUTPUT_DIR_STR,
        monitor=monitor,
        _market_owner=client,
        _ledger=ledger,
    )


def create_shadow_runtime(
    *,
    policy: ExecutionPolicy,
    settings: Settings,
    admission: ShadowAdmission,
    credential_provider_factory: CredentialProviderFactory = (
        _default_credential_provider_factory
    ),
    ledger_factory: Callable[[Path, Callable[[], datetime]], TradeLogger] = (
        _default_shadow_ledger_factory
    ),
    physical_state_repository_factory: PhysicalStateRepositoryFactory = (
        _default_physical_state_repository_factory
    ),
    market_client_factory: Callable[..., MarketOnlyClient] = MarketOnlyClient,
    session_factory: Callable[..., AllowlistedReadOnlySession] = AllowlistedReadOnlySession,
    local_notifier_factory: Callable[[], LocalShadowNotifier] = LocalShadowNotifier,
    engine_factory: Callable[..., TradingEngine] = TradingEngine,
) -> ShadowRuntime:
    """Build the bounded shadow graph after calendar admission has succeeded."""

    if settings.execution.mode is not policy.mode:
        raise RuntimeDisabledError("CLI and KIWOOM_EXECUTION_MODE must select the same shadow mode")
    process_names = {
        ExecutionMode.SHADOW_ONCE: "kiwoom-shadow-once",
        ExecutionMode.SHADOW_CONTINUOUS: "kiwoom-shadow-worker",
    }
    expected_process_name = process_names.get(policy.mode)
    if expected_process_name is None:
        raise RuntimeDisabledError("runtime construction requires an admitted shadow mode")
    if settings.kiwoom.api_mode is not KiwoomApiMode.PROD:
        raise RuntimeDisabledError("shadow execution requires KIWOOM_API_MODE=prod")
    if settings.runtime.process_name != expected_process_name:
        raise RuntimeError(
            f"{policy.mode.value} requires KIWOOM_PROCESS_NAME={expected_process_name}"
        )
    db_path = policy.assert_shadow_database_identity(settings.database.path)
    _assert_shadow_volume_attestation(db_path)
    if (
        admission.decision is not CalendarDecision.OPEN
        or admission.now.tzinfo is None
        or admission.now.utcoffset() is None
        or getattr(admission.now.tzinfo, "key", None) != ZoneInfo("Asia/Seoul").key
        or admission.now.date() != admission.kst_date
    ):
        raise RuntimeError("shadow admission must contain one aware KST OPEN instant")
    policy.assert_broker_orders_disabled()
    if any(
        (
            settings.notification.slack_webhook_url,
            settings.notification.slack_bot_token,
            settings.notification.slack_channel_id,
            settings.notification.gemini_api_key,
            settings.storage.s3_bucket_name,
        )
    ):
        raise RuntimeError("shadow execution forbids notification, AI, and archive configuration")

    admission.checkpoint()
    preflight = preflight_settings(settings, credential_provider_factory)
    credentials = preflight.credentials
    if credentials is None:
        raise RuntimeError("shadow credential preflight is incomplete")

    session: AllowlistedReadOnlySession | None = None
    client: MarketOnlyClient | None = None
    ledger: TradeLogger | None = None
    repository: PhysicalStateRepository | None = None
    try:
        admission.checkpoint()
        session = session_factory(
            stock_code=policy.stock_code,
            proxy_code=policy.proxy_code,
            max_attempts=policy.max_http_attempts,
            terminate_on_rate_limit=True,
            stop_event=admission.stop_event,
            deadline_remaining=admission.deadline_remaining,
        )
        admission.checkpoint()
        client = market_client_factory(credentials, session=session)
        admission.checkpoint()
        live_gateway = KiwoomMarketDataGatewayAdapter.from_client(client)
        snapshot = fetch_market_snapshot(
            client,
            stock_code=policy.stock_code,
            proxy_code=policy.proxy_code,
            market_gateway=live_gateway,
        )
        admission.checkpoint()
        gateway = CachedMarketGateway(policy.stock_code, policy.proxy_code, snapshot)
        ledger = ledger_factory(db_path, admission.clock)
        if admission.deadline_remaining is not None:
            set_deadline = getattr(ledger, "set_shutdown_deadline", None)
            if callable(set_deadline):
                set_deadline(admission.deadline_remaining)
        repository = physical_state_repository_factory(ledger)
        system_config, strategy_config = settings.to_legacy_mappings()
        app_config = {**dict(system_config), **dict(strategy_config)}
        notifier = local_notifier_factory()
        shadow_client = type("ShadowMarketGraph", (), {"market": gateway})()
        monitor = engine_factory(
            shadow_client,
            app_config,
            ledger=ledger,
            physical_state_repository=repository,
            market_gateway=gateway,
            target_stop_policy=settings.strategy.target_stop_policy,
            notifier=notifier,
            paper_transition_guard=policy.assert_paper_transition,
            wall_clock=admission.clock,
            stop_event=admission.stop_event,
            deadline_remaining=admission.deadline_remaining,
        )
    except BaseException as error:
        cleanup_types = _close_failed_shadow_resources(
            repository, ledger, client, session
        )
        raise ShadowExecutionFailure(
            _terminal_reason_after_cleanup(error, admission.deadline_remaining),
            type(error).__name__,
            cleanup_types,
        ) from None
    return ShadowRuntime(
        policy=policy,
        client=client,
        monitor=monitor,
        notifier=notifier,
        db_path=db_path,
        session=session,
        stop_event=admission.stop_event,
        deadline_remaining=admission.deadline_remaining,
    )


def _close_with_safe_types(*resources: Any) -> tuple[str, ...]:
    failures = []
    for resource in resources:
        if resource is None or not hasattr(resource, "close"):
            continue
        try:
            resource.close()
        except BaseException as error:
            failures.append(type(error).__name__)
    return tuple(failures)


def _assert_shadow_volume_attestation(db_path: Path) -> None:
    """Optionally require the Compose named-volume boundary at activation."""

    if os.environ.get("KIWOOM_REQUIRE_SHADOW_VOLUME") != "1":
        return
    volume_root = Path("/var/lib/kiwoom")
    if db_path.parent != volume_root or not os.path.ismount(volume_root):
        raise RuntimeError("shadow database is not mounted on the admitted data volume")
    if not os.access(volume_root, os.W_OK):
        raise RuntimeError("shadow data volume is not writable by the runtime user")


def _close_failed_shadow_resources(
    repository: PhysicalStateRepository | None,
    ledger: PaperTradeLedger | None,
    client: Any,
    session: AllowlistedReadOnlySession | None,
) -> tuple[str, ...]:
    """Retire local work before clearing the market-only token owner."""

    failures = []
    for label, resource in (
        ("physical-state repository", repository),
        ("paper ledger", ledger),
        ("market-only client", client),
        ("market session finalizer", session),
    ):
        if resource is None or not hasattr(resource, "close"):
            continue
        try:
            resource.close()
        except BaseException as error:
            failures.append(type(error).__name__)
            logger.error("Failed to close shadow %s during construction rollback.", label)
    return tuple(failures)


def _terminal_reason_for_error(error: BaseException | None) -> ShadowTerminalReason:
    current = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ShadowStopRequested):
            return ShadowTerminalReason.STOP_REQUESTED
        if isinstance(current, ShadowRunDeadlineExceeded):
            return ShadowTerminalReason.RUN_DEADLINE
        if isinstance(current, ShadowShutdownDeadlineExceeded):
            return ShadowTerminalReason.SHUTDOWN_DEADLINE
        current = current.__cause__
    return ShadowTerminalReason.FAILURE


def _terminal_reason_after_cleanup(
    error: BaseException | None,
    deadline_remaining: Callable[[], float] | None,
) -> ShadowTerminalReason:
    """Resolve a genuine lifecycle primary after all owned cleanup completes."""

    reason = _terminal_reason_for_error(error)
    if reason is not ShadowTerminalReason.STOP_REQUESTED or deadline_remaining is None:
        return reason
    try:
        deadline_remaining()
    except ShadowShutdownDeadlineExceeded:
        return ShadowTerminalReason.SHUTDOWN_DEADLINE
    except ShadowRunDeadlineExceeded:
        return ShadowTerminalReason.RUN_DEADLINE
    except Exception:
        pass
    return reason


def _close_failed_runtime_resources(
    client: Any,
    physical_state_repository: Optional[PhysicalStateRepository],
    ledger: Optional[PaperTradeLedger],
    deadline_remaining: Callable[[], float],
) -> None:
    """Bound rollback liveness without claiming timed-out resources were closed."""

    completed = threading.Event()
    phase = ["coordinator start"]
    phase_lock = threading.Lock()

    def close_resources() -> None:
        try:
            with phase_lock:
                phase[0] = "paper ledger deadline installation"
            try:
                set_deadline = getattr(ledger, "set_shutdown_deadline", None)
                if callable(set_deadline):
                    set_deadline(deadline_remaining)
            except BaseException as error:
                logger.error(
                    "Runtime construction rollback deadline installation failed "
                    "for paper ledger (type=%s).",
                    type(error).__name__,
                )
            for label, resource in (
                ("Kiwoom client", client),
                ("physical-state repository", physical_state_repository),
                ("paper ledger", ledger),
            ):
                with phase_lock:
                    phase[0] = label
                try:
                    close = getattr(resource, "close", None)
                    if callable(close):
                        close()
                except BaseException as error:
                    logger.error(
                        "Failed to close %s during runtime construction rollback "
                        "(type=%s).",
                        label,
                        type(error).__name__,
                    )
        finally:
            with phase_lock:
                phase[0] = "complete"
                completed.set()

    coordinator = threading.Thread(
        target=close_resources,
        name="RuntimeConstructionRollback",
        daemon=True,
    )
    try:
        coordinator.start()
        finished = completed.wait(timeout=deadline_remaining())
    except BaseException as error:
        with phase_lock:
            phase_snapshot = phase[0]
        logger.error(
            "Runtime construction rollback coordination failed "
            "(phase=%s, type=%s).",
            phase_snapshot,
            type(error).__name__,
        )
        return
    if not finished:
        with phase_lock:
            if completed.is_set():
                return
            phase_snapshot = phase[0]
        logger.warning(
            "Runtime construction rollback deadline exceeded (phase=%s).",
            phase_snapshot,
        )
