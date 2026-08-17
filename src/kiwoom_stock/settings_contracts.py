"""Pure settings contracts and the canonical environment registry."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Tuple

from kiwoom_stock.domain.strategy import TARGET_STOP_UNIT_VERSION


ALL_ENVIRONMENTS = ("local", "dev", "test", "staging", "prod", "production-like")
CONFIGURATION_HELP = ".env.example and docs/configuration.md"
MISSING = object()


@dataclass(frozen=True)
class SettingSpec:
    """Machine-readable contract for one canonical environment variable."""

    name: str
    value_type: str
    required: str
    default: Optional[str]
    consumer: str
    sensitive: bool
    environments: Tuple[str, ...]
    validation: str


def _spec(
    name: str,
    value_type: str,
    required: str,
    default: Optional[str],
    consumer: str,
    sensitive: bool,
    validation: str,
    environments: Tuple[str, ...] = ALL_ENVIRONMENTS,
) -> SettingSpec:
    return SettingSpec(
        name, value_type, required, default, consumer, sensitive, environments, validation
    )


_SETTING_SPEC_ROWS: Tuple[Tuple[Any, ...], ...] = (
    ("KIWOOM_EXECUTION_MODE", "enum", "no", "check-only", "execution policy", False,
     "one of check-only/shadow-once/shadow-continuous; live unavailable"),
    ("KIWOOM_SWING_CANDIDATE_ENABLED", "strict boolean", "no", "false",
     "isolated swing shadow candidate", False,
     "exactly true or false; false is the fail-closed default"),
    ("KIWOOM_SWING_CANDIDATE_DB_PATH", "file path", "candidate enabled", "./runtime/swing-candidate.sqlite3",
     "isolated swing candidate ledger", False,
     "absolute isolated file path when candidate is enabled"),
    ("KIWOOM_SWING_CANDIDATE_PORTFOLIO_ID", "string", "candidate enabled", "swing-paper-v1",
     "isolated swing candidate portfolio", False,
     "non-empty isolated portfolio identity"),
    ("KIWOOM_SWING_STRATEGY_SEMANTICS_VERSION", "string", "no", "swing-v1",
     "swing candidate policy", False,
     "non-empty immutable strategy semantics version"),
    ("KIWOOM_IMAGE_REF", "OCI image digest", "shadow execution", None,
     "shadow activation attestation", False,
     "exact ghcr.io/spicechicken/kiwoom_stock@sha256 digest", ("prod", "production-like")),
    ("KIWOOM_IMAGE_DIGEST", "OCI image digest", "shadow execution", None,
     "shadow activation attestation", False,
     "exact ghcr.io/spicechicken/kiwoom_stock@sha256 digest", ("prod", "production-like")),
    ("KIWOOM_REQUIRE_SHADOW_VOLUME", "strict boolean", "shadow execution", None,
     "shadow volume attestation", False,
     "exactly 1 when the admitted named volume is required", ("prod", "production-like")),
    ("KIWOOM_API_MODE", "enum", "no", "disabled", "runtime composition", False,
     "one of disabled/mock/prod"),
    ("KIWOOM_PROCESS_NAME", "string", "yes", None, "runtime lifecycle", False, "non-empty"),
    ("KIWOOM_APP_ENV", "enum", "no", "local", "retention policy", False,
     "one of local/dev/test/staging/prod/production-like"),
    ("KIWOOM_CREDENTIALS_DIR", "absolute directory path", "for mock/prod", None,
     "strict credential provider", False,
     "absolute external directory for mock/prod", ("staging", "prod", "production-like")),
    ("KIWOOM_OUTPUT_DIR", "directory path", "no", "current working directory", "report output", False,
     "non-empty, no traversal, not filesystem root"),
    ("KIWOOM_DB_PATH", "file path", "no", "trades.db",
     "runtime and post-market SQLite", False,
     "non-empty, no traversal, not filesystem root"),
    ("KIWOOM_SLACK_WEBHOOK_URL", "URL", "no", None, "Slack webhook", True,
     "http(s) URL with host when set"),
    ("KIWOOM_SLACK_BOT_TOKEN", "string", "with channel ID", None, "Slack file upload", True,
     "non-empty and paired with channel ID"),
    ("KIWOOM_SLACK_CHANNEL_ID", "string", "with bot token", None, "Slack file upload", False,
     "non-empty and paired with bot token"),
    ("KIWOOM_GEMINI_API_KEY", "string", "no", None, "Gemini reports", True, "non-empty when set"),
    ("KIWOOM_S3_BUCKET_NAME", "string", "no; production-class missing preserves outputs", None, "S3 archive", False,
     "valid lowercase S3 bucket name when set", ("prod", "production-like")),
    ("KIWOOM_AWS_REGION", "string", "no", None, "future AWS session", False,
     "lowercase AWS region form when set", ("staging", "prod", "production-like")),
    ("KIWOOM_FAST_INTERVAL_SECONDS", "positive float", "no", "10", "TradingEngine", False,
     "> 0 and <= slow interval"),
    ("KIWOOM_SLOW_INTERVAL_SECONDS", "positive float", "no", "60", "TradingEngine", False,
     "> 0 and >= fast interval"),
    ("KIWOOM_MAX_WORKERS", "positive integer", "no", "8", "TradingEngine", False, "> 0"),
    ("KIWOOM_MARKET_PROXY_CODE", "six-digit string", "no", "069500", "MarketAnalyzer", False,
     "exactly six digits"),
    ("KIWOOM_MAX_STOCKS", "positive integer", "no", "50", "StockManager", False, "> 0"),
    ("KIWOOM_ETF_KEYWORDS", "comma-separated strings", "no", "empty list", "StockManager", False,
     "trimmed unique non-empty values"),
    ("KIWOOM_DEBUG_MODE", "strict boolean", "no", "false", "TradingStrategy", False,
     "exactly true or false"),
    ("KIWOOM_DAY_TRADE_EXIT_TIME", "HH:MM", "no", "15:30", "TradingStrategy", False,
     "valid 24-hour HH:MM"),
    ("KIWOOM_ENTRY_DEADLINE", "HH:MM", "no", "15:00", "TradingStrategy", False,
     "valid HH:MM earlier than exit time"),
    ("KIWOOM_CUMULATIVE_TRADE_RETURN_SCORE_FLOOR", "float percentage points", "no", "-5",
     "TradingStrategy", False, "finite and <= 0"),
    ("KIWOOM_TOTAL_LOSS_LIMIT", "deprecated float percentage points", "no", None,
     "settings migration only", False,
     "deprecated input; must equal the canonical score floor when both are set"),
    ("KIWOOM_TARGET_STOP_UNIT_VERSION", "enum", "atomic group", TARGET_STOP_UNIT_VERSION,
     "TradingStrategy", False, "exactly percentage-points-v1; all three target/stop settings together"),
    ("KIWOOM_TARGET_PROFIT_PERCENTAGE_POINTS", "positive float percentage points", "atomic group", "3.0",
     "TradingStrategy", False, "finite and > 0; all three target/stop settings together"),
    ("KIWOOM_STOP_LOSS_PERCENTAGE_POINTS", "positive float percentage points", "atomic group", "3.0",
     "TradingStrategy", False, "finite and > 0; all three target/stop settings together"),
)

SETTING_SPECS: Tuple[SettingSpec, ...] = tuple(_spec(*row) for row in _SETTING_SPEC_ROWS)
SETTING_SPEC_BY_NAME: Mapping[str, SettingSpec] = MappingProxyType(
    {item.name: item for item in SETTING_SPECS}
)


@dataclass(frozen=True)
class SettingsIssue:
    name: str
    rule: str


class SettingsValidationError(ValueError):
    """Aggregate validation error that never includes submitted values."""

    def __init__(self, issues: Sequence[SettingsIssue]):
        self.issues = tuple(issues)
        lines = ["Invalid application settings (%d issue(s)):" % len(self.issues)]
        lines.extend("- %s: %s" % (issue.name, issue.rule) for issue in self.issues)
        lines.append("Resolve the listed variables using %s." % CONFIGURATION_HELP)
        super().__init__("\n".join(lines))
