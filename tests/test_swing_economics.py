from datetime import date

import pytest

from kiwoom_stock.domain.swing_economics import (
    EpisodeFillObservation,
    EpisodeMarkObservation,
    SwingEconomicsError,
    SwingEconomicComparison,
    aggregate_episode_outcomes,
    build_episode_outcome,
)


def leg(
    fill_id: str,
    episode_id: str,
    side: str,
    session: date,
    gross: int,
    base: int,
    stress: int,
) -> EpisodeFillObservation:
    return EpisodeFillObservation(
        fill_id,
        episode_id,
        f"position-{episode_id}",
        "005930",
        side,
        1,
        session,
        gross,
        base,
        stress,
    )


def closed_episode(episode_id: str):
    entry = leg(f"{episode_id}-buy", episode_id, "BUY", date(2026, 8, 18), -1000, -1005, -1010)
    exit_leg = leg(f"{episode_id}-sell", episode_id, "SELL", date(2026, 8, 19), 1000, 995, 990)
    return build_episode_outcome((entry, exit_leg), holding_sessions=2)


def test_episode_economics_exposes_after_cost_loss_and_long_hold_comparison():
    baseline = aggregate_episode_outcomes(
        "synthetic-economics-v1",
        (closed_episode("repeat-1"), closed_episode("repeat-2")),
    )
    candidate = build_episode_outcome(
        (
            leg("long-buy", "long-1", "BUY", date(2026, 8, 18), -1000, -1005, -1010),
        ),
        holding_sessions=5,
        mark=EpisodeMarkObservation(
            "long-1",
            "position-long-1",
            "005930",
            date(2026, 8, 22),
            1025,
            5,
            10,
        ),
    )
    candidate_aggregate = aggregate_episode_outcomes(
        "synthetic-economics-v1",
        (candidate,),
    )
    comparison = SwingEconomicComparison(baseline, candidate_aggregate)

    assert baseline.base_pnl_krw == -20
    assert baseline.stress_pnl_krw == -40
    assert baseline.base_cost_krw == 20
    assert candidate.base_pnl_krw == 15
    assert candidate.unrealized_base_pnl_krw == 15
    assert comparison.base_pnl_delta_krw == 35
    assert comparison.stress_pnl_delta_krw == 45
    assert comparison.base_cost_delta_krw == -10
    assert comparison.fill_count_delta == -3
    assert comparison.holding_sessions_delta == 3  # candidate set has 5 vs baseline average 2


def test_open_episode_requires_complete_identity_bound_mark():
    entry = leg("buy", "episode-1", "BUY", date(2026, 8, 18), -1000, -1005, -1010)
    with pytest.raises(SwingEconomicsError):
        build_episode_outcome((entry,), holding_sessions=3)

    with pytest.raises(SwingEconomicsError):
        build_episode_outcome(
            (entry,),
            holding_sessions=3,
            mark=EpisodeMarkObservation(
                "other-episode",
                "position-episode-1",
                "005930",
                date(2026, 8, 20),
                1020,
                5,
                10,
            ),
        )


def test_episode_and_aggregate_reject_churn_or_identity_loss():
    entry = leg("buy-1", "episode-1", "BUY", date(2026, 8, 18), -1000, -1005, -1010)
    second_entry = leg("buy-2", "episode-1", "BUY", date(2026, 8, 19), -1000, -1005, -1010)
    with pytest.raises(SwingEconomicsError):
        build_episode_outcome((entry, second_entry), holding_sessions=2)

    outcome = closed_episode("episode-1")
    with pytest.raises(SwingEconomicsError):
        aggregate_episode_outcomes("dataset", (outcome, outcome))

    first = aggregate_episode_outcomes("dataset-a", (outcome,))
    second = aggregate_episode_outcomes("dataset-b", (outcome,))
    with pytest.raises(SwingEconomicsError):
        SwingEconomicComparison(first, second)
