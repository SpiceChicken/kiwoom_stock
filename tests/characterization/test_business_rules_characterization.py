"""Characterize stable calculation and decision boundaries before refactoring."""

from datetime import datetime, timedelta
import math
import unittest
from unittest.mock import patch

from kiwoom_stock.core import indicators
from kiwoom_stock.core.physics_engine import calculate_net_velocity
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.state import (
    PhysicalStateBatchCommitReceipt,
    PhysicalStateCommitReceipt,
    PhysicalStateHydrationSource,
    PhysicalStateLoadResult,
    PhysicalTrackerState,
    PhysicalStateWrite,
)
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.monitoring.strategy import TradingStrategy


def _frozen_datetime(value: datetime):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return value
            return value.replace(tzinfo=tz)

        @classmethod
        def today(cls):
            return value

    return FrozenDateTime


class _NoDatabase:
    def __init__(self):
        self.submissions = []

    def load_physical_state(self, stock_code):
        return PhysicalStateLoadResult(PhysicalStateHydrationSource.INITIAL, None)

    def persist_physical_state(self, state, forces):
        return self.persist_physical_state_batch(
            (PhysicalStateWrite(state, tuple(dict(forces).items())),)
        ).items[0]

    def persist_physical_state_batch(self, writes):
        writes = tuple(writes)
        receipts = tuple(
            PhysicalStateCommitReceipt(
                write.state.stock_code,
                write.state.last_observed_at.isoformat(),
                write.state.updated_at,
            )
            for write in writes
        )
        self.submissions.extend(
            (write.state.stock_code, dict(write.forces)) for write in writes
        )
        return PhysicalStateBatchCommitReceipt(
            receipts[0].generation, receipts, receipts[0].committed_at
        )

    def close(self):
        pass


def _physics_params(**overrides):
    params = {
        "strength": 100.0,
        "current_price": 100.0,
        "previous_price": 100.0,
        "vwap": 100.0,
        "atr_percent": 1.0,
        "previous_velocity": 0.0,
        "vol_ratio": 1.0,
        "rsi": 50.0,
        "tot_sel_req": 100.0,
        "tot_buy_req": 100.0,
        "prev_strength_5m": 100.0,
        "interval_impulse": 0.0,
        "interval_amount_krw": 0.0,
        "reference_mass": 10_000_000.0,
    }
    params.update(overrides)
    return params


def _entry_metrics(**force_overrides):
    forces = {
        "thrust": 1.0,
        "gravity": -0.5,
        "net_force": 1.0,
        "volume_drop_ratio": 1.0,
        "jerk": 0.1,
        "current_velocity": 0.1,
        "impulse": 0.0,
        "magnetic": 0.0,
    }
    forces.update(force_overrides)
    metrics = SupplyData(
        stock_code="005930",
        cur_prc=50_000.0,
        atr_percent=2.0,
        down_atr_percent=0.5,
    )
    metrics.forces = forces
    return metrics


def _position(**overrides):
    values = {
        "id": 1,
        "stock_code": "005930",
        "stock_name": "Samsung",
        "buy_price": 10_000.0,
        "buy_time": "2026-07-17 10:00:00",
        "buy_regime": "STABLE_BULL",
        "status": "OPEN",
        "atr_percent": 1.0,
        "down_atr_percent": 0.5,
    }
    values.update(overrides)
    return Position(**values)


