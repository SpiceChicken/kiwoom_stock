"""Production runtime composition helpers."""

from dataclasses import dataclass, field
from datetime import date, datetime
import logging
import os
from pathlib import Path
import threading
from typing import Any, Callable, Dict, Mapping, Optional, Protocol
from zoneinfo import ZoneInfo

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.application.credential_preflight import (
    CredentialProviderFactory,
    preflight_environment,
    preflight_settings,
)
from kiwoom_stock.application.credentials import CredentialProvider
from kiwoom_stock.application.ports import PaperTradeLedger, PhysicalStateRepository
from kiwoom_stock.application.execution import ExecutionPolicy
from kiwoom_stock.application.shadow_worker import (
    CalendarDecision,
    ShadowAdmission,
    ShadowExecutionReceipt,
)
from kiwoom_stock.core import config as default_config
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    AllowlistedReadOnlySession,
    CachedMarketGateway,
    MarketOnlyClient,
    fetch_market_snapshot,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.local_shadow_notifier import LocalShadowNotifier
from kiwoom_stock.settings import KiwoomApiMode, Settings


logger = logging.getLogger(__name__)


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
    def __call__(self, db_path: Path) -> TradeLogger:
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
    ) -> Any:
        """Build an engine from already constructed persistence dependencies."""


def _default_ledger_factory(db_path: Path) -> TradeLogger:
    return TradeLogger(db_path)


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
) -> Any:
    return TradingEngine(
        client,
        app_config,
        ledger=ledger,
        physical_state_repository=physical_state_repository,
    )


@dataclass(frozen=True)
class TradingRuntime:
    """Fully wired runtime objects needed by the process entrypoint."""

    settings: Settings
    app_config: Dict[str, Any] = field(repr=False)
    output_dir_str: str
    client: Any = field(repr=False)
    monitor: Any


class ShadowExecutionFailure(RuntimeError):
    """Redacted execution/cleanup failure preserving only safe type names."""

    def __init__(self, primary_type: str | None, cleanup_types: tuple[str, ...]):
        self.primary_type = primary_type
        self.cleanup_types = cleanup_types
        super().__init__("shadow execution failed")

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
            raise ShadowExecutionFailure(
                type(primary).__name__ if primary is not None else None,
                cleanup_types,
            ) from None
        if self._db_path != self._policy.shadow_database_path:
            raise RuntimeError("shadow runtime database identity drifted")
        if not isinstance(attempts, int) or attempts < 0:
            raise RuntimeError("shadow runtime reported invalid HTTP evidence")
        if not isinstance(api_counts, Mapping) or any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in api_counts.values()
        ):
            raise RuntimeError("shadow runtime reported invalid API evidence")
        return ShadowExecutionReceipt(
            cycles=1,
            http_attempts=attempts,
            api_counts=api_counts,
            db_identity=str(self._db_path),
            resources_closed=True,
            local_counts=local_counts,
        )

    def _checkpoint_lifecycle(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise RuntimeError("shadow stop requested")
        if self._deadline_remaining is not None:
            try:
                self._deadline_remaining()
            except Exception as error:
                raise RuntimeError("shadow shutdown deadline exceeded") from error


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
        ledger = ledger_factory(settings.database.path)
        physical_state_repository = physical_state_repository_factory(ledger)
        endpoint = settings.kiwoom.endpoint
        credentials = preflight.credentials
        if endpoint is None or credentials is None:
            raise ValueError("enabled credential preflight is incomplete")
        client = client_factory(credentials=credentials, endpoint=endpoint)
        client.ensure_auth_ready()
        monitor = engine_factory(
            client,
            app_config,
            ledger=ledger,
            physical_state_repository=physical_state_repository,
        )
    except BaseException:
        _close_failed_runtime_resources(client, physical_state_repository, ledger)
        raise

    return TradingRuntime(
        settings=settings,
        app_config=app_config,
        output_dir_str=config_module.OUTPUT_DIR_STR,
        client=client,
        monitor=monitor,
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
        raise RuntimeDisabledError("CLI and KIWOOM_EXECUTION_MODE must both be shadow-once")
    if settings.kiwoom.api_mode is not KiwoomApiMode.PROD:
        raise RuntimeDisabledError("shadow-once requires KIWOOM_API_MODE=prod")
    if settings.runtime.process_name != "kiwoom-shadow-once":
        raise RuntimeError("shadow-once requires KIWOOM_PROCESS_NAME=kiwoom-shadow-once")
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
        raise RuntimeError("shadow-once forbids notification, AI, and archive configuration")

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
        snapshot = fetch_market_snapshot(
            client,
            stock_code=policy.stock_code,
            proxy_code=policy.proxy_code,
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
            notifier=notifier,
            paper_transition_guard=policy.assert_paper_transition,
            wall_clock=admission.clock,
            stop_event=admission.stop_event,
            deadline_remaining=admission.deadline_remaining,
        )
    except BaseException:
        _close_failed_shadow_resources(repository, ledger, client, session)
        raise
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
) -> None:
    """Retire local work before clearing the market-only token owner."""

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
        except BaseException:
            logger.error("Failed to close shadow %s during construction rollback.", label)


def _close_failed_runtime_resources(
    client: Any,
    physical_state_repository: Optional[PhysicalStateRepository],
    ledger: Optional[PaperTradeLedger],
) -> None:
    """Release constructed local resources without replacing the primary error."""
    for label, resource in (
        ("Kiwoom client", client),
        ("physical-state repository", physical_state_repository),
        ("paper ledger", ledger),
    ):
        if resource is None or not hasattr(resource, "close"):
            continue
        try:
            resource.close()
        except BaseException:
            logger.exception("Failed to close %s during runtime construction rollback.", label)
