"""Typed application settings and explicit legacy configuration loading.

Only :func:`load_settings_from_environment` reads the process environment.
Pydantic Settings is confined to that source boundary; the application receives
frozen standard-library dataclasses.
"""

import math
import os
import re
from numbers import Real
from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urlsplit

from pydantic import ValidationError, create_model
from pydantic_settings import BaseSettings, SettingsConfigDict

from kiwoom_stock.application.execution import ExecutionMode
from kiwoom_stock.domain.strategy import (
    TARGET_STOP_UNIT_VERSION,
    StrategySemanticsValidationError,
    TargetStopPolicy,
)


from kiwoom_stock.settings_contracts import (
    ALL_ENVIRONMENTS,
    CONFIGURATION_HELP,
    MISSING,
    SETTING_SPEC_BY_NAME,
    SETTING_SPECS,
    SettingSpec,
    SettingsIssue,
    SettingsValidationError,
)
from kiwoom_stock.settings_sources import (
    EnvironmentSnapshot,
    load_legacy_documents,
)
from kiwoom_stock.settings_resolver import (
    LEGACY_CANDIDATES as _LEGACY_CANDIDATES,
    SettingsResolver as _Resolver,
    all_equal as _all_equal,
    lookup as _lookup,
)

class KiwoomApiMode(str, Enum):
    DISABLED = "disabled"
    MOCK = "mock"
    PROD = "prod"


class KiwoomEndpoint(str, Enum):
    MOCK = "https://mockapi.kiwoom.com"
    PROD = "https://api.kiwoom.com"


KIWOOM_API_MODES = tuple(mode.value for mode in KiwoomApiMode)
FORBIDDEN_CREDENTIAL_ENV = frozenset(
    {"KIWOOM_APP_KEY", "KIWOOM_SECRET_KEY", "KIWOOM_BASE_URL"}
)
_FORBIDDEN_NORMALIZED_KEYS = frozenset(
    {
        "appkey",
        "secretkey",
        "baseurl",
        "kiwoomappkey",
        "kiwoomsecretkey",
        "kiwoombaseurl",
    }
)
_MISSING = MISSING
_TARGET_STOP_CANONICAL_NAMES = (
    "KIWOOM_TARGET_STOP_UNIT_VERSION",
    "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS",
    "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS",
)
_TARGET_STOP_CANONICAL_MAPPING_KEYS = (
    "target_stop_unit_version",
    "target_profit_percentage_points",
    "stop_loss_percentage_points",
)
_AMBIGUOUS_TARGET_STOP_KEYS = ("target_profit_rate", "stop_loss_rate")
_TARGET_STOP_COMPATIBILITY_KEYS = (
    *_AMBIGUOUS_TARGET_STOP_KEYS,
    *_TARGET_STOP_CANONICAL_MAPPING_KEYS,
)
_CUMULATIVE_SCORE_CANONICAL_NAME = "KIWOOM_CUMULATIVE_TRADE_RETURN_SCORE_FLOOR"
_CUMULATIVE_SCORE_DEPRECATED_ENV_NAME = "KIWOOM_TOTAL_LOSS_LIMIT"
_CUMULATIVE_SCORE_MAPPING_KEYS = (
    "cumulative_trade_return_score_floor",
    "total_loss_limit",
)

_CANONICAL_SYSTEM_COMPATIBILITY_KEYS: Tuple[str, ...] = (
    "process_name",
    "app_env",
    "aws_s3_bucket_name",
    "aws_region",
    "webhook_url",
    "gemini_api_key",
    "slack_token",
    "slack_channel",
    "fast_interval",
    "slow_interval",
    "max_workers",
    "market",
    "filters",
)
_CANONICAL_STRATEGY_KEYS: Tuple[str, ...] = (
    "debug_mode",
    "day_trade_exit_time",
    "entry_deadline",
    "cumulative_trade_return_score_floor",
    "target_stop_unit_version",
    "target_profit_percentage_points",
    "stop_loss_percentage_points",
    "regimes",
)

@dataclass(frozen=True)
class RuntimeSettings:
    app_env: str
    process_name: str

    def __post_init__(self) -> None:
        if self.app_env not in ALL_ENVIRONMENTS:
            raise ValueError("runtime app_env must be a supported environment")
        if not isinstance(self.process_name, str) or not self.process_name.strip():
            raise ValueError("runtime process_name must be a non-empty string")


@dataclass(frozen=True)
class ExecutionSettings:
    mode: ExecutionMode


@dataclass(frozen=True)
class KiwoomSettings:
    api_mode: KiwoomApiMode
    credentials_dir: Optional[Path]

    def __post_init__(self) -> None:
        if not isinstance(self.api_mode, KiwoomApiMode):
            raise TypeError("Kiwoom api_mode must be a KiwoomApiMode")
        if self.api_mode is KiwoomApiMode.DISABLED:
            if self.credentials_dir is not None:
                raise ValueError(
                    "disabled Kiwoom mode must not have a credential directory"
                )
        elif (
            not isinstance(self.credentials_dir, Path)
            or not self.credentials_dir.is_absolute()
        ):
            raise ValueError(
                "enabled Kiwoom mode requires an absolute credential directory"
            )

    @property
    def endpoint(self) -> Optional[KiwoomEndpoint]:
        """Derive the only allowed endpoint from the typed mode."""

        return _endpoint_for_mode(self.api_mode)


@dataclass(frozen=True)
class MonitoringSettings:
    fast_interval_seconds: float
    slow_interval_seconds: float
    max_workers: int
    market_proxy_code: str
    max_stocks: int
    etf_keywords: Tuple[str, ...]