class IndicatorCharacterizationTests(unittest.TestCase):
    def test_scalar_zero_and_empty_input_contracts(self):
        cases = [
            ("roc previous zero", indicators.calculate_roc, (10.0, 0.0), 0.0),
            ("disparity base zero", indicators.calculate_disparity, (10.0, 0.0), 0.0),
            ("slope previous zero", indicators.calculate_slope, (10.0, 0.0), 0.0),
            ("volume no history", indicators.calculate_volume_ratio, (10.0, []), 1.0),
            ("volume zero history", indicators.calculate_volume_ratio, (10.0, [0.0]), 1.0),
            ("volatility nonpositive ATR", indicators.calculate_volatility_ratio, (3.0, 0.0), 0.0),
        ]

        for label, function, args, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(function(*args), expected)

    def test_insufficient_history_defaults_are_stable(self):
        self.assertEqual(indicators.calculate_sma([1.0, 2.0], period=3), 0.0)
        self.assertEqual(indicators.calculate_ema([1.0, 2.0], period=3), 2.0)
        self.assertEqual(indicators.calculate_ema([], period=3), 0.0)
        self.assertEqual(indicators.calculate_rsi([1.0, 2.0], period=3), 50.0)
        self.assertEqual(
            indicators.calculate_bollinger_bands([1.0, 2.0], period=3),
            {"upper": 0.0, "mid": 0.0, "lower": 0.0},
        )
        self.assertEqual(
            indicators.calculate_atr([2.0, 3.0], [0.0, 1.0], [1.0, 2.0], period=2),
            0.0,
        )
        self.assertEqual(
            indicators.calculate_atr_percent([2.0], [0.0], [1.0], current_price=0.0),
            0.0,
        )

    def test_indicator_golden_vectors_and_rounding(self):
        self.assertEqual(indicators.calculate_ema([1, 2, 3, 4, 5], period=3), 4.0)
        self.assertEqual(indicators.calculate_rsi([1, 2, 3, 4, 5, 6], period=3), 100.0)
        self.assertEqual(
            indicators.calculate_bollinger_bands([1, 2, 3, 4, 5], period=5),
            {"upper": 6.16, "mid": 3.0, "lower": -0.16},
        )
        self.assertEqual(
            indicators.calculate_atr([11, 12, 13, 14], [9, 10, 11, 12], [10, 11, 12, 13], period=3),
            2.0,
        )
        self.assertEqual(
            indicators.calculate_atr_percent(
                [11, 12, 13, 14],
                [9, 10, 11, 12],
                [10, 11, 12, 13],
                current_price=13,
                period=3,
            ),
            15.38,
        )


class PhysicsCharacterizationTests(unittest.TestCase):
    def test_force_composition_golden_vector_is_rounded_to_four_places(self):
        result = calculate_net_velocity(
            **_physics_params(
                strength=125.0,
                current_price=101.0,
                vwap=100.0,
                atr_percent=2.0,
                previous_velocity=1.25,
                vol_ratio=10.0,
                rsi=80.0,
                tot_sel_req=300.0,
                tot_buy_req=100.0,
                prev_strength_5m=105.0,
                previous_price=100.0,
                interval_impulse=2.0,
                interval_amount_krw=20_000_000.0,
                reference_mass=100_000_000.0,
            )
        )

        self.assertEqual(
            result,
            {
                "thrust": 0.9242,
                "gravity": -0.4621,
                "drag": -0.1389,
                "magnetic": 0.2311,
                "jerk": 0.7616,
                "impulse": 0.9869,
                "net_force": 1.3159,
                "current_velocity": 3.5528,
            },
        )

    def test_mass_cutoffs_are_inclusive_at_ten_and_five_percent(self):
        thrust_at_cutoff = calculate_net_velocity(
            **_physics_params(
                strength=120.0,
                interval_amount_krw=10_000_000.0,
                reference_mass=100_000_000.0,
            )
        )
        thrust_with_full_mass = calculate_net_velocity(
            **_physics_params(
                strength=120.0,
                interval_amount_krw=100_000_000.0,
                reference_mass=100_000_000.0,
            )
        )
        jerk_at_cutoff = calculate_net_velocity(
            **_physics_params(
                strength=120.0,
                prev_strength_5m=100.0,
                interval_amount_krw=5_000_000.0,
                reference_mass=100_000_000.0,
            )
        )

        self.assertGreater(thrust_at_cutoff["thrust"], 0.0)
        self.assertEqual(thrust_at_cutoff["thrust"], thrust_with_full_mass["thrust"])
        self.assertGreater(jerk_at_cutoff["jerk"], 0.0)


