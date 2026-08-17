"""Characterize legacy behavior relevant to the not-yet-active swing contract."""

from datetime import date, datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.domain.models import Position, PositionStatus
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.manager import StockManager
from kiwoom_stock.monitoring.strategy import TradingStrategy


KST = ZoneInfo("Asia/Seoul")

CANONICAL_SWING_FIELDS = (
    "strategy_version", "thesis_id", "signal_episode_id", "planned_horizon_sessions",
    "entry_signal_at", "armed_at", "disarmed_at", "rearmed_at", "side", "fill_at",
    "fill_session_date", "price", "quantity", "gross_notional", "commission", "tax",
    "slippage", "currency", "from_status", "to_status", "reason_code", "occurred_at", "event_session_date",
    "raw_price", "adjusted_price", "source_id", "available_at", "computed_at", "mark_session_date", "mark_price",
    "mark_quality", "revision", "supersedes_id", "max_price", "max_price_at", "exit_policy_version",
    "realized_pnl", "unrealized_pnl", "net_pnl", "return_denominator", "exit_reason", "censored",
)

HARD_RISK_ALLOWLIST = frozenset({"CATASTROPHIC_PRICE_RISK", "PORTFOLIO_RISK_LIMIT"})
MARK_PROVENANCE_REQUIRED_FIELDS = frozenset(
    {"source_id", "available_at", "computed_at", "revision", "supersedes_id"}
)
INCOMPLETE_MARK_QUALITIES = frozenset({"SUSPENDED_CARRY_FORWARD", "MISSING"})
UNKNOWN_CORPORATE_ACTION = "INSUFFICIENT_DATA"


def _hard_risk_is_executable(reason, raw_executable_price, versioned_threshold):
    return (
        reason in HARD_RISK_ALLOWLIST
        and raw_executable_price is not None
        and versioned_threshold is not None
    )


def _mark_has_provenance(mark):
    return all(mark.get(field) is not None for field in MARK_PROVENANCE_REQUIRED_FIELDS)


def _classify_corporate_action(action, decision_at):
    if action is None or action.get("available_at") > decision_at:
        return UNKNOWN_CORPORATE_ACTION
    return "KNOWN"


def _episode_can_rearm(*, flat, slow_false, completed_fast_false_count, cooldown_sessions):
    return flat and slow_false and completed_fast_false_count >= 2 and cooldown_sessions >= 1


def _fill_contract_is_valid(*, decision_at, fill_at, slow_context_completed, fast_bar_completed,
                            next_regular_bar_open, filled, cash_delta):
    if not (slow_context_completed and fast_bar_completed and fill_at == next_regular_bar_open):
        return False
    if decision_at > fill_at:
        return False
    return filled or cash_delta == 0


def _classify_legacy_row_for_candidate(row):
    """Test-only contract classifier; production dual-read is intentionally absent in P0."""
    required = set(CANONICAL_SWING_FIELDS)
    return "INSUFFICIENT_DATA" if any(row.get(field) is None for field in required) else "AVAILABLE"


def _manager(ledger, clock, *, current_session_resolver=None):
    options = {
        "clock": clock,
        "paper_transition_guard": MagicMock(),
        "strict_paper_errors": True,
    }
    if current_session_resolver is not None:
        options["current_session_resolver"] = current_session_resolver
    return StockManager(
        MagicMock(),
        ledger,
        {},
        **options,
    )


def _buy(manager, *, code="005930", price=10_000.0):
    manager.stock_names[code] = "Samsung"
    success, data = manager.apply_paper_buy(
        {
            "stock_code": code,
            "price": price,
            "regime": "STABLE_BULL",
            "forces": {},
        }
    )
    assert success is True
    assert data is not None
    return manager.active_positions[code]


def _position(*, code="005930", buy_price=100.0, sell_price=None):
    return Position(
        id=1,
        stock_code=code,
        stock_name=code,
        buy_price=buy_price,
        buy_time="2026-08-03 10:00:00",
        buy_regime="STABLE_BULL",
        sell_price=sell_price,
    )