@dataclass(frozen=True)
class StrategySettings:
    debug_mode: bool
    day_trade_exit_time: str
    entry_deadline: str
    cumulative_trade_return_score_floor: float
    target_stop_policy: TargetStopPolicy
    regimes: Mapping[str, Any] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.cumulative_trade_return_score_floor, bool)
            or not isinstance(self.cumulative_trade_return_score_floor, (int, float))
            or not math.isfinite(float(self.cumulative_trade_return_score_floor))
            or self.cumulative_trade_return_score_floor > 0
        ):
            raise ValueError(
                "cumulative_trade_return_score_floor must be a finite "
                "non-boolean number at or below zero"
            )
        if not isinstance(self.target_stop_policy, TargetStopPolicy):
            raise TypeError("target_stop_policy must be a TargetStopPolicy")

    @property
    def target_stop_unit_version(self) -> str:
        return self.target_stop_policy.unit_version

    @property
    def target_profit_percentage_points(self) -> float:
        return self.target_stop_policy.target_profit_percentage_points

    @property
    def stop_loss_percentage_points(self) -> float:
        return self.target_stop_policy.stop_loss_percentage_points


@dataclass(frozen=True)
class NotificationSettings:
    slack_webhook_url: Optional[str] = field(default=None, repr=False)
    slack_bot_token: Optional[str] = field(default=None, repr=False)
    slack_channel_id: Optional[str] = None
    gemini_api_key: Optional[str] = field(default=None, repr=False)


@dataclass(frozen=True)
class StorageSettings:
    output_dir: Path
    s3_bucket_name: Optional[str]
    aws_region: Optional[str]


@dataclass(frozen=True)
class DatabaseSettings:
    path: Path


@dataclass(frozen=True)
class SwingCandidateSettings:
    """Explicitly disabled-by-default isolated swing candidate boundary."""

    enabled: bool
    database_path: Path
    portfolio_id: str
    strategy_semantics_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise TypeError("swing candidate enabled must be boolean")
        if not isinstance(self.database_path, Path) or not self.database_path.name:
            raise ValueError("swing candidate database path must identify a file")
        if not isinstance(self.portfolio_id, str) or not self.portfolio_id.strip():
            raise ValueError("swing candidate portfolio identity is required")
        if (
            not isinstance(self.strategy_semantics_version, str)
            or not self.strategy_semantics_version.strip()
        ):
            raise ValueError("swing candidate strategy semantics version is required")
        if self.enabled and not self.database_path.is_absolute():
            raise ValueError("enabled swing candidate database path must be absolute")


@dataclass(frozen=True)
class SettingsDiagnostics:
    sources: Mapping[str, str]
    warnings: Tuple[str, ...]


def _empty_mapping() -> Mapping[str, Any]:
    return MappingProxyType({})


@dataclass(frozen=True)
class LegacyMappings:
    """Explicit, immutable compatibility input for legacy JSON settings."""

    config: Mapping[str, Any] = field(default_factory=_empty_mapping, repr=False)
    strategy_config: Mapping[str, Any] = field(default_factory=_empty_mapping, repr=False)
    scoring_config: Mapping[str, Any] = field(default_factory=_empty_mapping, repr=False)
    notices: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        issues = _legacy_structure_issues(
            (
                ("CONFIG", self.config),
                ("STRATEGY_CONFIG", self.strategy_config),
                ("SCORING_CONFIG", self.scoring_config),
            )
        )
        if issues:
            raise SettingsValidationError(issues)
        object.__setattr__(self, "config", _freeze_mapping(self.config))
        object.__setattr__(
            self, "strategy_config", _freeze_mapping(self.strategy_config)
        )
        object.__setattr__(
            self, "scoring_config", _freeze_mapping(self.scoring_config)
        )
        object.__setattr__(self, "notices", tuple(self.notices))

    @classmethod
    def from_mappings(
        cls,
        config: Optional[Mapping[str, Any]] = None,
        strategy_config: Optional[Mapping[str, Any]] = None,
        scoring_config: Optional[Mapping[str, Any]] = None,
        notices: Sequence[str] = (),
    ) -> "LegacyMappings":
        return cls(
            config or {},
            strategy_config or {},
            scoring_config or {},
            tuple(notices),
        )