class PhysicalStateCharacterizationTests(unittest.TestCase):
    def test_initial_velocity_uses_positive_rsi_excess(self):
        repository = _NoDatabase()
        captured = {}

        def capture_velocity(**kwargs):
            captured.update(kwargs)
            return {
                "thrust": 0.0,
                "gravity": 0.0,
                "drag": 0.0,
                "magnetic": 0.0,
                "jerk": 0.0,
                "impulse": 0.0,
                "net_force": 0.0,
                "current_velocity": kwargs["previous_velocity"],
            }

        tracker = PhysicalStateTracker(repository)
        with patch("kiwoom_stock.core.state_manager.calculate_net_velocity", side_effect=capture_velocity):
            tracker.process_tick(
                stock_code="005930",
                strength=100.0,
                current_price=50_000.0,
                vwap=50_000.0,
                atr_percent=1.0,
                vol_ratio=1.0,
                rsi=65.0,
                tot_sel_req=100.0,
                tot_buy_req=100.0,
                total_volume=100.0,
            )

        self.assertEqual(captured["previous_velocity"], 1.5)

    def test_frozen_volume_persists_canonical_state_transition(self):
        repository = _NoDatabase()
        tracker = PhysicalStateTracker(repository)

        arguments = {
            "stock_code": "005930",
            "strength": 110.0,
            "current_price": 50_000.0,
            "vwap": 50_000.0,
            "atr_percent": 1.0,
            "vol_ratio": 1.0,
            "rsi": 55.0,
            "tot_sel_req": 100.0,
            "tot_buy_req": 100.0,
            "total_volume": 100.0,
        }
        tracker.process_tick(**arguments)
        tracker.process_tick(**arguments)

        self.assertEqual(len(repository.submissions), 2)

    def test_strength_baseline_is_external_and_private_history_is_removed(self):
        tracker = PhysicalStateTracker(_NoDatabase())
        self.assertFalse(hasattr(tracker, "_strength_history"))
        self.assertFalse(hasattr(tracker, "_get_and_update_prev_strength"))

    def test_reference_mass_boundary_and_log_scale_are_forwarded_to_physics(self):
        captured_reference_masses = []

        def capture_reference_mass(**kwargs):
            captured_reference_masses.append(kwargs["reference_mass"])
            return {
                "thrust": 0.0,
                "gravity": 0.0,
                "drag": 0.0,
                "magnetic": 0.0,
                "jerk": 0.0,
                "impulse": 0.0,
                "net_force": 0.0,
                "current_velocity": kwargs["previous_velocity"],
            }

        market_caps = [99_999_999_999.0, 100_000_000_000.0, 350_000_000_000.0]
        for index, market_cap in enumerate(market_caps):
            tracker = PhysicalStateTracker(_NoDatabase())
            with patch(
                "kiwoom_stock.core.state_manager.calculate_net_velocity",
                side_effect=capture_reference_mass,
            ):
                tracker.process_tick(
                    stock_code=f"CODE-{index}",
                    strength=100.0,
                    current_price=50_000.0,
                    vwap=50_000.0,
                    atr_percent=1.0,
                    vol_ratio=1.0,
                    rsi=50.0,
                    tot_sel_req=100.0,
                    tot_buy_req=100.0,
                    total_volume=100.0,
                    market_cap=market_cap,
                )

        self.assertEqual(captured_reference_masses[0], 10_000_000.0)
        self.assertEqual(captured_reference_masses[1], 10_000_000.0)
        self.assertAlmostEqual(
            captured_reference_masses[2],
            10_000_000.0 * (3.5 ** (math.log10(350_000_000_000.0) - 11.0)),
        )

    def test_frozen_tick_passes_zero_strength_volume_ratio_and_impulse_to_physics(self):
        repository = _NoDatabase()
        captured_calls = []

        def capture_frozen_inputs(**kwargs):
            captured_calls.append(kwargs)
            return {
                "thrust": 0.0,
                "gravity": 0.0,
                "drag": 0.0,
                "magnetic": 0.0,
                "jerk": 0.0,
                "impulse": 0.0,
                "net_force": 0.0,
                "current_velocity": kwargs["previous_velocity"],
            }

        tracker = PhysicalStateTracker(repository)
        tick = {
            "stock_code": "005930",
            "strength": 120.0,
            "current_price": 50_000.0,
            "vwap": 50_000.0,
            "atr_percent": 1.0,
            "vol_ratio": 2.5,
            "rsi": 50.0,
            "tot_sel_req": 100.0,
            "tot_buy_req": 100.0,
            "total_volume": 100.0,
        }
        with patch(
            "kiwoom_stock.core.state_manager.calculate_net_velocity",
            side_effect=capture_frozen_inputs,
        ):
            tracker.process_tick(**tick)
            result = tracker.process_tick(**tick)

        frozen_call = captured_calls[1]
        self.assertEqual(frozen_call["strength"], 0.0)
        self.assertEqual(frozen_call["vol_ratio"], 0.0)
        self.assertEqual(frozen_call["interval_impulse"], 0.0)
        self.assertEqual(frozen_call["interval_amount_krw"], 0.0)
        self.assertEqual(result["forces"]["impulse"], 0.0)
        self.assertEqual(len(repository.submissions), 2)

    def test_complete_v1_recovery_decays_while_legacy_is_separate(self):
        from datetime import timezone

        now = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)

        class RecoveryDatabase(_NoDatabase):
            def load_physical_state(self, stock_code):
                self.asserted_code = stock_code
                persisted_at = now - timedelta(hours=2)
                return PhysicalStateLoadResult(
                    PhysicalStateHydrationSource.PERSISTED,
                    PhysicalTrackerState(
                        1, stock_code, 10.0, 100.0, 70_000.0, (),
                        persisted_at, persisted_at,
                    ),
                )

        database = RecoveryDatabase()
        tracker = PhysicalStateTracker(database, clock=lambda: now)
        tracker.recover_state_from_crash("005930", decay_constant=0.5)

        self.assertEqual(database.asserted_code, "005930")
        self.assertAlmostEqual(tracker._l1_cache["005930"], 10.0 * math.exp(-1.0))


