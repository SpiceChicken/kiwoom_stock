from datetime import date, datetime, timezone

import pytest

from kiwoom_stock.domain.swing_contracts import (
    AdmissionEvent, AdmissionResult, ContractError, EpisodeEventType, EpisodeRearmEvidence,
    EpisodeSnapshot, EpisodeState, FillTiming, InsufficientDataError, Mark, MarkQuality,
    TemporalCausalityError, legacy_unknown, reduce_episode, validate_episode_transition,
)

T = datetime(2026, 8, 16, 9, tzinfo=timezone.utc)


def valid_mark(quality=MarkQuality.OFFICIAL_CLOSE, **kwargs):
    return Mark(date(2026, 8, 17), None if quality is MarkQuality.MISSING else 70_000, quality,
                "source", T, T, 1, portfolio_id="paper", position_id="pos-1", symbol="005930", **kwargs)


def test_mark_requires_full_typed_identity_and_incomplete_quality():
    assert valid_mark().permits_new_entry
    assert not valid_mark(MarkQuality.MISSING).permits_new_entry
    with pytest.raises(ContractError):
        Mark(date.today(), 1, MarkQuality.OFFICIAL_CLOSE, "source", T, T, 1)
    with pytest.raises(ContractError):
        Mark(
            date.today(),
            1,
            "OFFICIAL_CLOSE",
            "source",
            T,
            T,
            1,
            portfolio_id="paper",
            position_id="p",
            symbol="s")  # type: ignore[arg-type]


def test_fill_timing_requires_proof_bearing_next_bar():
    timing = FillTiming(
        T, T.replace(
            day=17), "next", T.date(), date(
            2026, 8, 17), T.replace(
                day=17), "previous", date(
                    2026, 8, 15), 10, 11, True, T.replace(
                        day=15, hour=15))
    assert timing.fill_at == timing.bar_open_at
    with pytest.raises(TemporalCausalityError):
        FillTiming(
            T, T.replace(
                day=17), "next", T.date(), T.date(), T.replace(
                day=17), "previous", date(
                2026, 8, 15), 10, 11, True, T.replace(
                    day=15, hour=15))


def test_episode_single_typed_graph_and_terminal_version():
    signal = AdmissionEvent("e", "v1", True, AdmissionResult.FILLED, "signal", EpisodeEventType.SIGNAL)
    admission = AdmissionEvent("e", "v1", True, AdmissionResult.FILLED, "attempt", EpisodeEventType.ADMISSION)
    exit_event = AdmissionEvent("e", "v1", False, AdmissionResult.REJECTED, "exit", EpisodeEventType.EXIT)
    rearm = AdmissionEvent("e", "v1", True, AdmissionResult.FILLED, "rearm", EpisodeEventType.REARM)
    state = EpisodeSnapshot(EpisodeState.ARMED, "v1")
    state = reduce_episode(state, signal, current_version="v1")
    state = reduce_episode(state, admission, current_version="v1")
    assert state.admission_results == (("attempt", AdmissionResult.FILLED),)
    state = reduce_episode(state, exit_event, current_version="v1")
    state = reduce_episode(state, rearm, current_version="v1", evidence=EpisodeRearmEvidence(True, True, 2, 1))
    assert state.state is EpisodeState.ARMED
    with pytest.raises(ContractError):
        reduce_episode(state, rearm, current_version="v1", evidence=EpisodeRearmEvidence(True, True, 2, 1))
    assert reduce_episode(state, signal, current_version="old").state is EpisodeState.TERMINAL
    with pytest.raises(ContractError):
        validate_episode_transition(EpisodeState.COOLDOWN, EpisodeState.ARMED)


def test_action_and_legacy_malformed_evidence_fail_closed():
    with pytest.raises(InsufficientDataError):
        legacy_unknown(
            quantity=True,
            cost_krw=1,
            episode_id="e",
            horizon=2,
            mark=valid_mark())  # type: ignore[arg-type]
    with pytest.raises(InsufficientDataError):
        legacy_unknown(quantity=1, cost_krw=1.5, episode_id="e", horizon=2, mark=valid_mark())  # type: ignore[arg-type]
    with pytest.raises(InsufficientDataError):
        legacy_unknown(
            quantity=1,
            cost_krw=1,
            episode_id="e",
            horizon=True,
            mark=valid_mark())  # type: ignore[arg-type]
    with pytest.raises(InsufficientDataError):
        legacy_unknown(quantity=1, cost_krw=1, episode_id="e", horizon=2, mark="not-a-mark")  # type: ignore[arg-type]
