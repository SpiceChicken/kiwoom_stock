"""Tests for pure physical state transition calculations."""

import ast
from datetime import datetime, timedelta
import math
from pathlib import Path
from zoneinfo import ZoneInfo

from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.state import (
    calculate_initial_velocity_from_rsi,
    calculate_interval_impulse,
    calculate_recovered_velocity,
    calculate_reference_mass,
    calculate_volume_interval,
    calculate_volume_window,
    decay_velocity,
    is_new_volume_session,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
)


class _RecoveryDatabase:
    def __init__(self, timestamp: datetime):
        self.timestamp = timestamp
        self.asserted_code = None

    def load_physical_state(self, stock_code):
        self.asserted_code = stock_code
        return PhysicalStateLoadResult(
            PhysicalStateHydrationSource.LEGACY_COLD_START, None
        )

    def persist_physical_state(self, state, forces):
        raise AssertionError("recovery tests must not submit physical state")

    def persist_physical_state_batch(self, writes):
        raise AssertionError("recovery tests must not submit physical state")

    def close(self):
        pass


def test_initial_velocity_reference_mass_and_recovery_decay_match_legacy_rules():
    assert calculate_initial_velocity_from_rsi(65.0) == 1.5
    assert calculate_initial_velocity_from_rsi(40.0) == 0.0

    assert calculate_reference_mass(99_999_999_999.0) == 10_000_000.0
    assert calculate_reference_mass(100_000_000_000.0) == 10_000_000.0
    assert calculate_reference_mass(350_000_000_000.0) == (
        10_000_000.0 * (3.5 ** (math.log10(350_000_000_000.0) - 11.0))
    )

    assert decay_velocity(10.0, elapsed_hours=2.0, decay_constant=0.5) == 10.0 * math.exp(-1.0)


def test_volume_interval_window_and_impulse_match_legacy_rules():
    first_interval = calculate_volume_interval(last_volume=-1.0, total_volume=100.0)
    assert first_interval.interval_volume == 0.0
    assert first_interval.is_frozen is False

    frozen_interval = calculate_volume_interval(last_volume=100.0, total_volume=100.0)
    assert frozen_interval.interval_volume == 0.0
    assert frozen_interval.is_frozen is True

    small_impulse = calculate_interval_impulse(
        interval_volume=10.0,
        current_price=50_000.0,
        reference_mass=10_000_000.0,
        is_frozen=False,
    )
    assert small_impulse.interval_amount_krw == 500_000.0
    assert small_impulse.interval_impulse == 0.0

    cutoff_impulse = calculate_interval_impulse(
        interval_volume=200.0,
        current_price=50_000.0,
        reference_mass=10_000_000.0,
        is_frozen=False,
    )
    assert cutoff_impulse.interval_amount_krw == 10_000_000.0
    assert cutoff_impulse.interval_impulse == 1.0


def test_volume_session_boundary_requires_regular_open_transition():
    pre_open = datetime(2026, 8, 21, 0, 44, tzinfo=ZoneInfo("Asia/Seoul"))
    same_session = datetime(2026, 8, 21, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    next_session = datetime(2026, 8, 24, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    assert is_new_volume_session(pre_open, same_session) is True
    assert is_new_volume_session(same_session, same_session) is False
    assert is_new_volume_session(same_session, next_session) is True


def test_volume_window_preserves_120_tick_limit_and_drop_ratio():
    history = ()
    for _ in range(150):
        history = calculate_volume_window(history, interval_volume=10.0, is_frozen=False).history

    assert len(history) == 120

    history = ()
    for _ in range(60):
        history = calculate_volume_window(history, interval_volume=100.0, is_frozen=False).history

    result = None
    for _ in range(60):
        result = calculate_volume_window(history, interval_volume=30.0, is_frozen=False)
        history = result.history

    assert result is not None
    assert 0.29 < result.drop_ratio < 0.31

    frozen = calculate_volume_window(history, interval_volume=999.0, is_frozen=True)
    assert frozen.history == history


def test_physical_state_tracker_accepts_injected_clock_for_crash_recovery():
    now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    database = _RecoveryDatabase(timestamp=now - timedelta(hours=2))
    tracker = PhysicalStateTracker(database, clock=lambda: now)

    tracker.recover_state_from_crash("005930", decay_constant=0.5)

    assert database.asserted_code == "005930"
    # Unversioned velocity-only rows are explicit legacy cold starts; velocity
    # is never mixed into a partial v1 state.
    assert tracker._l1_cache["005930"] == 0.0


def test_domain_state_does_not_import_external_io_or_call_system_time():
    source = Path("src/kiwoom_stock/domain/state.py")
    tree = ast.parse(source.read_text(encoding="utf-8"))

    forbidden_imports = {
        "requests",
        "boto3",
        "slack_sdk",
        "google",
        "sqlite3",
        "os",
        "pathlib",
        "time",
    }
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(forbidden_imports)

    forbidden_time_calls = {"now", "today", "utcnow"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden_time_calls
