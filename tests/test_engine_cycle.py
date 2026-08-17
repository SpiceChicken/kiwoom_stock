from datetime import date, datetime, timezone

import pytest

from kiwoom_stock.application.session import (
    CycleContext,
    SessionEndReason,
    TradingSessionResult,
)
from kiwoom_stock.monitoring.engine_cycle import (
    TradingCycleCoordinator,
    acknowledge_due_targets,
    complete_cycle_targets,
)


def test_cycle_target_helpers_are_deterministic_and_non_mutating():
    previous = {"A": 10.0}

    assert complete_cycle_targets(["B"], {"C": object()}) == ["B", "C"]
    assert acknowledge_due_targets(
        ["B"], 20.0, ["A", "B"], previous
    ) == {"A": 10.0, "B": 20.0}
    assert previous == {"A": 10.0}

    with pytest.raises(ValueError, match="unique"):
        complete_cycle_targets(["B", "B"], set())
    with pytest.raises(ValueError, match="selected"):
        acknowledge_due_targets(["C"], 20.0, ["A", "B"], previous)


def test_normal_cycle_coordinator_preserves_authoritative_order():
    context = CycleContext(
        now=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
        xkrx_session_date=date(2026, 8, 17),
    )
    events = []

    coordinator = TradingCycleCoordinator(
        check_monitoring_status=lambda _context: events.append("admit") or True,
        reconcile_overnight_positions=lambda _context: events.append("reconcile"),
        refresh_global_state=lambda: events.append("refresh"),
        get_due_targets=lambda: events.append("due") or ["005930"],
        complete_targets=lambda targets: events.append("complete") or targets,
        prepare_cycle=lambda targets: events.append(("prepare", targets)),
        fresh_active_marks=lambda targets: events.append("marks") or {},
        check_terminal_status=lambda _context, _marks: events.append("terminal") or None,
        acknowledge_targets=lambda targets, selected_at: events.append(
            ("ack", targets, selected_at)
        ),
        evaluate_stocks=lambda targets: events.append(("evaluate", targets)) or [],
        process_decisions=lambda verdicts, _context: events.append(
            ("process", verdicts)
        ),
        flush_status=lambda regime: events.append(("flush", regime)),
        market_regime_value=lambda: "STABLE_BULL",
    )

    outcome = coordinator.run(context, 160.0, 100.0)

    assert outcome.terminal_result is None
    assert outcome.next_global_update == 160.0
    assert events == [
        "admit",
        "reconcile",
        "refresh",
        "due",
        "complete",
        ("prepare", ["005930"]),
        "marks",
        "terminal",
        ("ack", ["005930"], 160.0),
        ("evaluate", ["005930"]),
        ("process", []),
        ("flush", "STABLE_BULL"),
    ]


def test_terminal_cycle_does_not_acknowledge_or_evaluate():
    context = CycleContext(
        now=datetime(2026, 8, 17, 10, 0, tzinfo=timezone.utc),
        xkrx_session_date=date(2026, 8, 17),
    )
    events = []

    coordinator = TradingCycleCoordinator(
        check_monitoring_status=lambda _context: True,
        reconcile_overnight_positions=lambda _context: None,
        refresh_global_state=lambda: None,
        get_due_targets=lambda: ["005930"],
        complete_targets=lambda targets: targets,
        prepare_cycle=lambda _targets: events.append("prepare"),
        fresh_active_marks=lambda _targets: {},
        check_terminal_status=lambda _context, _marks: events.append("terminal")
        or TradingSessionResult(reason=SessionEndReason.MARKET_CLOSED),
        acknowledge_targets=lambda _targets, _selected_at: events.append("ack"),
        evaluate_stocks=lambda _targets: events.append("evaluate") or [],
        process_decisions=lambda _verdicts, _context: events.append("process"),
        flush_status=lambda _regime: events.append("flush"),
        market_regime_value=lambda: "STABLE_BULL",
    )

    outcome = coordinator.run(context, 100.0, 100.0)

    assert outcome.terminal_result is not None
    assert outcome.terminal_result.reason is SessionEndReason.MARKET_CLOSED
    assert events == ["prepare", "terminal"]