@dataclass(frozen=True)
class Settings:
    runtime: RuntimeSettings
    execution: ExecutionSettings
    kiwoom: KiwoomSettings
    monitoring: MonitoringSettings
    strategy: StrategySettings
    notification: NotificationSettings
    storage: StorageSettings
    database: DatabaseSettings
    swing_candidate: SwingCandidateSettings
    diagnostics: SettingsDiagnostics
    _legacy: LegacyMappings = field(repr=False)

    def __post_init__(self) -> None:
        issue = _api_environment_issue(
            self.kiwoom.api_mode,
            self.runtime.app_env,
        )
        if issue is not None:
            raise SettingsValidationError((issue,))
        if self.swing_candidate.enabled and self.swing_candidate.database_path == self.database.path:
            raise SettingsValidationError((SettingsIssue(
                "KIWOOM_SWING_CANDIDATE_DB_PATH",
                "must be isolated from KIWOOM_DB_PATH",
            ),))

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, str],
        legacy: Optional[LegacyMappings] = None,
        legacy_config: Optional[Mapping[str, Any]] = None,
        legacy_strategy_config: Optional[Mapping[str, Any]] = None,
        legacy_scoring_config: Optional[Mapping[str, Any]] = None,
        default_output_dir: Union[str, Path] = ".",
        source_name: str = "canonical mapping",
    ) -> "Settings":
        """Validate settings without reading environment variables or the filesystem."""

        explicit_legacy = (legacy_config, legacy_strategy_config, legacy_scoring_config)
        if legacy is not None and any(item is not None for item in explicit_legacy):
            raise ValueError("pass either legacy or explicit legacy mappings, not both")
        legacy_data = legacy or LegacyMappings.from_mappings(*explicit_legacy)
        issues: List[SettingsIssue] = []
        warnings = list(legacy_data.notices)
        sources: Dict[str, str] = {}
        resolver = _Resolver(mapping, legacy_data, issues, warnings, sources, source_name)
        get = resolver.get

        for name in sorted(mapping):
            if _normalized_credential_key(name) in _FORBIDDEN_NORMALIZED_KEYS:
                issues.append(
                    SettingsIssue(
                        name,
                        "must not exist; use KIWOOM_CREDENTIALS_DIR with the strict file provider",
                    )
                )
            elif name.startswith("KIWOOM_") and name not in SETTING_SPEC_BY_NAME:
                issues.append(SettingsIssue(name, "unknown canonical variable; remove it or document it"))
        warnings.extend(_unknown_legacy_warnings(legacy_data))

        api_mode = _api_mode(get("KIWOOM_API_MODE", "disabled"), issues)
        execution_mode = _execution_mode(
            get("KIWOOM_EXECUTION_MODE", ExecutionMode.CHECK_ONLY.value), issues
        )
        process_name = _required_text("KIWOOM_PROCESS_NAME", get("KIWOOM_PROCESS_NAME"), issues)
        app_env = _app_env(get("KIWOOM_APP_ENV", "local"), issues)
        credentials_dir = _path(
            "KIWOOM_CREDENTIALS_DIR",
            get("KIWOOM_CREDENTIALS_DIR", None),
            issues,
            False,
        )
        if api_mode in ("mock", "prod"):
            if credentials_dir is None:
                issues.append(
                    SettingsIssue(
                        "KIWOOM_CREDENTIALS_DIR",
                        "is required when KIWOOM_API_MODE is mock or prod",
                    )
                )
            elif not credentials_dir.is_absolute():
                issues.append(
                    SettingsIssue(
                        "KIWOOM_CREDENTIALS_DIR",
                        "must be an absolute external directory",
                    )
                )
        elif api_mode is KiwoomApiMode.DISABLED and credentials_dir is not None:
            issues.append(
                SettingsIssue(
                    "KIWOOM_CREDENTIALS_DIR",
                    "must be unset when KIWOOM_API_MODE is disabled",
                )
            )
        environment_issue = _api_environment_issue(api_mode, app_env)
        if environment_issue is not None:
            issues.append(environment_issue)
        output_dir = _path("KIWOOM_OUTPUT_DIR", get("KIWOOM_OUTPUT_DIR", str(default_output_dir)), issues, False)
        database_path = _path("KIWOOM_DB_PATH", get("KIWOOM_DB_PATH", "trades.db"), issues, True)
        swing_enabled = _strict_bool(
            get("KIWOOM_SWING_CANDIDATE_ENABLED", "false"),
            issues,
            name="KIWOOM_SWING_CANDIDATE_ENABLED",
        )
        swing_database_path = _path(
            "KIWOOM_SWING_CANDIDATE_DB_PATH",
            get("KIWOOM_SWING_CANDIDATE_DB_PATH", "./runtime/swing-candidate.sqlite3"),
            issues,
            True,
        )
        swing_portfolio_id = _required_text(
            "KIWOOM_SWING_CANDIDATE_PORTFOLIO_ID",
            get("KIWOOM_SWING_CANDIDATE_PORTFOLIO_ID", "swing-paper-v1"),
            issues,
        )
        swing_semantics_version = _required_text(
            "KIWOOM_SWING_STRATEGY_SEMANTICS_VERSION",
            get("KIWOOM_SWING_STRATEGY_SEMANTICS_VERSION", "swing-v1"),
            issues,
        )
        if swing_enabled and swing_database_path is not None and not swing_database_path.is_absolute():
            issues.append(SettingsIssue(
                "KIWOOM_SWING_CANDIDATE_DB_PATH",
                "must be absolute when KIWOOM_SWING_CANDIDATE_ENABLED=true",
            ))

        webhook_url = _url("KIWOOM_SLACK_WEBHOOK_URL", get("KIWOOM_SLACK_WEBHOOK_URL", None), issues, False)
        slack_token = _optional_text("KIWOOM_SLACK_BOT_TOKEN", get("KIWOOM_SLACK_BOT_TOKEN", None), issues)
        slack_channel = _optional_text("KIWOOM_SLACK_CHANNEL_ID", get("KIWOOM_SLACK_CHANNEL_ID", None), issues)
        gemini_key = _optional_text("KIWOOM_GEMINI_API_KEY", get("KIWOOM_GEMINI_API_KEY", None), issues)
        bucket = _s3_bucket(get("KIWOOM_S3_BUCKET_NAME", None), issues)
        aws_region = _aws_region(get("KIWOOM_AWS_REGION", None), issues)

        fast = _positive_float("KIWOOM_FAST_INTERVAL_SECONDS", get("KIWOOM_FAST_INTERVAL_SECONDS", "10"), issues)
        slow = _positive_float("KIWOOM_SLOW_INTERVAL_SECONDS", get("KIWOOM_SLOW_INTERVAL_SECONDS", "60"), issues)
        workers = _positive_int("KIWOOM_MAX_WORKERS", get("KIWOOM_MAX_WORKERS", "8"), issues)
        proxy_code = _stock_code(get("KIWOOM_MARKET_PROXY_CODE", "069500"), issues)
        max_stocks = _positive_int("KIWOOM_MAX_STOCKS", get("KIWOOM_MAX_STOCKS", "50"), issues)
        etf_keywords = _string_list(get("KIWOOM_ETF_KEYWORDS", ""), issues)

        debug_mode = _strict_bool(get("KIWOOM_DEBUG_MODE", "false"), issues)
        exit_time = _clock("KIWOOM_DAY_TRADE_EXIT_TIME", get("KIWOOM_DAY_TRADE_EXIT_TIME", "15:30"), issues)
        entry_deadline = _clock("KIWOOM_ENTRY_DEADLINE", get("KIWOOM_ENTRY_DEADLINE", "15:00"), issues)
        cumulative_score_floor = _resolve_cumulative_trade_return_score_floor(
            mapping,
            legacy_data,
            issues,
            warnings,
            sources,
            source_name,
        )
        target_stop_policy = _resolve_target_stop_settings(
            mapping,
            legacy_data,
            issues,
            warnings,
            sources,
            source_name,
        )
        regimes = _legacy_mapping(legacy_data, "regimes", issues, warnings)

        if fast is not None and slow is not None and fast > slow:
            rule = "must be less than or equal to KIWOOM_SLOW_INTERVAL_SECONDS"
            issues.append(SettingsIssue("KIWOOM_FAST_INTERVAL_SECONDS", rule))
        if (exit_time is not None and entry_deadline is not None
                and _minutes(entry_deadline) >= _minutes(exit_time)):
            issues.append(SettingsIssue("KIWOOM_ENTRY_DEADLINE", "must be earlier than KIWOOM_DAY_TRADE_EXIT_TIME"))
        if bool(slack_token) != bool(slack_channel):
            missing = "KIWOOM_SLACK_CHANNEL_ID" if slack_token else "KIWOOM_SLACK_BOT_TOKEN"
            issues.append(SettingsIssue(missing, "must be set together with its Slack counterpart"))
        if issues:
            raise SettingsValidationError(_deduplicate_issues(issues))

        if (
            api_mode is None
            or execution_mode is None
            or process_name is None
            or app_env is None
            or output_dir is None
            or database_path is None
            or swing_database_path is None
            or swing_portfolio_id is None
            or swing_semantics_version is None
            or fast is None
            or slow is None
            or workers is None
            or proxy_code is None
            or max_stocks is None
            or exit_time is None
            or entry_deadline is None
            or cumulative_score_floor is None
            or target_stop_policy is None
        ):
            raise RuntimeError("validated settings unexpectedly remained incomplete")
        return cls(
            RuntimeSettings(app_env, process_name),
            ExecutionSettings(execution_mode),
            KiwoomSettings(api_mode, credentials_dir),
            MonitoringSettings(fast, slow, workers, proxy_code, max_stocks, etf_keywords),
            StrategySettings(
                debug_mode,
                exit_time,
                entry_deadline,
                cumulative_score_floor,
                target_stop_policy,
                regimes,
            ),
            NotificationSettings(webhook_url, slack_token, slack_channel, gemini_key),
            StorageSettings(output_dir, bucket, aws_region),
            DatabaseSettings(database_path),
            SwingCandidateSettings(
                swing_enabled,
                swing_database_path,
                swing_portfolio_id,
                swing_semantics_version,
            ),
            SettingsDiagnostics(MappingProxyType(dict(sources)), tuple(warnings)),
            legacy_data,
        )

    def to_legacy_mappings(self) -> Tuple[Mapping[str, Any], Mapping[str, Any]]:
        """Create read-only compatibility views for current consumers."""

        system = _thaw_mapping(self._legacy.config)
        strategy_config = _thaw_mapping(self._legacy.strategy_config)
        for container in (system, strategy_config):
            for key in (
                *_TARGET_STOP_COMPATIBILITY_KEYS,
                *_CUMULATIVE_SCORE_MAPPING_KEYS,
            ):
                container.pop(key, None)
            nested_strategy = container.get("strategy")
            if isinstance(nested_strategy, Mapping):
                container["strategy"] = {
                    key: value
                    for key, value in nested_strategy.items()
                    if key not in (
                        *_TARGET_STOP_COMPATIBILITY_KEYS,
                        *_CUMULATIVE_SCORE_MAPPING_KEYS,
                    )
                }
        for key in _CANONICAL_SYSTEM_COMPATIBILITY_KEYS:
            strategy_config.pop(key, None)
        system.update(
            {
                "process_name": self.runtime.process_name,
                "app_env": self.runtime.app_env,
                "aws_s3_bucket_name": self.storage.s3_bucket_name,
                "aws_region": self.storage.aws_region,
                "webhook_url": self.notification.slack_webhook_url,
                "gemini_api_key": self.notification.gemini_api_key,
                "slack_token": self.notification.slack_bot_token,
                "slack_channel": self.notification.slack_channel_id,
                "fast_interval": self.monitoring.fast_interval_seconds,
                "slow_interval": self.monitoring.slow_interval_seconds,
                "max_workers": self.monitoring.max_workers,
            }
        )
        market = dict(system.get("market", {}))
        market["proxy_code"] = self.monitoring.market_proxy_code
        system["market"] = market
        filters = dict(system.get("filters", {}))
        filters.update(
            {
                "max_stocks": self.monitoring.max_stocks,
                "etf_keywords": list(self.monitoring.etf_keywords),
            }
        )
        system["filters"] = filters

        strategy_values: Dict[str, Any] = {}
        for source in (system.get("strategy", {}), strategy_config.get("strategy", {})):
            if isinstance(source, Mapping):
                strategy_values.update(
                    {
                        key: value
                        for key, value in source.items()
                        if key not in (
                            *_TARGET_STOP_COMPATIBILITY_KEYS,
                            *_CUMULATIVE_SCORE_MAPPING_KEYS,
                        )
                    }
                )
        strategy_values.update(
            {
                "debug_mode": self.strategy.debug_mode,
                "day_trade_exit_time": self.strategy.day_trade_exit_time,
                "entry_deadline": self.strategy.entry_deadline,
                "cumulative_trade_return_score_floor": (
                    self.strategy.cumulative_trade_return_score_floor
                ),
                "regimes": _thaw_mapping(self.strategy.regimes),
            }
        )
        strategy_config["strategy"] = strategy_values
        return _freeze_mapping(system), _freeze_mapping(strategy_config)

    @property
    def legacy_scoring_config(self) -> Mapping[str, Any]:
        """Expose the unused legacy view read-only during its migration window."""

        return self._legacy.scoring_config