def test_day_one_mark_overnight_reconciles_on_day_two_then_late_failure_closes(
    tmp_path,
):
    day_one = datetime(2026, 8, 3, 15, 20, tzinfo=KST)
    day_two = datetime(2026, 8, 4, 15, 27, tzinfo=KST)

    def clock():
        return day_one
    ledger = TradeLogger(tmp_path / "legacy.sqlite3", clock=clock)
    try:
        manager = _manager(
            ledger,
            clock,
            current_session_resolver=lambda now: (
                date(2026, 8, 3) if now == day_one else date(2026, 8, 4)
            ),
        )
        position = _buy(manager)
        strategy = TradingStrategy({"debug_mode": False}, clock=clock)

        assert strategy.decide_position(
            position,
            10_000.0,
            {"current_velocity": 3.0, "thrust": 2.1, "magnetic": 0.1, "jerk": 0.0},
            day_one,
        ).decision.value == "MARK_OVERNIGHT"
        manager.apply_paper_mark_overnight(position)
        assert position.status is PositionStatus.OVERNIGHT

        def clock():
            return day_two
        manager._clock = clock
        manager._current_session_resolver = lambda _now: date(2026, 8, 4)
        manager._next_session_resolver = lambda _owner: date(2026, 8, 4)
        assert manager.reconcile_overnight_positions() == 1
        assert position.status is PositionStatus.OPEN

        # The late-session overnight qualification is false; legacy forced close wins.
        reason = strategy.get_exit_reason(
            position,
            10_100.0,
            {"current_velocity": 0.0, "thrust": 0.0, "magnetic": 0.0, "jerk": 0.0},
            day_two,
        )
        assert reason == "Day Trade Close"
    finally:
        ledger.close()


def test_fixed_three_percentage_point_exits_precede_negative_jerk_exit():
    strategy = TradingStrategy({"debug_mode": True})
    forces = {"jerk": -0.6, "thrust": 0.0}

    assert strategy.get_exit_reason(_position(buy_price=100.0), 103.0, forces) == (
        "Fixed Target (3 %p; percentage-points-v1)"
    )
    assert strategy.get_exit_reason(_position(buy_price=100.0), 97.0, forces) == (
        "Fixed Stop (-3 %p; percentage-points-v1)"
    )


def test_fresh_strategy_does_not_retain_kinetic_high_water_mark():
    config = {"debug_mode": True}
    first = TradingStrategy(config)
    position = _position()
    position.atr_percent = 0.5
    position.down_atr_percent = 0.5

    assert first.get_exit_reason(position, 102.5, {"jerk": 0.0, "thrust": 1.0}) is None
    assert first.get_exit_reason(position, 101.5, {"jerk": 0.0, "thrust": 1.0}) == (
        "Trailing Stop (Profit Retention)"
    )

    fresh = TradingStrategy(config)
    assert fresh.get_exit_reason(position, 101.5, {"jerk": 0.0, "thrust": 1.0}) is None


def test_unweighted_percentage_points_can_flip_nominal_pnl_sign():
    manager = StockManager(MagicMock(), MagicMock(load_active_positions=lambda: {}), {})
    manager.active_positions = {
        "small": _position(code="small", buy_price=100.0, sell_price=110.0),
        "large": _position(code="large", buy_price=100_000.0, sell_price=95_000.0),
    }

    score = manager.calculate_cumulative_trade_return_score(0.0)
    nominal_pnl = (110.0 - 100.0) + (95_000.0 - 100_000.0)
    assert score == 5.0
    assert nominal_pnl < 0.0


def test_sell_removes_active_position_and_same_signal_allows_legacy_reentry(tmp_path):
    now = datetime(2026, 8, 3, 10, 0, tzinfo=KST)

    def clock():
        return now
    ledger = TradeLogger(tmp_path / "reentry.sqlite3", clock=clock)
    try:
        manager = _manager(
            ledger,
            clock,
            current_session_resolver=lambda _now: date(2026, 8, 3),
        )
        _buy(manager)
        manager.apply_paper_sell(
            {"stock_code": "005930", "price": 10_100.0},
            "test exit",
        )
        assert "005930" not in manager.active_positions

        strategy = TradingStrategy({"debug_mode": True}, clock=clock)
        engine = TradingEngine.__new__(TradingEngine)
        engine.stock_mgr = manager
        engine.strategy = strategy
        assert engine._should_enter(
            {"stock_code": "005930", "is_buy_signal": True}
        ) is True
        success, _ = manager.apply_paper_buy(
            {
                "stock_code": "005930",
                "price": 10_100.0,
                "regime": "STABLE_BULL",
                "forces": {},
            }
        )
        assert success is True
        assert "005930" in manager.active_positions
    finally:
        ledger.close()


