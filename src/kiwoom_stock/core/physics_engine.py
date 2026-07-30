"""Legacy physics import path.

Pure physics implementations are owned by ``kiwoom_stock.domain.physics``.
This module intentionally re-exports the same functions during migration.
"""

from kiwoom_stock.domain.physics import (
    _calculate_drag_force,
    _calculate_gravity_force,
    _calculate_impulse,
    _calculate_jerk_force,
    _calculate_magnetic_force,
    _calculate_thrust_force,
    _rational_penalty,
    _sigmoid,
    calculate_net_velocity,
)

__all__ = [
    "_calculate_drag_force",
    "_calculate_gravity_force",
    "_calculate_impulse",
    "_calculate_jerk_force",
    "_calculate_magnetic_force",
    "_calculate_thrust_force",
    "_rational_penalty",
    "_sigmoid",
    "calculate_net_velocity",
]