_PROCESS_FIELDS: Dict[str, Any] = {
    spec.name: (Optional[str], None) for spec in SETTING_SPECS
}
_ProcessEnvironment = create_model(
    "_ProcessEnvironment",
    __base__=BaseSettings,
    __config__=SettingsConfigDict(extra="ignore", case_sensitive=True, frozen=True),
    **_PROCESS_FIELDS,
)


def _environment_source(issues: List[SettingsIssue]) -> BaseSettings:
    try:
        return _ProcessEnvironment(_env_file=None, _secrets_dir=None)
    except ValidationError as exc:
        for error in exc.errors(include_url=False, include_input=False):
            name = ".".join(str(part) for part in error.get("loc", ())) or "SETTINGS_SOURCE"
            issues.append(SettingsIssue(name, str(error.get("type", "invalid source"))))
        return _ProcessEnvironment.model_construct()


def load_settings_from_environment(
    legacy: Optional[LegacyMappings] = None,
    default_output_dir: Optional[Union[str, Path]] = None,
) -> Settings:
    """Read non-secret process settings and return frozen settings."""

    snapshot = EnvironmentSnapshot.capture(os.environ).values
    source_issues: List[SettingsIssue] = []
    source = _environment_source(source_issues)
    canonical = {
        name: value
        for name, value in snapshot.items()
        if name.startswith("KIWOOM_")
        or (
            name.upper().replace("_", "").replace("-", "").startswith("KIWOOM")
            and _normalized_credential_key(name) in _FORBIDDEN_NORMALIZED_KEYS
        )
    }
    canonical.update(
        {name: value for name, value in source.model_dump().items() if value is not None}
    )
    try:
        result = Settings.from_mapping(
            canonical,
            legacy=legacy,
            default_output_dir=default_output_dir or Path.cwd(),
            source_name="process environment or secret file",
        )
    except SettingsValidationError as exc:
        if source_issues:
            raise SettingsValidationError(
                _deduplicate_issues(tuple(source_issues) + exc.issues)
            ) from None
        raise
    if source_issues:
        raise SettingsValidationError(_deduplicate_issues(source_issues))
    return result