def test_legacy_trades_schema_has_no_swing_fields_and_unknowns_are_not_inferable(
    tmp_path,
):
    ledger = TradeLogger(tmp_path / "schema.sqlite3")
    try:
        columns = {
            row[1]
            for row in ledger.conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        ledger.record_buy({
            "stock_code": "005930",
            "stock_name": "Samsung",
            "buy_price": 10_000.0,
            "buy_time": "2026-08-03 10:00:00",
            "buy_regime": "STABLE_BULL",
            "thrust": 0.0,
            "gravity": 0.0,
            "drag": 0.0,
            "magnetic": 0.0,
            "jerk": 0.0,
            "impulse": 0.0,
            "net_force": 0.0,
        })
        representative = dict(
            ledger.conn.execute("SELECT * FROM trades LIMIT 1").fetchone()
        )
    finally:
        ledger.close()

    assert set(CANONICAL_SWING_FIELDS).isdisjoint(columns)
    assert _classify_legacy_row_for_candidate(representative) == "INSUFFICIENT_DATA"


def test_hard_risk_allowlist_requires_raw_price_and_versioned_threshold():
    assert HARD_RISK_ALLOWLIST == {
        "CATASTROPHIC_PRICE_RISK",
        "PORTFOLIO_RISK_LIMIT",
    }
    assert _hard_risk_is_executable("CATASTROPHIC_PRICE_RISK", 100, "risk-v1")
    assert _hard_risk_is_executable("PORTFOLIO_RISK_LIMIT", 100, "risk-v1")
    assert not _hard_risk_is_executable("THESIS_INVALIDATION", 100, "risk-v1")
    assert not _hard_risk_is_executable("PORTFOLIO_RISK_LIMIT", None, "risk-v1")
    assert not _hard_risk_is_executable("PORTFOLIO_RISK_LIMIT", 100, None)


def test_mark_provenance_revision_fields_and_incomplete_qualities_are_explicit():
    mark = {
        "source_id": "feed-v1:005930:2026-08-03",
        "available_at": datetime(2026, 8, 3, 15, 30, tzinfo=KST),
        "computed_at": datetime(2026, 8, 3, 15, 31, tzinfo=KST),
        "revision": 2,
        "supersedes_id": "mark-1",
    }
    assert MARK_PROVENANCE_REQUIRED_FIELDS <= mark.keys()
    assert _mark_has_provenance(mark)
    assert INCOMPLETE_MARK_QUALITIES == {"SUSPENDED_CARRY_FORWARD", "MISSING"}
    assert not _mark_has_provenance({**mark, "revision": None})


def test_unknown_corporate_action_is_insufficient_data_before_decision():
    decision_at = datetime(2026, 8, 3, 10, 0, tzinfo=KST)
    assert _classify_corporate_action(None, decision_at) == "INSUFFICIENT_DATA"
    assert _classify_corporate_action(
        {"available_at": datetime(2026, 8, 3, 10, 1, tzinfo=KST)}, decision_at
    ) == "INSUFFICIENT_DATA"
    assert _classify_corporate_action(
        {"available_at": datetime(2026, 8, 3, 9, 59, tzinfo=KST)}, decision_at
    ) == "KNOWN"


def test_episode_rearm_requires_flat_position_and_all_neutral_predicates():
    assert _episode_can_rearm(
        flat=True, slow_false=True, completed_fast_false_count=2, cooldown_sessions=1
    )
    assert not _episode_can_rearm(
        flat=False, slow_false=True, completed_fast_false_count=2, cooldown_sessions=1
    )
    assert not _episode_can_rearm(
        flat=True, slow_false=False, completed_fast_false_count=2, cooldown_sessions=1
    )
    assert not _episode_can_rearm(
        flat=True, slow_false=True, completed_fast_false_count=1, cooldown_sessions=1
    )
    assert not _episode_can_rearm(
        flat=True, slow_false=True, completed_fast_false_count=2, cooldown_sessions=0
    )


def test_fill_temporal_ordering_and_unfilled_no_cash_contract():
    decision_at = datetime(2026, 8, 3, 10, 1, tzinfo=KST)
    next_open = datetime(2026, 8, 3, 10, 2, tzinfo=KST)
    assert _fill_contract_is_valid(
        decision_at=decision_at,
        fill_at=next_open,
        slow_context_completed=True,
        fast_bar_completed=True,
        next_regular_bar_open=next_open,
        filled=True,
        cash_delta=-100,
    )
    assert _fill_contract_is_valid(
        decision_at=decision_at,
        fill_at=next_open,
        slow_context_completed=True,
        fast_bar_completed=True,
        next_regular_bar_open=next_open,
        filled=False,
        cash_delta=0,
    )
    assert not _fill_contract_is_valid(
        decision_at=next_open,
        fill_at=decision_at,
        slow_context_completed=True,
        fast_bar_completed=True,
        next_regular_bar_open=decision_at,
        filled=True,
        cash_delta=-100,
    )
    assert not _fill_contract_is_valid(
        decision_at=decision_at,
        fill_at=next_open,
        slow_context_completed=False,
        fast_bar_completed=True,
        next_regular_bar_open=next_open,
        filled=True,
        cash_delta=-100,
    )
