"""Pure state-transition calculations for physical stock monitoring."""

from dataclasses import dataclass
from datetime import datetime
import math
from typing import Sequence, Tuple


MIN_REFERENCE_MASS = 10_000_000.0
MARKET_CAP_LOG_THRESHOLD = 100_000_000_000.0
MARKET_CAP_LOG_ANCHOR = 100_000_000_000.0
MARKET_CAP_SCALE_BASE = 3.5
VOLUME_HISTORY_LIMIT = 120
VOLUME_WINDOW_SIZE = 60


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
        return VolumeInterval(interval_volume=total_volume - last_volume, is_frozen=False)

    return VolumeInterval(interval_volume=0.0, is_frozen=False)


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


def decay_velocity(velocity: float, elapsed_hours: float, decay_constant: float = 0.5) -> float:
    """Apply exponential time decay to a stored velocity."""

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
