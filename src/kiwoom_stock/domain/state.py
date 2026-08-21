"""Pure state-transition calculations for physical stock monitoring."""

from dataclasses import dataclass
from datetime import datetime, time as wall_time
import math
from enum import Enum
from numbers import Real
from typing import Optional, Sequence, Tuple
from zoneinfo import ZoneInfo


MIN_REFERENCE_MASS = 10_000_000.0
MARKET_CAP_LOG_THRESHOLD = 100_000_000_000.0
MARKET_CAP_LOG_ANCHOR = 100_000_000_000.0
MARKET_CAP_SCALE_BASE = 3.5
VOLUME_HISTORY_LIMIT = 120
VOLUME_WINDOW_SIZE = 60
PHYSICAL_TRACKER_SCHEMA_VERSION = 1
_KST = ZoneInfo("Asia/Seoul")
_KRX_SESSION_OPEN = wall_time(9, 0)
_PERSISTED_FORCE_KEYS = frozenset(
    {
        "current_velocity",
        "thrust",
        "gravity",
        "drag",
        "magnetic",
        "jerk",
        "impulse",
        "net_force",
    }
)


class PhysicalStateValidationError(ValueError):
    """Persisted or incoming physical state is unsafe to consume."""


class PhysicalStateHydrationSource(Enum):
    INITIAL = "initial"
    LEGACY_COLD_START = "legacy_cold_start"
    PERSISTED = "persisted"


def _finite_real(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PhysicalStateValidationError(f"{name} must be a finite real number")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise PhysicalStateValidationError(f"{name} must be a finite real number")
    return numeric


@dataclass(frozen=True)
class PhysicalStateCommitReceipt:
    """Durable acknowledgement returned only after SQLite commit succeeds."""

    stock_code: str
    generation: str
    committed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise PhysicalStateValidationError("commit receipt stock_code is required")
        if not isinstance(self.generation, str) or not self.generation:
            raise PhysicalStateValidationError("commit receipt generation is required")
        if (
            not isinstance(self.committed_at, datetime)
            or self.committed_at.tzinfo is None
            or self.committed_at.utcoffset() is None
        ):
            raise PhysicalStateValidationError("commit receipt committed_at must be aware")


@dataclass(frozen=True)
class PhysicalTrackerState:
    """Versioned immutable state that is carried across monitoring cycles."""

    schema_version: int
    stock_code: str
    velocity: float
    last_cumulative_volume: Optional[float]
    last_price: Optional[float]
    interval_volume_history: Tuple[float, ...]
    last_observed_at: Optional[datetime]
    updated_at: datetime

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != PHYSICAL_TRACKER_SCHEMA_VERSION
        ):
            raise PhysicalStateValidationError(
                f"unsupported physical tracker schema: {self.schema_version}"
            )
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise PhysicalStateValidationError("physical tracker stock_code is required")
        if (
            not isinstance(self.updated_at, datetime)
            or self.updated_at.tzinfo is None
            or self.updated_at.utcoffset() is None
        ):
            raise PhysicalStateValidationError("physical tracker updated_at must be aware")
        _finite_real(self.velocity, "physical tracker velocity")
        for name in ("last_cumulative_volume", "last_price"):
            value = getattr(self, name)
            if value is not None:
                _finite_real(value, f"physical tracker {name}")
        if self.last_cumulative_volume is not None and self.last_cumulative_volume < 0.0:
            raise PhysicalStateValidationError("physical tracker cumulative volume cannot be negative")
        if self.last_price is not None and self.last_price <= 0.0:
            raise PhysicalStateValidationError("physical tracker last price must be positive")
        if not isinstance(self.interval_volume_history, tuple):
            raise PhysicalStateValidationError("physical tracker volume history must be a tuple")
        if len(self.interval_volume_history) > VOLUME_HISTORY_LIMIT:
            raise PhysicalStateValidationError("physical tracker volume history is unbounded")
        for value in self.interval_volume_history:
            _finite_real(value, "physical tracker volume history value")
            if value <= 0.0:
                raise PhysicalStateValidationError(
                    "physical tracker volume history must contain positive values"
                )
        if self.last_observed_at is not None:
            if (
                not isinstance(self.last_observed_at, datetime)
                or self.last_observed_at.tzinfo is None
                or self.last_observed_at.utcoffset() is None
            ):
                raise PhysicalStateValidationError(
                    "physical tracker last_observed_at must be aware"
                )
            if self.last_observed_at > self.updated_at:
                raise PhysicalStateValidationError(
                    "physical tracker observation cannot follow its update"
                )

    def assert_persistable(self) -> None:
        """Reject incomplete initial state at the durable repository boundary."""

        if (
            self.last_cumulative_volume is None
            or self.last_price is None
            or self.last_observed_at is None
        ):
            raise PhysicalStateValidationError("physical tracker snapshot is incomplete")

    @classmethod
    def initial(cls, stock_code: str, now: datetime) -> "PhysicalTrackerState":
        return cls(
            schema_version=PHYSICAL_TRACKER_SCHEMA_VERSION,
            stock_code=stock_code,
            velocity=0.0,
            last_cumulative_volume=None,
            last_price=None,
            interval_volume_history=(),
            last_observed_at=None,
            updated_at=now,
        )


