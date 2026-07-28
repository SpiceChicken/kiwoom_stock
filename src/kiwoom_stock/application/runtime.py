"""Production runtime composition helpers."""

from dataclasses import dataclass, field
from datetime import date
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Protocol

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.application.credential_preflight import (
    CredentialProviderFactory,
    preflight_environment,
    preflight_settings,
)
from kiwoom_stock.application.credentials import CredentialProvider
from kiwoom_stock.application.ports import PaperTradeLedger, PhysicalStateRepository
from kiwoom_stock.core import config as default_config
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.infrastructure.physical_state_repository import (
    AsyncPhysicalStateRepository,
)
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.settings import Settings


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
