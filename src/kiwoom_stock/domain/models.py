"""Shared domain data models with legacy import compatibility.

These classes keep the current field names, defaults, and calculation behavior.
Legacy modules re-export them during the migration window.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import math
from numbers import Real
from typing import Dict, List, Optional

from kiwoom_stock.domain.strategy import (
    calculate_position_return_percentage_points,
)
from kiwoom_stock.domain.state import (
    PhysicalStateHydrationSource,
    PhysicalStateValidationError,
)


BASELINE_SOURCE_ROW_4 = "row_4_fixed_cadence"
BASELINE_SAMPLE_INDEX = 4


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PhysicalStateValidationError(f"{name} must be a finite real number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PhysicalStateValidationError(f"{name} must be a finite real number")
    return numeric


@dataclass(frozen=True)
class PhysicalContinuityEvidence:
    """Continuity facts safe to forward through shadow evidence."""

    schema_version: int
    hydration_source: str
    previous_observed_at: Optional[datetime]
    history_depth: int
    baseline_source: str = BASELINE_SOURCE_ROW_4
    baseline_sample_index: int = BASELINE_SAMPLE_INDEX
    baseline_time_estimated: bool = True

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise PhysicalStateValidationError(
                "continuity schema_version must be a positive int"
            )
        if self.hydration_source not in {
            source.value for source in PhysicalStateHydrationSource
        }:
            raise PhysicalStateValidationError(
                "continuity hydration_source is unsupported"
            )
        if self.previous_observed_at is not None and (
            not isinstance(self.previous_observed_at, datetime)
            or self.previous_observed_at.tzinfo is None
            or self.previous_observed_at.utcoffset() is None
        ):
            raise PhysicalStateValidationError(
                "continuity previous_observed_at must be aware"
            )
        if type(self.history_depth) is not int or self.history_depth < 0:
            raise PhysicalStateValidationError(
                "continuity history_depth must be a non-negative int"
            )
        if self.baseline_source != BASELINE_SOURCE_ROW_4:
            raise PhysicalStateValidationError("unsupported strength baseline source")
        if self.baseline_sample_index != BASELINE_SAMPLE_INDEX:
            raise PhysicalStateValidationError(
                "unsupported strength baseline sample index"
            )
        if self.baseline_time_estimated is not True:
            raise PhysicalStateValidationError(
                "fixed-cadence baseline time must be estimated"
            )

    def to_safe_dict(self) -> Dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hydration_source": self.hydration_source,
            "previous_observed_at": (
                self.previous_observed_at.isoformat()
                if self.previous_observed_at is not None
                else None
            ),
            "history_depth": self.history_depth,
            "baseline_source": self.baseline_source,
            "baseline_sample_index": self.baseline_sample_index,
            "baseline_time_estimated": self.baseline_time_estimated,
        }


@dataclass
class PgmData:
    """Program trading data."""

    netprps_prica: float = 0.0
    all_trde_rt: float = 0.0
    buy_cntr_amt: float = 0.0
    sel_cntr_amt: float = 0.0


@dataclass
class ForeignData:
    """Foreign broker flow data."""

    netprps_prica: float = 0.0
    trde_prica: float = 1.0


@dataclass
class SupplyData:
    """Per-symbol supply, price, and indicator snapshot."""

    stock_code: str = ""
    strength: float = 100.0
    prev_strength_5m: float = 100.0
    vol_ratio: float = 0.0
    price: float = 0.0
    vwap: float = 0.0
    prev_vwap: float = 0.0

    trend_rsi: float = 50.0
    vol_factor: float = 1.0
    atr_percent: float = 0.5
    down_atr_percent: float = 0.5

    ema5: float = 0.0
    ema20: float = 0.0
    ema60: float = 0.0
    prev_ema60: float = 0.0

    price_series: List[float] = field(default_factory=list)
    volume_series: List[float] = field(default_factory=list)
    chart_data: List[Dict] = field(default_factory=list)

    trde_qty: int = 0
    cur_prc: float = 0.0
    mac: float = 100000.0

    pgm_data: PgmData = field(default_factory=PgmData)
    foreign_data: ForeignData = field(default_factory=ForeignData)
    forces: Dict[str, float] = field(default_factory=dict)
    continuity: Optional[PhysicalContinuityEvidence] = None


@dataclass(frozen=True)
class PhysicalObservation:
    """Validated immutable input for one physical-state transition.

    The upstream rows do not contain per-row timestamps. Baseline semantics
    therefore identify the fixed-cadence row index and explicitly mark its
    event time as estimated instead of inventing a source timestamp.
    """

    stock_code: str
    observed_at: datetime
    current_price: float
    cumulative_volume: float
    strength: float
    prev_strength_5m: float
    vwap: float
    atr_percent: float
    vol_ratio: float
    rsi: float
    tot_sel_req: float
    tot_buy_req: float
    market_cap: float = 1_000_000_000_000.0
    baseline_source: str = BASELINE_SOURCE_ROW_4
    baseline_sample_index: int = BASELINE_SAMPLE_INDEX
    baseline_time_estimated: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise PhysicalStateValidationError(
                "physical observation stock_code is required"
            )
        if (
            not isinstance(self.observed_at, datetime)
            or self.observed_at.tzinfo is None
            or self.observed_at.utcoffset() is None
        ):
            raise PhysicalStateValidationError(
                "physical observation observed_at must be timezone-aware"
            )
        for name in (
            "current_price",
            "cumulative_volume",
            "strength",
            "prev_strength_5m",
            "vwap",
            "atr_percent",
            "vol_ratio",
            "rsi",
            "tot_sel_req",
            "tot_buy_req",
            "market_cap",
        ):
            _finite_real(getattr(self, name), f"physical observation {name}")
        if self.current_price <= 0.0:
            raise PhysicalStateValidationError(
                "physical observation current_price must be positive"
            )
        if self.cumulative_volume < 0.0:
            raise PhysicalStateValidationError(
                "physical observation cumulative_volume cannot be negative"
            )
        if self.market_cap <= 0.0:
            raise PhysicalStateValidationError(
                "physical observation market_cap must be positive"
            )
        if self.baseline_source != BASELINE_SOURCE_ROW_4:
            raise PhysicalStateValidationError("unsupported strength baseline source")
        if type(self.baseline_sample_index) is not int or self.baseline_sample_index != 4:
            raise PhysicalStateValidationError(
                "strength baseline sample index must be 4"
            )
        if self.baseline_time_estimated is not True:
            raise PhysicalStateValidationError(
                "strength baseline time must be estimated"
            )


class MarketRegime(Enum):
    STABLE_BULL = "안정적 강세장"
    VOLATILE_BULL = "변동성 강세장"
    QUIET_BEAR = "조용한 하락장"
    PANIC_BEAR = "패닉 하락장"
    NEUTRAL = "평온 구간"
    UNKNOWN = "Unknown"


class PositionStatus(str, Enum):
    """Durable paper-position lifecycle states."""

    OPEN = "OPEN"
    OVERNIGHT = "OVERNIGHT"
    CLOSED = "CLOSED"


class PositionDecision(str, Enum):
    """Typed strategy intent; persistence is owned by the manager/ledger."""

    HOLD = "HOLD"
    SELL = "SELL"
    MARK_OVERNIGHT = "MARK_OVERNIGHT"


@dataclass(frozen=True)
class PositionDecisionResult:
    decision: PositionDecision
    reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PositionDecision):
            raise TypeError("position decision must be PositionDecision")
        if self.decision is PositionDecision.SELL:
            if not isinstance(self.reason, str) or not self.reason:
                raise ValueError("SELL position decision requires a reason")
        elif self.reason is not None:
            raise ValueError("non-SELL position decision cannot contain a reason")


@dataclass
class Position:
    id: int
    stock_code: str
    stock_name: str
    buy_price: float
    buy_time: str
    buy_regime: str
    status: PositionStatus = PositionStatus.OPEN
    thrust: float = 0.0
    gravity: float = 0.0
    drag: float = 0.0
    magnetic: float = 0.0
    jerk: float = 0.0
    impulse: float = 0.0
    net_force: float = 0.0
    sell_price: Optional[float] = None
    # Per-trade return in percentage points; never a weighted portfolio metric.
    profit_rate: Optional[float] = None
    sell_time: Optional[str] = None
    sell_reason: Optional[str] = None
    atr_percent: float = 0.5
    down_atr_percent: float = 0.5
    owning_session_date: Optional[date] = None
    state_changed_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            try:
                self.status = PositionStatus(self.status)
            except ValueError as error:
                raise ValueError(f"unsupported position status: {self.status}") from error
        elif not isinstance(self.status, PositionStatus):
            raise TypeError("position status must be PositionStatus")

    @property
    def calc_profit_rate(self) -> float:
        """Return this trade's unrounded return in percentage points."""

        if self.sell_price is None:
            return 0.0
        return calculate_position_return_percentage_points(
            self.buy_price,
            self.sell_price,
        )