@dataclass(frozen=True)
class PhysicalStateWrite:
    """Immutable complete state/force member of one SQLite transaction."""

    state: PhysicalTrackerState
    forces: Tuple[Tuple[str, float], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.state, PhysicalTrackerState):
            raise PhysicalStateValidationError(
                "physical state write requires PhysicalTrackerState"
            )
        self.state.assert_persistable()
        if not isinstance(self.forces, tuple):
            raise PhysicalStateValidationError(
                "physical state write forces must be a tuple"
            )
        keys = []
        normalized = []
        for entry in self.forces:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise PhysicalStateValidationError(
                    "physical state write force entry is invalid"
                )
            key, value = entry
            if not isinstance(key, str) or not key:
                raise PhysicalStateValidationError(
                    "physical state write force key is required"
                )
            keys.append(key)
            normalized.append((key, _finite_real(value, f"physical force {key}")))
        if len(keys) != len(set(keys)):
            raise PhysicalStateValidationError(
                "physical state write force keys must be unique"
            )
        if not _PERSISTED_FORCE_KEYS.issubset(keys):
            raise PhysicalStateValidationError(
                "physical state write force projection is incomplete"
            )
        object.__setattr__(self, "forces", tuple(normalized))


@dataclass(frozen=True)
class PhysicalStateBatchCommitReceipt:
    """Aggregate acknowledgement for one committed physical-state batch."""

    generation: str
    items: Tuple[PhysicalStateCommitReceipt, ...]
    committed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.generation, str) or not self.generation:
            raise PhysicalStateValidationError(
                "batch commit receipt generation is required"
            )
        if not isinstance(self.items, tuple) or not self.items:
            raise PhysicalStateValidationError(
                "batch commit receipt items must be a non-empty tuple"
            )
        if (
            not isinstance(self.committed_at, datetime)
            or self.committed_at.tzinfo is None
            or self.committed_at.utcoffset() is None
        ):
            raise PhysicalStateValidationError(
                "batch commit receipt committed_at must be aware"
            )
        stock_codes = []
        for item in self.items:
            if not isinstance(item, PhysicalStateCommitReceipt):
                raise PhysicalStateValidationError(
                    "batch commit receipt contains an invalid item"
                )
            stock_codes.append(item.stock_code)
            if item.generation != self.generation:
                raise PhysicalStateValidationError(
                    "batch commit receipt contains mixed generations"
                )
            if item.committed_at != self.committed_at:
                raise PhysicalStateValidationError(
                    "batch commit receipt contains mixed commit timestamps"
                )
        if len(stock_codes) != len(set(stock_codes)):
            raise PhysicalStateValidationError(
                "batch commit receipt stock codes must be unique"
            )


@dataclass(frozen=True)
class PhysicalStateLoadResult:
    """Typed repository result, including explicit legacy cold starts."""

    source: PhysicalStateHydrationSource
    state: Optional[PhysicalTrackerState]

    def __post_init__(self) -> None:
        if self.source is PhysicalStateHydrationSource.PERSISTED and self.state is None:
            raise PhysicalStateValidationError("persisted hydration requires a state")
        if self.source is not PhysicalStateHydrationSource.PERSISTED and self.state is not None:
            raise PhysicalStateValidationError("cold-start hydration cannot mix persisted state")


@dataclass(frozen=True)
class VolumeInterval:
    """Derived volume interval and freeze state for one incoming tick."""

    interval_volume: float
    is_frozen: bool


@dataclass(frozen=True)
class IntervalImpulse:
    """Impulse inputs derived from interval volume and reference mass."""

    interval_amount_krw: float
    interval_impulse: float


@dataclass(frozen=True)
class VolumeWindow:
    """Updated tick-volume history and current/past drop ratio."""

    history: Tuple[float, ...]
    drop_ratio: float


def calculate_initial_velocity_from_rsi(rsi: float) -> float:
    """Seed first-tick inertia from positive RSI excess."""

    return max(0.0, (rsi - 50.0) / 10.0)


