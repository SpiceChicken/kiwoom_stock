"""Domain model surface for business data structures."""

from kiwoom_stock.domain.models import (
    ForeignData,
    MarketRegime,
    PgmData,
    Position,
    PositionDecision,
    PositionDecisionResult,
    PositionStatus,
    SupplyData,
)
from kiwoom_stock.domain.strategy import (
    TARGET_STOP_UNIT_VERSION,
    StrategySemanticsValidationError,
    TargetStopPolicy,
    calculate_position_return_percentage_points,
)

__all__ = [
    "ForeignData",
    "MarketRegime",
    "PgmData",
    "Position",
    "PositionDecision",
    "PositionDecisionResult",
    "PositionStatus",
    "SupplyData",
    "TARGET_STOP_UNIT_VERSION",
    "StrategySemanticsValidationError",
    "TargetStopPolicy",
    "calculate_position_return_percentage_points",
]