def load_legacy_json_mappings(package_name: str = "config") -> LegacyMappings:
    """Explicitly load the three supported legacy JSON resources after startup guards."""

    documents = load_legacy_documents(package_name)
    return LegacyMappings.from_mappings(
        documents.config,
        documents.strategy_config,
        documents.scoring_config,
        documents.notices,
    )


def _required_text(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():

        issues.append(SettingsIssue(name, "is required and must be a non-empty string"))
        return None
    return value.strip()


def _optional_text(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    if value is None or value is _MISSING:
        return None
    if not isinstance(value, str):
        issues.append(SettingsIssue(name, "must be a non-empty string when set"))
        return None
    text = value.strip()
    return text or None


def _url(
    name: str, value: Any, issues: List[SettingsIssue], required: bool
) -> Optional[str]:
    text = _required_text(name, value, issues) if required else _optional_text(name, value, issues)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        issues.append(SettingsIssue(name, "must be an http(s) URL with a host"))
        return None
    return text


def _app_env(value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        issues.append(SettingsIssue("KIWOOM_APP_ENV", "must be one of %s" % ", ".join(ALL_ENVIRONMENTS)))
        return None
    text = value.strip()
    normalized = text.lower()
    if normalized not in ALL_ENVIRONMENTS:
        issues.append(
            SettingsIssue("KIWOOM_APP_ENV", "must be one of %s" % ", ".join(ALL_ENVIRONMENTS))
        )
        return None
    return normalized


def _api_mode(value: Any, issues: List[SettingsIssue]) -> Optional[KiwoomApiMode]:
    if not isinstance(value, str) or value.strip().lower() not in KIWOOM_API_MODES:
        issues.append(
            SettingsIssue(
                "KIWOOM_API_MODE",
                "must be one of %s" % ", ".join(KIWOOM_API_MODES),
            )
        )
        return None
    return KiwoomApiMode(value.strip().lower())


def _execution_mode(value: Any, issues: List[SettingsIssue]) -> Optional[ExecutionMode]:
    admitted = (
        ExecutionMode.CHECK_ONLY.value,
        ExecutionMode.SHADOW_ONCE.value,
        ExecutionMode.SHADOW_CONTINUOUS.value,
    )
    rule = "must be check-only, shadow-once, or shadow-continuous; live is unavailable"
    if not isinstance(value, str):
        issues.append(SettingsIssue("KIWOOM_EXECUTION_MODE", rule))
        return None
    normalized = value.strip().lower()
    if normalized not in admitted:
        issues.append(SettingsIssue("KIWOOM_EXECUTION_MODE", rule))
        return None
    return ExecutionMode(normalized)


def _endpoint_for_mode(
    api_mode: Optional[KiwoomApiMode],
) -> Optional[KiwoomEndpoint]:
    if api_mode is KiwoomApiMode.MOCK:
        return KiwoomEndpoint.MOCK
    if api_mode is KiwoomApiMode.PROD:
        return KiwoomEndpoint.PROD
    return None


def _api_environment_issue(
    api_mode: Optional[KiwoomApiMode],
    app_env: Optional[str],
) -> Optional[SettingsIssue]:
    if api_mode is KiwoomApiMode.MOCK and app_env != "staging":
        return SettingsIssue(
            "KIWOOM_API_MODE",
            "mock mode requires KIWOOM_APP_ENV=staging",
        )
    if api_mode is KiwoomApiMode.PROD and app_env not in (
        "prod",
        "production-like",
    ):
        return SettingsIssue(
            "KIWOOM_API_MODE",
            "prod mode requires a production-class KIWOOM_APP_ENV",
        )
    return None


def _safe_path_value(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[Path]:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        issues.append(SettingsIssue(name, "must be a non-empty safe path"))
        return None
    text = value.strip()
    candidate = Path(text)
    windows = PureWindowsPath(text)
    if ".." in candidate.parts or ".." in windows.parts:
        issues.append(SettingsIssue(name, "must not contain parent-directory traversal"))
        return None
    if candidate == Path("/") or (windows.anchor and windows == PureWindowsPath(windows.anchor)):
        issues.append(SettingsIssue(name, "must not be a broad filesystem root"))
        return None
    return candidate


def _path(
    name: str, value: Any, issues: List[SettingsIssue], file_path: bool
) -> Optional[Path]:
    if name == "KIWOOM_CREDENTIALS_DIR" and (
        value is None or value is _MISSING or (isinstance(value, str) and not value.strip())
    ):
        return None
    candidate = _safe_path_value(name, value, issues)
    if candidate is not None and file_path and candidate.name in ("", ".", ".."):
        issues.append(SettingsIssue(name, "must identify a file, not a directory"))
        return None
    return candidate


def _finite_float(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[float]:
    if isinstance(value, bool):
        issues.append(SettingsIssue(name, "must be a finite number"))
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        issues.append(SettingsIssue(name, "must be a finite number"))
        return None
    if not math.isfinite(parsed):
        issues.append(SettingsIssue(name, "must be a finite number"))
        return None
    return parsed


def _positive_float(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[float]:
    parsed = _finite_float(name, value, issues)
    if parsed is not None and parsed <= 0:
        issues.append(SettingsIssue(name, "must be greater than zero"))
        return None
    return parsed


def _positive_int(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[int]:
    if isinstance(value, bool) or not (
        isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"[+]?[0-9]+", value.strip()))
    ):
        issues.append(SettingsIssue(name, "must be an integer greater than zero"))
        return None
    parsed = int(value)
    if parsed <= 0:
        issues.append(SettingsIssue(name, "must be greater than zero"))
        return None
    return parsed


def _stock_code(value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{6}", value.strip()) is None:
        issues.append(SettingsIssue("KIWOOM_MARKET_PROXY_CODE", "must contain exactly six digits"))
        return None
    return value.strip()


def _string_list(value: Any, issues: List[SettingsIssue]) -> Tuple[str, ...]:
    if isinstance(value, str):
        if not value.strip():
            return ()
        items = [item.strip() for item in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        issues.append(SettingsIssue("KIWOOM_ETF_KEYWORDS", "must be a comma-separated string list"))
        return ()
    if any(not isinstance(item, str) or not item.strip() for item in items):
        issues.append(SettingsIssue("KIWOOM_ETF_KEYWORDS", "must contain only non-empty strings"))
        return ()
    normalized = tuple(item.strip() for item in items)
    if len(set(normalized)) != len(normalized):
        issues.append(SettingsIssue("KIWOOM_ETF_KEYWORDS", "must not contain duplicate values"))
        return ()
    return normalized


def _strict_bool(
    value: Any,
    issues: List[SettingsIssue],
    *,
    name: str = "KIWOOM_DEBUG_MODE",
) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    issues.append(SettingsIssue(name, "must be exactly true or false"))
    return False


def _clock(name: str, value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{2}:[0-9]{2}", value.strip()) is None:
        issues.append(SettingsIssue(name, "must use valid 24-hour HH:MM format"))
        return None
    text = value.strip()
    try:
        time.fromisoformat(text)
    except ValueError:
        issues.append(SettingsIssue(name, "must use valid 24-hour HH:MM format"))
        return None
    return text


def _resolve_cumulative_trade_return_score_floor(
    mapping: Mapping[str, str],
    legacy: LegacyMappings,
    issues: List[SettingsIssue],
    warnings: List[str],
    sources: Dict[str, str],
    source_name: str,
) -> Optional[float]:
    """Resolve the canonical floor and its one-window deprecated inputs."""
    canonical_present = _CUMULATIVE_SCORE_CANONICAL_NAME in mapping
    deprecated_values: List[Tuple[str, Any]] = []
    if _CUMULATIVE_SCORE_DEPRECATED_ENV_NAME in mapping:
        deprecated_values.append(
            (
                _CUMULATIVE_SCORE_DEPRECATED_ENV_NAME,
                mapping[_CUMULATIVE_SCORE_DEPRECATED_ENV_NAME],
            )
        )
    for container_name, path in _LEGACY_CANDIDATES[
        _CUMULATIVE_SCORE_DEPRECATED_ENV_NAME
    ]:
        container = (
            legacy.config if container_name == "CONFIG" else legacy.strategy_config
        )
        value = _lookup(container, path)
        if value is not _MISSING:
            deprecated_values.append(
                (f"{container_name}.{'.'.join(path)}", value)
            )

    canonical_value: Optional[float] = None
    if canonical_present:
        canonical_value = _finite_float(
            _CUMULATIVE_SCORE_CANONICAL_NAME,
            mapping[_CUMULATIVE_SCORE_CANONICAL_NAME],
            issues,
        )
        sources[_CUMULATIVE_SCORE_CANONICAL_NAME] = source_name

    parsed_deprecated: List[Tuple[str, float]] = []
    for label, raw_value in deprecated_values:
        parsed = _finite_float(label, raw_value, issues)
        if parsed is not None:
            parsed_deprecated.append((label, parsed))

    if deprecated_values:
        warnings.append(
            "Deprecated cumulative score floor input(s) %s; migrate to %s."
            % (
                ", ".join(label for label, _ in deprecated_values),
                _CUMULATIVE_SCORE_CANONICAL_NAME,
            )
        )

    if canonical_present:
        floor = canonical_value
        if (
            floor is not None
            and len(parsed_deprecated) == len(deprecated_values)
            and any(value != floor for _, value in parsed_deprecated)
        ):
            issues.append(
                SettingsIssue(
                    _CUMULATIVE_SCORE_CANONICAL_NAME,
                    "conflicts with a deprecated cumulative score floor input",
                )
            )
            floor = None
    elif deprecated_values:
        if len(parsed_deprecated) != len(deprecated_values):
            floor = None
        elif not _all_equal([value for _, value in parsed_deprecated]):
            issues.append(
                SettingsIssue(
                    _CUMULATIVE_SCORE_CANONICAL_NAME,
                    "deprecated cumulative score floor inputs conflict",
                )
            )
            floor = None
        else:
            floor = parsed_deprecated[0][1]
            sources[_CUMULATIVE_SCORE_CANONICAL_NAME] = parsed_deprecated[0][0]
    else:
        floor = -5.0
        sources[_CUMULATIVE_SCORE_CANONICAL_NAME] = "default"

    if floor is not None and floor > 0:
        issues.append(
            SettingsIssue(
                _CUMULATIVE_SCORE_CANONICAL_NAME,
                "must be less than or equal to zero",
            )
        )
        return None
    return floor


def _s3_bucket(value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    text = _optional_text("KIWOOM_S3_BUCKET_NAME", value, issues)
    if text is None:
        return None
    valid = (
        3 <= len(text) <= 63
        and re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", text) is not None
        and ".." not in text
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", text) is None
    )
    if not valid:
        issues.append(SettingsIssue("KIWOOM_S3_BUCKET_NAME", "must be a valid lowercase S3 bucket name"))
        return None
    return text


def _aws_region(value: Any, issues: List[SettingsIssue]) -> Optional[str]:
    text = _optional_text("KIWOOM_AWS_REGION", value, issues)
    if text is None:
        return None
    if re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+", text) is None:
        issues.append(
            SettingsIssue("KIWOOM_AWS_REGION", "must use a lowercase region such as ap-northeast-2")
        )
        return None
    return text


def _legacy_values(legacy: LegacyMappings, key: str) -> List[Tuple[str, Any]]:
    paths = (("strategy", key), (key,))
    result: List[Tuple[str, Any]] = []
    for source_name, source in (
        ("CONFIG", legacy.config),
        ("STRATEGY_CONFIG", legacy.strategy_config),
    ):
        for path in paths:
            value = _lookup(source, path)
            if value is not _MISSING:
                result.append(("%s.%s" % (source_name, ".".join(path)), value))
    return result


def _scan_legacy_target_stop_groups(
    legacy: LegacyMappings,
    issues: List[SettingsIssue],
) -> Tuple[bool, Tuple[str, ...]]:
    config_strategy = legacy.config.get("strategy")
    strategy_strategy = legacy.strategy_config.get("strategy")
    groups: Tuple[Tuple[str, Mapping[str, Any]], ...] = (
        ("CONFIG", legacy.config),
        (
            "CONFIG.strategy",
            config_strategy if isinstance(config_strategy, Mapping) else {},
        ),
        ("STRATEGY_CONFIG", legacy.strategy_config),
        (
            "STRATEGY_CONFIG.strategy",
            strategy_strategy if isinstance(strategy_strategy, Mapping) else {},
        ),
    )
    any_present = False
    exact_labels: List[str] = []
    complete_pairs: List[Tuple[str, Any, Any]] = []
    for label, group in groups:
        has_target = "target_profit_rate" in group
        has_stop = "stop_loss_rate" in group
        if not has_target and not has_stop:
            continue
        any_present = True
        issue_name = f"LEGACY.{label}.target_stop"
        if has_target != has_stop:
            missing = "stop_loss_rate" if has_target else "target_profit_rate"
            issues.append(
                SettingsIssue(
                    issue_name,
                    f"is an orphan group missing {missing}",
                )
            )
            continue
        target = group["target_profit_rate"]
        stop = group["stop_loss_rate"]
        complete_pairs.append((label, target, stop))
        if (
            isinstance(target, bool)
            or isinstance(stop, bool)
            or not isinstance(target, Real)
            or not isinstance(stop, Real)
            or not math.isfinite(float(target))
            or not math.isfinite(float(stop))
            or target != 0.03
            or stop != -0.03
        ):
            issues.append(
                SettingsIssue(
                    issue_name,
                    "only the exact numeric pair 0.03/-0.03 can be migrated",
                )
            )
            continue
        exact_labels.append(label)

    if len(complete_pairs) > 1 and not _all_equal(
        [(target, stop) for _, target, stop in complete_pairs]
    ):
        issues.append(
            SettingsIssue(
                "LEGACY.target_stop",
                "complete target/stop groups have conflicting values",
            )
        )
    return any_present, tuple(exact_labels)


def _resolve_target_stop_settings(
    canonical: Mapping[str, Any],
    legacy: LegacyMappings,
    issues: List[SettingsIssue],
    warnings: List[str],
    sources: Dict[str, str],
    source_name: str,
) -> Optional[TargetStopPolicy]:
    present = set(canonical).intersection(_TARGET_STOP_CANONICAL_NAMES)
    issue_count = len(issues)
    legacy_present, legacy_labels = _scan_legacy_target_stop_groups(legacy, issues)
    legacy_is_valid = legacy_present and len(issues) == issue_count

    if present:
        missing = set(_TARGET_STOP_CANONICAL_NAMES).difference(present)
        for name in sorted(missing):
            issues.append(
                SettingsIssue(name, "must be set with the complete target/stop atomic group")
            )
            sources[name] = "missing"
        for name in sorted(present):
            sources[name] = source_name
        version_value = canonical.get("KIWOOM_TARGET_STOP_UNIT_VERSION")
        if version_value != TARGET_STOP_UNIT_VERSION:
            issues.append(
                SettingsIssue(
                    "KIWOOM_TARGET_STOP_UNIT_VERSION",
                    "must be exactly percentage-points-v1",
                )
            )
            version: Optional[str] = None
        else:
            version = TARGET_STOP_UNIT_VERSION
        target = (
            _positive_float(
                "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS",
                canonical.get("KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS", _MISSING),
                issues,
            )
            if "KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS" in canonical
            else None
        )
        stop = (
            _positive_float(
                "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS",
                canonical.get("KIWOOM_STOP_LOSS_PERCENTAGE_POINTS", _MISSING),
                issues,
            )
            if "KIWOOM_STOP_LOSS_PERCENTAGE_POINTS" in canonical
            else None
        )
        if legacy_is_valid:
            warnings.append(
                "Canonical target/stop group overrides deprecated exact legacy pair from %s."
                % ", ".join(legacy_labels)
            )
        if version is None or target is None or stop is None:
            return None
        try:
            return TargetStopPolicy(version, target, stop)
        except StrategySemanticsValidationError:
            issues.append(
                SettingsIssue(
                    "KIWOOM_TARGET_STOP_POLICY",
                    "must define one valid percentage-points-v1 policy",
                )
            )
            return None

    if legacy_present:
        provenance = ", ".join(legacy_labels)
        for name in _TARGET_STOP_CANONICAL_NAMES:
            sources[name] = "normalized legacy pair: " + provenance
        if legacy_is_valid:
            warnings.append(
                "Deprecated exact legacy target/stop pair 0.03/-0.03 from %s was normalized "
                "to 3.0/3.0 percentage-points-v1."
                % provenance
            )
            return TargetStopPolicy()
        return None

    for name in _TARGET_STOP_CANONICAL_NAMES:
        sources[name] = "default"
    return TargetStopPolicy()


def _legacy_mapping(
    legacy: LegacyMappings,
    key: str,
    issues: List[SettingsIssue],
    warnings: List[str],
) -> Mapping[str, Any]:
    values = _legacy_values(legacy, key)
    if not values:
        return _empty_mapping()
    if not _all_equal([value for _, value in values]):
        issues.append(SettingsIssue("LEGACY.strategy.%s" % key, "has conflicting legacy values"))
    warnings.append(
        "Legacy strategy key %s is preserved but has no active canonical setting." % key
    )
    if not isinstance(values[0][1], Mapping):
        issues.append(SettingsIssue("LEGACY.strategy.%s" % key, "must be an object mapping"))
        return _empty_mapping()
    return _freeze_mapping(values[0][1])


def _unknown_legacy_warnings(legacy: LegacyMappings) -> List[str]:
    known: Set[str] = {
        path[0]
        for candidates in _LEGACY_CANDIDATES.values()
        for _, path in candidates
    }
    known.update(
        {
            "strategy",
            "target_profit_rate",
            "stop_loss_rate",
            "regimes",
            "score_decay_rate",
            "momentum_threshold",
        }
    )
    result: List[str] = []
    for source_name, source in (
        ("CONFIG", legacy.config),
        ("STRATEGY_CONFIG", legacy.strategy_config),
    ):
        for key in sorted(set(source).difference(known)):
            result.append(
                "Unknown legacy key %s.%s is preserved for compatibility; review it."
                % (source_name, key)
            )
    for key in sorted(set(legacy.strategy_config).intersection(_CANONICAL_SYSTEM_COMPATIBILITY_KEYS)):
        result.append(
            "Legacy key STRATEGY_CONFIG.%s cannot override canonical system compatibility values; "
            "migrate it to KIWOOM_*."
            % key
        )
    if legacy.scoring_config:
        result.append("SCORING_CONFIG is preserved but has no tracked production consumer.")
    config_strategy = legacy.config.get("strategy")
    strategy_strategy = legacy.strategy_config.get("strategy")
    if isinstance(config_strategy, Mapping) and isinstance(strategy_strategy, Mapping):
        known_strategy = set(_CANONICAL_STRATEGY_KEYS)
        for key in sorted(set(config_strategy).intersection(strategy_strategy).difference(known_strategy)):
            if not _all_equal([config_strategy[key], strategy_strategy[key]]):
                result.append(
                    "Legacy strategy key CONFIG.strategy.%s conflicts with STRATEGY_CONFIG.strategy.%s; "
                    "STRATEGY_CONFIG keeps precedence for compatibility."
                    % (key, key)
                )
    return result


def _minutes(value: str) -> int:
    parsed = time.fromisoformat(value)
    return parsed.hour * 60 + parsed.minute


def _normalized_credential_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.lower().replace("_", "").replace("-", "")


def _legacy_structure_issues(
    sources: Sequence[Tuple[str, Mapping[str, Any]]],
) -> Tuple[SettingsIssue, ...]:
    """Cycle-safely inspect all JSON-like legacy containers."""

    issues: List[SettingsIssue] = []
    visited: Set[int] = set()
    active: Set[int] = set()

    def visit(source_name: str, value: Any, path: Tuple[str, ...]) -> None:
        if not isinstance(value, (Mapping, list, tuple)):
            return
        identity = id(value)
        if identity in active:
            issues.append(
                SettingsIssue(
                    "LEGACY.%s.%s" % (source_name, ".".join(path) or "<root>"),
                    "cyclic containers are forbidden",
                )
            )
            return
        if identity in visited:
            return
        active.add(identity)
        visited.add(identity)
        items: Iterable[Tuple[object, Any]]
        if isinstance(value, Mapping):
            items = value.items()
        else:
            items = ((str(index), item) for index, item in enumerate(value))
        for key, item in items:
            key_text = str(key)
            current = path + (key_text,)
            if (
                isinstance(value, Mapping)
                and _normalized_credential_key(key) in _FORBIDDEN_NORMALIZED_KEYS
            ):
                issues.append(
                    SettingsIssue(
                        "LEGACY.%s.%s" % (source_name, ".".join(current)),
                        "credential keys are forbidden in legacy mappings",
                    )
                )
            visit(source_name, item, current)
        active.remove(identity)

    for source_name, source in sources:
        if not isinstance(source, Mapping):
            issues.append(
                SettingsIssue(
                    "LEGACY.%s" % source_name,
                    "must be a mapping",
                )
            )
            continue
        visit(source_name, source, ())
    return tuple(issues)


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({str(key): _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value


def _thaw_mapping(value: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: _thaw_value(item) for key, item in value.items()}


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value


def _deduplicate_issues(issues: Sequence[SettingsIssue]) -> Tuple[SettingsIssue, ...]:
    seen: Set[Tuple[str, str]] = set()
    result: List[SettingsIssue] = []
    for issue in issues:
        key = (issue.name, issue.rule)
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return tuple(result)