def calculate_volume_interval(last_volume: float, total_volume: float) -> VolumeInterval:
    """Calculate interval volume while preserving frozen-feed semantics."""

    if last_volume == total_volume and total_volume >= 0.0:
        return VolumeInterval(interval_volume=0.0, is_frozen=True)

    if last_volume >= 0.0:
        if total_volume < last_volume:
            raise PhysicalStateValidationError("cumulative volume regressed")
        return VolumeInterval(interval_volume=total_volume - last_volume, is_frozen=False)

    return VolumeInterval(interval_volume=0.0, is_frozen=False)


def is_new_volume_session(
    previous_observed_at: Optional[datetime],
    observed_at: datetime,
) -> bool:
    """Identify the KRX regular-session boundary for daily volume resets.

    Kiwoom's ``trde_qty`` is a session cumulative volume.  A pre-open
    observation can still expose the previous session's total, so comparing
    calendar dates alone is insufficient.  A decrease is therefore expected
    only when the current observation is at/after the KRX open and the prior
    observation was before that same day's open.
    """

    if previous_observed_at is None:
        return False
    current = observed_at.astimezone(_KST)
    previous = previous_observed_at.astimezone(_KST)
    current_open = datetime.combine(
        current.date(), _KRX_SESSION_OPEN, tzinfo=_KST
    )
    return current >= current_open and previous < current_open


def calculate_reference_mass(market_cap: float) -> float:
    """Calculate the market-cap-adjusted reference mass used by physics forces."""

    if market_cap < MARKET_CAP_LOG_THRESHOLD:
        return MIN_REFERENCE_MASS

    log_scale = math.log10(market_cap) - math.log10(MARKET_CAP_LOG_ANCHOR)
    return MIN_REFERENCE_MASS * math.pow(MARKET_CAP_SCALE_BASE, log_scale)


def calculate_interval_impulse(
    interval_volume: float,
    current_price: float,
    reference_mass: float,
    is_frozen: bool,
) -> IntervalImpulse:
    """Calculate interval amount and impulse for a non-frozen positive-volume tick."""

    if is_frozen or interval_volume <= 0.0:
        return IntervalImpulse(interval_amount_krw=0.0, interval_impulse=0.0)

    interval_amount_krw = interval_volume * current_price
    if interval_amount_krw >= reference_mass:
        return IntervalImpulse(
            interval_amount_krw=interval_amount_krw,
            interval_impulse=interval_amount_krw / reference_mass,
        )

    return IntervalImpulse(interval_amount_krw=interval_amount_krw, interval_impulse=0.0)


def calculate_volume_window(
    history: Sequence[float],
    interval_volume: float,
    is_frozen: bool,
    *,
    max_ticks: int = VOLUME_HISTORY_LIMIT,
    window_size: int = VOLUME_WINDOW_SIZE,
) -> VolumeWindow:
    """Return updated interval-volume history and recent/past volume ratio."""

    updated_history = list(history)
    if not is_frozen and interval_volume > 0.0:
        updated_history.append(interval_volume)

    if len(updated_history) > max_ticks:
        updated_history.pop(0)

    current_window = updated_history[-window_size:]
    prev_window = updated_history[:-window_size]

    current_volume = sum(current_window)
    previous_volume = sum(prev_window)
    drop_ratio = (current_volume / previous_volume) if previous_volume > 0.0 else 1.0

    return VolumeWindow(history=tuple(updated_history), drop_ratio=drop_ratio)


def calculate_elapsed_hours(started_at: datetime, ended_at: datetime) -> float:
    """Calculate elapsed hours between two timestamps."""

    return (ended_at - started_at).total_seconds() / 3600.0


def validate_decay_constant(decay_constant: float) -> None:
    _finite_real(decay_constant, "decay_constant")
    if decay_constant < 0.0:
        raise PhysicalStateValidationError("decay_constant cannot be negative")


def decay_velocity(velocity: float, elapsed_hours: float, decay_constant: float = 0.5) -> float:
    """Apply exponential time decay to a stored velocity."""

    _finite_real(velocity, "velocity")
    _finite_real(elapsed_hours, "elapsed_hours")
    validate_decay_constant(decay_constant)
    if elapsed_hours < 0.0:
        raise PhysicalStateValidationError("elapsed_hours cannot be negative")
    return velocity * math.exp(-decay_constant * elapsed_hours)


def calculate_recovered_velocity(
    velocity: float,
    last_timestamp: datetime,
    now: datetime,
    decay_constant: float = 0.5,
) -> float:
    """Recover a stored velocity with elapsed-time decay."""

    return decay_velocity(
        velocity=velocity,
        elapsed_hours=calculate_elapsed_hours(last_timestamp, now),
        decay_constant=decay_constant,
    )
