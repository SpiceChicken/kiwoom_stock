"""Fail-closed capability policy for operational entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from pathlib import Path


SHADOW_STOCK_CODE = "005930"
SHADOW_PROXY_CODE = "069500"
SHADOW_MAX_HTTP_ATTEMPTS = 23
SHADOW_MAX_CYCLES = 1
# Keep both bounded shadow modes on the isolated shadow ledger, separate from
# the legacy trading ledger file.
SHADOW_DATABASE_PATH = Path("/var/lib/kiwoom/shadow-trades.db")
SHADOW_PROCESS_LOCK_PATH = Path("/var/lib/kiwoom/shadow-worker.lock")

_SOURCE_SHA = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_DIGEST = re.compile(
    r"^ghcr\.io/spicechicken/kiwoom_stock@sha256:[0-9a-f]{64}$"
)
_ACTIVATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ExecutionPolicyError(RuntimeError):
    """The requested capability set is not admitted."""


class LiveActivationNotImplemented(ExecutionPolicyError):
    """Live brokerage execution is intentionally unavailable."""


class ExecutionMode(str, Enum):
    CHECK_ONLY = "check-only"
    SHADOW_ONCE = "shadow-once"
    SHADOW_CONTINUOUS = "shadow-continuous"
    LIVE = "live"


@dataclass(frozen=True)
class ActivationTuple:
    """Non-secret immutable identity for one bounded activation."""

    source_sha: str
    image_digest: str
    activation_id: str

    def __post_init__(self) -> None:
        if _SOURCE_SHA.fullmatch(self.source_sha) is None:
            raise ExecutionPolicyError("activation source SHA must be 40 lowercase hex characters")
        if _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ExecutionPolicyError("activation image digest is outside the approved GHCR repository")
        if _ACTIVATION_ID.fullmatch(self.activation_id) is None:
            raise ExecutionPolicyError("activation ID must use the approved non-secret identifier form")


@dataclass(frozen=True)
class ExecutionPolicy:
    """Frozen capability bundle; callers cannot combine individual booleans."""

    mode: ExecutionMode
    activation: ActivationTuple
    stock_code: str = SHADOW_STOCK_CODE
    proxy_code: str = SHADOW_PROXY_CODE
    max_cycles: int = SHADOW_MAX_CYCLES
    max_http_attempts: int = SHADOW_MAX_HTTP_ATTEMPTS
    shadow_database_path: Path = field(default=SHADOW_DATABASE_PATH, init=False)
    market_reads: bool = True
    paper_ledger_writes: bool = True
    account_reads: bool = False
    broker_orders: bool = False
    oauth_revoke: bool = False
    external_notifications: bool = False
    reports: bool = False
    swing_candidate_enabled: bool = False
    swing_candidate_database_path: Path | None = None
    swing_candidate_portfolio_id: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in (
            ExecutionMode.SHADOW_ONCE,
            ExecutionMode.SHADOW_CONTINUOUS,
        ):
            raise ExecutionPolicyError("only an admitted shadow mode can construct the shadow policy")
        expected = (
            self.stock_code == SHADOW_STOCK_CODE
            and self.proxy_code == SHADOW_PROXY_CODE
            and self.max_cycles == 1
            and self.max_http_attempts == 23
            and self.shadow_database_path == SHADOW_DATABASE_PATH
            and self.market_reads
            and self.paper_ledger_writes
            and not self.account_reads
            and not self.broker_orders
            and not self.oauth_revoke
            and not self.external_notifications
            and not self.reports
        )
        if not expected:
            raise ExecutionPolicyError("shadow capability invariant was modified")
        if self.swing_candidate_enabled:
            if (
                self.swing_candidate_database_path is None
                or not isinstance(self.swing_candidate_database_path, Path)
                or not self.swing_candidate_database_path.is_absolute()
                or self.swing_candidate_database_path == self.shadow_database_path
                or not isinstance(self.swing_candidate_portfolio_id, str)
                or not self.swing_candidate_portfolio_id.strip()
            ):
                raise ExecutionPolicyError(
                    "enabled swing candidate requires an isolated database and portfolio"
                )
        elif (
            self.swing_candidate_database_path is not None
            or self.swing_candidate_portfolio_id is not None
        ):
            raise ExecutionPolicyError(
                "disabled swing candidate cannot carry candidate identity"
            )

    @classmethod
    def for_request(
        cls,
        mode: ExecutionMode,
        activation: ActivationTuple | None,
        *,
        swing_candidate_enabled: bool = False,
        swing_candidate_database_path: Path | None = None,
        swing_candidate_portfolio_id: str | None = None,
    ) -> "ExecutionPolicy":
        if mode is ExecutionMode.LIVE:
            raise LiveActivationNotImplemented("live execution is not implemented")
        if mode not in (ExecutionMode.SHADOW_ONCE, ExecutionMode.SHADOW_CONTINUOUS):
            raise ExecutionPolicyError("an admitted shadow execution mode is required")
        if activation is None:
            raise ExecutionPolicyError("shadow execution requires the exact activation tuple")
        return cls(
            mode=mode,
            activation=activation,
            swing_candidate_enabled=swing_candidate_enabled,
            swing_candidate_database_path=swing_candidate_database_path,
            swing_candidate_portfolio_id=swing_candidate_portfolio_id,
        )

    def assert_swing_candidate_identity(
        self,
        *,
        enabled: bool,
        database_path: Path | None,
        portfolio_id: str | None,
    ) -> None:
        if self.swing_candidate_enabled is not enabled:
            raise ExecutionPolicyError("swing candidate enablement drifted between policy and settings")
        if enabled and (
            self.swing_candidate_database_path != database_path
            or self.swing_candidate_portfolio_id != portfolio_id
        ):
            raise ExecutionPolicyError("swing candidate identity drifted between policy and settings")

    def assert_paper_transition(self) -> None:
        """Authorize only the isolated shadow paper-ledger transition."""

        if self.mode not in (
            ExecutionMode.SHADOW_ONCE,
            ExecutionMode.SHADOW_CONTINUOUS,
        ) or not self.paper_ledger_writes:
            raise ExecutionPolicyError("paper transition is not admitted")

    def assert_broker_orders_disabled(self) -> None:
        if self.broker_orders:
            raise ExecutionPolicyError("broker order capability is forbidden in shadow execution")

    def assert_shadow_database_identity(
        self,
        configured_path: Path,
    ) -> Path:
        """Bind schema/open/write to the exact isolated shadow ledger identity."""

        configured = Path(configured_path)
        expected = SHADOW_DATABASE_PATH
        if not configured.is_absolute() or configured != expected:
            raise ExecutionPolicyError("configured database is not the admitted shadow ledger")
        current = configured
        while current != current.parent:
            if current.is_symlink():
                raise ExecutionPolicyError("shadow database aliases and symlinks are forbidden")
            current = current.parent
        resolved = configured.resolve(strict=False)
        if resolved != expected.resolve(strict=False):
            raise ExecutionPolicyError("shadow database aliases and symlinks are forbidden")
        if configured.exists():
            try:
                if configured.stat().st_nlink != 1:
                    raise ExecutionPolicyError(
                        "shadow database must have exactly one filesystem link"
                    )
            except OSError:
                raise ExecutionPolicyError(
                    "shadow database identity could not be verified"
                ) from None
        return configured