class EntryPriorityCharacterizationTests(unittest.TestCase):
    def setUp(self):
        self.strategy = TradingStrategy({"debug_mode": True})

    def test_exact_hard_lock_boundaries_remain_permitted(self):
        cases = [
            ("thrust lower bound", {"thrust": 0.8}, {}),
            ("zero net force", {"net_force": 0.0}, {}),
            ("stall thrust boundary", {"gravity": -0.9, "thrust": 1.0}, {}),
            ("volume exhaustion boundary", {"volume_drop_ratio": 0.5}, {}),
            ("ATR quality boundary", {}, {"atr_percent": 1.25, "down_atr_percent": 0.5}),
        ]

        for label, force_overrides, metric_overrides in cases:
            with self.subTest(label=label):
                metrics = _entry_metrics(**force_overrides)
                for name, value in metric_overrides.items():
                    setattr(metrics, name, value)
                result = self.strategy.evaluate(metrics)
                self.assertTrue(result["is_buy_signal"], result["status"])

    def test_breakout_thresholds_are_inclusive_and_override_later_locks(self):
        metrics = _entry_metrics(
            thrust=1.0,
            impulse=3.0,
            jerk=0.5,
            net_force=-10.0,
            volume_drop_ratio=0.1,
        )
        metrics.atr_percent = 1.0
        metrics.down_atr_percent = 0.9

        result = self.strategy.evaluate(metrics)

        self.assertTrue(result["is_buy_signal"])
        self.assertIn("Breakout Override", result["status"])

    def test_climax_shield_has_priority_over_breakout_override(self):
        result = self.strategy.evaluate(
            _entry_metrics(
                thrust=1.5,
                gravity=-0.9,
                impulse=3.0,
                jerk=0.5,
            )
        )

        self.assertFalse(result["is_buy_signal"])
        self.assertIn("Climax Shield", result["status"])

    def test_nonpositive_price_precedes_all_force_signals(self):
        metrics = _entry_metrics(thrust=5.0, impulse=5.0, jerk=5.0)
        metrics.cur_prc = -1.0

        result = self.strategy.evaluate(metrics)

        self.assertFalse(result["is_buy_signal"])
        self.assertEqual(result["price"], -1.0)
        self.assertIn("0원 호가 무시", result["status"])


