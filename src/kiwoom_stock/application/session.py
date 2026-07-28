"""Immutable trading-session termination outcomes."""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple


class SessionEndReason(str, Enum):
    """Reason that the monitoring session stopped."""

    MARKET_CLOSED = "market_closed"
    USER_INTERRUPT = "user_interrupt"
    KILL_SWITCH = "kill_switch"


class CriticalNotificationOutcome(str, Enum):
    """Observable outcome of the critical-notifier callable."""

    NOT_APPLICABLE = "not_applicable"
    CALL_RETURNED = "call_returned"
    CALL_RAISED = "call_raised"


@dataclass(frozen=True)
class TradingSessionResult:
    """Terminal session result passed from the engine to the process root."""

    reason: SessionEndReason
    total_pnl: Optional[float] = None
    loss_limit: Optional[float] = None
    unresolved_position_codes: Tuple[str, ...] = ()
    critical_notification_outcome: CriticalNotificationOutcome = (
        CriticalNotificationOutcome.NOT_APPLICABLE
    )
    critical_notification_error_type: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, SessionEndReason):
            raise TypeError("reason must be SessionEndReason")
        if not isinstance(self.critical_notification_outcome, CriticalNotificationOutcome):
            raise TypeError(
                "critical_notification_outcome must be CriticalNotificationOutcome"
            )
        if not isinstance(self.unresolved_position_codes, tuple):
            raise TypeError("unresolved_position_codes must be a tuple")
        if any(not isinstance(code, str) or not code for code in self.unresolved_position_codes):
            raise ValueError("unresolved position codes must be non-empty strings")
        if len(set(self.unresolved_position_codes)) != len(self.unresolved_position_codes):
            raise ValueError("unresolved position codes must be unique")
        if self.unresolved_position_codes != tuple(sorted(self.unresolved_position_codes)):
            raise ValueError("unresolved position codes must be sorted")

        if self.reason is SessionEndReason.KILL_SWITCH:
            self._validate_kill_switch_result()
        else:
            self._validate_normal_result()

    def _validate_kill_switch_result(self) -> None:
        if self.total_pnl is None or self.loss_limit is None:
            raise ValueError("kill-switch result requires total PnL and loss limit")
        for name, value in (
            ("total_pnl", self.total_pnl),
            ("loss_limit", self.loss_limit),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a finite number")
            if value != value or abs(value) == float("inf"):
                raise ValueError(f"{name} must be a finite number")
        if not self.total_pnl <= self.loss_limit:
            raise ValueError("kill-switch total PnL must be at or below the loss limit")
        if self.critical_notification_outcome not in (
            CriticalNotificationOutcome.CALL_RETURNED,
            CriticalNotificationOutcome.CALL_RAISED,
        ):
            raise ValueError("kill-switch result requires a critical notification attempt")
        if self.critical_notification_outcome is CriticalNotificationOutcome.CALL_RAISED:
            if (
                not isinstance(self.critical_notification_error_type, str)
                or not self.critical_notification_error_type
            ):
                raise ValueError("raised critical notification requires an error type")
        elif self.critical_notification_error_type is not None:
            raise ValueError("returned critical notification cannot have an error type")

    def _validate_normal_result(self) -> None:
        if self.total_pnl is not None or self.loss_limit is not None:
            raise ValueError("normal session result cannot contain kill-switch PnL metadata")
        if self.unresolved_position_codes:
            raise ValueError("normal session result cannot contain unresolved positions")
        if (
            self.critical_notification_outcome
            is not CriticalNotificationOutcome.NOT_APPLICABLE
        ):
            raise ValueError("normal session result cannot contain a notification attempt")
        if self.critical_notification_error_type is not None:
            raise ValueError("normal session result cannot contain a notification error")

    @property
    def requires_attention(self) -> bool:
        return self.reason is SessionEndReason.KILL_SWITCH

    @property
    def post_market_allowed(self) -> bool:
        return self.reason is not SessionEndReason.KILL_SWITCH

    @property
    def exit_code(self) -> int:
        return 1 if self.reason is SessionEndReason.KILL_SWITCH else 0
