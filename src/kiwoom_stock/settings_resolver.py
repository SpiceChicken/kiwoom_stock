"""Canonical-over-legacy settings precedence and source diagnostics."""

from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Protocol, Sequence, Tuple

from kiwoom_stock.settings_contracts import MISSING, SettingsIssue


class LegacySettingsSource(Protocol):
    @property
    def config(self) -> Mapping[str, Any]: ...

    @property
    def strategy_config(self) -> Mapping[str, Any]: ...


LEGACY_CANDIDATES: Mapping[str, Tuple[Tuple[str, Tuple[str, ...]], ...]] = MappingProxyType(
    {
        "KIWOOM_PROCESS_NAME": (("CONFIG", ("process_name",)), ("STRATEGY_CONFIG", ("process_name",))),
        "KIWOOM_APP_ENV": (("CONFIG", ("app_env",)), ("STRATEGY_CONFIG", ("app_env",))),
        "KIWOOM_SLACK_WEBHOOK_URL": (("CONFIG", ("webhook_url",)),),
        "KIWOOM_SLACK_BOT_TOKEN": (("CONFIG", ("slack_token",)),),
        "KIWOOM_SLACK_CHANNEL_ID": (("CONFIG", ("slack_channel",)),),
        "KIWOOM_GEMINI_API_KEY": (("CONFIG", ("gemini_api_key",)),),
        "KIWOOM_S3_BUCKET_NAME": (("CONFIG", ("aws_s3_bucket_name",)),),
        "KIWOOM_AWS_REGION": (("CONFIG", ("aws_region",)),),
        "KIWOOM_FAST_INTERVAL_SECONDS": (("CONFIG", ("fast_interval",)),),
        "KIWOOM_SLOW_INTERVAL_SECONDS": (("CONFIG", ("slow_interval",)),),
        "KIWOOM_MAX_WORKERS": (("CONFIG", ("max_workers",)),),
        "KIWOOM_MARKET_PROXY_CODE": (("CONFIG", ("market", "proxy_code")),),
        "KIWOOM_MAX_STOCKS": (("CONFIG", ("filters", "max_stocks")),),
        "KIWOOM_ETF_KEYWORDS": (("CONFIG", ("filters", "etf_keywords")),),
        "KIWOOM_DEBUG_MODE": (
            ("CONFIG", ("strategy", "debug_mode")),
            ("STRATEGY_CONFIG", ("strategy", "debug_mode")),
        ),
        "KIWOOM_DAY_TRADE_EXIT_TIME": (
            ("CONFIG", ("strategy", "day_trade_exit_time")),
            ("STRATEGY_CONFIG", ("strategy", "day_trade_exit_time")),
        ),
        "KIWOOM_ENTRY_DEADLINE": (
            ("CONFIG", ("strategy", "entry_deadline")),
            ("STRATEGY_CONFIG", ("strategy", "entry_deadline")),
        ),
        "KIWOOM_TOTAL_LOSS_LIMIT": (
            ("CONFIG", ("strategy", "total_loss_limit")),
            ("STRATEGY_CONFIG", ("strategy", "total_loss_limit")),
        ),
    }
)


def lookup(mapping: Mapping[str, Any], path: Tuple[str, ...]) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return MISSING
        current = current[key]
    return current


def all_equal(values: Sequence[Any]) -> bool:
    if not values:
        return True
    first = values[0]
    for value in values[1:]:
        try:
            if not bool(value == first):
                return False
        except Exception:
            return False
    return True


class SettingsResolver:
    """Resolve one canonical value while retaining source diagnostics."""

    def __init__(
        self,
        canonical: Mapping[str, Any],
        legacy: LegacySettingsSource,
        issues: List[SettingsIssue],
        warnings: List[str],
        sources: Dict[str, str],
        source_name: str,
    ) -> None:
        self.canonical = canonical
        self.legacy = legacy
        self.issues = issues
        self.warnings = warnings
        self.sources = sources
        self.source_name = source_name

    def get(self, name: str, default: Any = MISSING) -> Any:
        hits = self._legacy_hits(name)
        if name in self.canonical:
            if hits:
                self.warnings.append(
                    "%s overrides deprecated legacy source(s): %s."
                    % (name, ", ".join(label for label, _ in hits))
                )
            self.sources[name] = self.source_name
            return self.canonical[name]
        if hits:
            if not all_equal([value for _, value in hits]):
                self.issues.append(
                    SettingsIssue(name, "conflicting legacy values exist; set the canonical variable")
                )
            elif len(hits) > 1:
                self.warnings.append(
                    "%s is duplicated across legacy sources: %s."
                    % (name, ", ".join(label for label, _ in hits))
                )
            self.warnings.append(
                "%s uses deprecated legacy source %s; migrate to the canonical variable."
                % (name, hits[0][0])
            )
            self.sources[name] = hits[0][0]
            return hits[0][1]
        if default is not MISSING:
            self.sources[name] = "default"
            return default
        self.sources[name] = "missing"
        return MISSING

    def _legacy_hits(self, name: str) -> List[Tuple[str, Any]]:
        result: List[Tuple[str, Any]] = []
        for source_name, path in LEGACY_CANDIDATES.get(name, ()):
            source = self.legacy.config if source_name == "CONFIG" else self.legacy.strategy_config
            value = lookup(source, path)
            if value is not MISSING:
                result.append(("%s.%s" % (source_name, ".".join(path)), value))
        return result
