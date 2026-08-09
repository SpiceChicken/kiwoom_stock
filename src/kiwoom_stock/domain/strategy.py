"""Pure target/stop policy and position-return semantics."""

import math
from dataclasses import dataclass
from numbers import Real


TARGET_STOP_UNIT_VERSION = "percentage-points-v1"


class StrategySemanticsValidationError(ValueError):
    """A target/stop policy or price violates the domain contract."""


def _positive_finite_real(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
        or float(value) <= 0.0
    ):
        raise StrategySemanticsValidationError(
            f"{name} must be a finite number greater than zero"
        )
    return float(value)


@dataclass(frozen=True)
class TargetStopPolicy:
    """Versioned positive percentage-point magnitudes for fixed exits."""

    unit_version: str = TARGET_STOP_UNIT_VERSION
    target_profit_percentage_points: float = 3.0
    stop_loss_percentage_points: float = 3.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.unit_version, str)
            or self.unit_version != TARGET_STOP_UNIT_VERSION
        ):
            raise StrategySemanticsValidationError(
                "target/stop unit version must be percentage-points-v1"
            )
        object.__setattr__(
            self,
            "target_profit_percentage_points",
            _positive_finite_real(
                self.target_profit_percentage_points,
                "target_profit_percentage_points",
            ),
        )
        object.__setattr__(
            self,
            "stop_loss_percentage_points",
            _positive_finite_real(
                self.stop_loss_percentage_points,
                "stop_loss_percentage_points",
            ),
        )


def calculate_position_return_percentage_points(
    buy_price: object,
    current_price: object,
) -> float:
    """Return the unrounded per-position percentage-point price change."""

    buy = _positive_finite_real(buy_price, "buy_price")
    current = _positive_finite_real(current_price, "current_price")
    return (current - buy) * 100.0 / buy