class ExitPriorityCharacterizationTests(unittest.TestCase):
    def test_time_windows_accept_explicit_now_without_datetime_patch(self):
        strategy = TradingStrategy(
            {
                "debug_mode": False,
                "day_trade_exit_time": "15:30",
                "entry_deadline": "15:00",
            }
        )

        friday_open = datetime(2026, 7, 17, 9, 0, 0)
        friday_deadline = datetime(2026, 7, 17, 15, 0, 0)
        friday_exit = datetime(2026, 7, 17, 15, 30, 0)
        saturday_open = datetime(2026, 7, 18, 10, 0, 0)

        self.assertTrue(strategy.is_monitoring_time(now=friday_open))
        self.assertTrue(strategy.is_monitoring_time(now=friday_exit))
        self.assertFalse(strategy.is_monitoring_time(now=saturday_open))
        self.assertTrue(strategy.is_trading_window(now=friday_open))
        self.assertFalse(strategy.is_trading_window(now=friday_deadline))

    def test_clock_injection_controls_exit_boundary(self):
        boundary = datetime(2026, 7, 17, 15, 27, 0)
        strategy = TradingStrategy(
            {"debug_mode": False, "day_trade_exit_time": "15:30"},
            clock=lambda: boundary,
        )

        self.assertEqual(
            strategy.get_exit_reason(_position(), 10_000.0, {"jerk": 0.0, "thrust": 1.0}),
            "Day Trade Close",
        )

    def test_forced_exit_boundary_is_inclusive(self):
        just_before = datetime(2026, 7, 17, 15, 26, 59)
        at_boundary = datetime(2026, 7, 17, 15, 27, 0)
        neutral_forces = {"jerk": 0.0, "thrust": 1.0}

        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(just_before)):
            strategy = TradingStrategy({"debug_mode": False, "day_trade_exit_time": "15:30"})
            self.assertIsNone(strategy.get_exit_reason(_position(), 10_000.0, neutral_forces))

        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(at_boundary)):
            strategy = TradingStrategy({"debug_mode": False, "day_trade_exit_time": "15:30"})
            self.assertEqual(
                strategy.get_exit_reason(_position(), 10_000.0, neutral_forces),
                "Day Trade Close",
            )

    def test_overnight_transition_precedes_forced_day_close(self):
        at_forced_exit = datetime(2026, 7, 17, 15, 27, 0)
        position = _position()
        forces = {"current_velocity": 3.0, "thrust": 2.0001, "magnetic": 0.1, "jerk": 0.0}

        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(at_forced_exit)):
            strategy = TradingStrategy({"debug_mode": False, "day_trade_exit_time": "15:30"})
            decision = strategy.decide_position(position, 10_000.0, forces)

        self.assertEqual(decision.decision.value, "MARK_OVERNIGHT")
        self.assertEqual(position.status, "OPEN")

    def test_fixed_target_precedes_late_overnight_candidate(self):
        at_forced_exit = datetime(2026, 7, 17, 15, 27, 0)
        position = _position()
        forces = {"current_velocity": 3.0, "thrust": 2.0001, "magnetic": 0.1}
        strategy = TradingStrategy({"debug_mode": False, "day_trade_exit_time": "15:30"})

        reason = strategy.get_exit_reason(
            position,
            10_300.0,
            forces,
            now=at_forced_exit,
        )

        self.assertIn("Fixed Target", reason)
        self.assertEqual(position.status, "OPEN")

    def test_existing_overnight_status_blocks_forced_exit_and_bailout(self):
        late = datetime(2026, 7, 17, 15, 30, 0)
        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(late)):
            strategy = TradingStrategy({"debug_mode": False, "day_trade_exit_time": "15:30"})
            reason = strategy.get_exit_reason(
                _position(status="OVERNIGHT"),
                9_000.0,
                {"jerk": -5.0, "thrust": 0.0},
            )

        self.assertIsNone(reason)

    def test_bailout_thresholds_are_inclusive_but_thrust_one_is_not_negative_jerk_bailout(self):
        morning = datetime(2026, 7, 17, 10, 0, 0)
        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(morning)):
            strategy = TradingStrategy({"debug_mode": True})
            flash = strategy.get_exit_reason(
                _position(),
                9_850.0,
                {"jerk": -1.0, "thrust": 1.0},
            )
            negative_jerk = strategy.get_exit_reason(
                _position(),
                9_900.0,
                {"jerk": -0.5, "thrust": 0.9999},
            )
            thrust_boundary = strategy.get_exit_reason(
                _position(),
                9_900.0,
                {"jerk": -0.5, "thrust": 1.0},
            )

        self.assertIn("Flash Crash Detected", flash)
        self.assertIn("Negative Jerk", negative_jerk)
        self.assertIsNone(thrust_boundary)

    def test_high_altitude_threshold_and_one_up_atr_sniper_are_inclusive(self):
        morning = datetime(2026, 7, 17, 10, 0, 0)
        position = _position(atr_percent=1.0, down_atr_percent=0.5)
        with patch("kiwoom_stock.monitoring.strategy.datetime", _frozen_datetime(morning)):
            strategy = TradingStrategy({"debug_mode": True})
            strategy._kinetic_state[position.stock_code] = {
                "buy_price": 10_000.0,
                "max_price": 10_200.0,
            }
            reason = strategy.get_exit_reason(
                position,
                10_149.0,
                {"jerk": -0.0001, "thrust": 1.0},
            )

        self.assertIn("Sniper Exit", reason)

    def test_kill_switch_activates_at_exact_configured_loss(self):
        strategy = TradingStrategy(
            {
                "debug_mode": True,
                "cumulative_trade_return_score_floor": -5.0,
            }
        )

        self.assertFalse(strategy.is_kill_switch_activated(-4.9999))
        self.assertTrue(strategy.is_kill_switch_activated(-5.0))


if __name__ == "__main__":
    unittest.main()
