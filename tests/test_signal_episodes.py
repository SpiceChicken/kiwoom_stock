from datetime import date

import pytest

from kiwoom_stock.application.ports import (
    SwingEpisodeAppendCommand,
    SwingIdempotencyConflictError,
    SwingTransitionConflictError,
)
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy
from kiwoom_stock.domain.swing_contracts import (
    AdmissionEvent,
    AdmissionResult,
    EpisodeEventType,
    EpisodeRearmEvidence,
    EpisodeState,
)


def _ledger(path):
    return SwingLedger(
        path,
        portfolio_id="candidate",
        policy=AccountingPolicy("p1", 1_000, CostPolicy("base"), CostPolicy("stress")),
    )


def _event(event_id, event_type, *, rising, result=AdmissionResult.REJECTED, version="swing-v1"):
    return AdmissionEvent("episode-1", version, rising, result, event_id, event_type)


def _command(event, key, expected, *, evidence=None, previous=None, current=None):
    return SwingEpisodeAppendCommand(
        key,
        "episode-1",
        event,
        expected,
        evidence,
        current,
        previous,
    )


def _register(ledger):
    ledger.register_portfolio(idempotency_key="register")


def test_episode_cycle_consumes_rejected_admission_and_rearms_after_real_cooldown(tmp_path):
    ledger = _ledger(tmp_path / "episode.sqlite3")
    _register(ledger)
    ledger.append_episode(_command(_event("signal-1", EpisodeEventType.SIGNAL, rising=True), "signal", 0))
    ledger.append_episode(
        _command(
            _event("admission-1", EpisodeEventType.ADMISSION, rising=True, result=AdmissionResult.UNFILLED),
            "admission",
            1,
        )
    )
    ledger.append_episode(_command(_event("reject-1", EpisodeEventType.REJECT, rising=False), "reject", 2))
    rearm = EpisodeRearmEvidence(True, True, 2, 1, False)
    ledger.append_episode(
        _command(
            _event("rearm-1", EpisodeEventType.REARM, rising=True, result=AdmissionResult.FILLED),
            "rearm",
            3,
            evidence=rearm,
            previous=date(2026, 8, 19),
            current=date(2026, 8, 20),
        )
    )
    hydrated = ledger.hydrate_episode(episode_id="episode-1")
    assert hydrated.snapshot.state is EpisodeState.ARMED
    assert hydrated.snapshot.admission_results == (("admission-1", AdmissionResult.UNFILLED),)
    assert hydrated.verified_sequence == 4
    assert hydrated.event_ids == ("signal-1", "admission-1", "reject-1", "rearm-1")
    ledger.close()


def test_episode_idempotency_and_expected_sequence_are_exactly_once(tmp_path):
    path = tmp_path / "episode.sqlite3"
    ledger = _ledger(path)
    _register(ledger)
    command = _command(_event("signal-1", EpisodeEventType.SIGNAL, rising=True), "signal", 0)
    first = ledger.append_episode(command)
    replay = ledger.append_episode(command)
    assert first.replayed is False
    assert replay.replayed is True
    with pytest.raises(SwingIdempotencyConflictError):
        ledger.append_episode(_command(_event("other", EpisodeEventType.SIGNAL, rising=True), "signal", 0))
    with pytest.raises(SwingTransitionConflictError):
        ledger.append_episode(_command(_event("admission-1", EpisodeEventType.ADMISSION, rising=True), "stale", 0))
    ledger.close()


def test_persistent_signal_or_invalid_cooldown_cannot_rearm(tmp_path):
    ledger = _ledger(tmp_path / "episode.sqlite3")
    _register(ledger)
    ledger.append_episode(_command(_event("signal-1", EpisodeEventType.SIGNAL, rising=True), "signal", 0))
    ledger.append_episode(_command(_event("admission-1", EpisodeEventType.ADMISSION, rising=True), "admission", 1))
    ledger.append_episode(_command(_event("exit-1", EpisodeEventType.EXIT, rising=False), "exit", 2))
    with pytest.raises(SwingTransitionConflictError):
        ledger.append_episode(
            _command(
                _event("rearm-persistent", EpisodeEventType.REARM, rising=True),
                "rearm-persistent",
                3,
                evidence=EpisodeRearmEvidence(True, True, 2, 1, True),
                previous=date(2026, 8, 19),
                current=date(2026, 8, 20),
            )
        )
    with pytest.raises(SwingTransitionConflictError):
        ledger.append_episode(
            _command(
                _event("rearm-weekend", EpisodeEventType.REARM, rising=True),
                "rearm-weekend",
                3,
                evidence=EpisodeRearmEvidence(True, True, 2, 1, False),
                previous=date(2026, 8, 20),
                current=date(2026, 8, 22),
            )
        )
    ledger.close()


def test_episode_restart_and_projection_tamper_fail_closed(tmp_path):
    path = tmp_path / "episode.sqlite3"
    ledger = _ledger(path)
    _register(ledger)
    ledger.append_episode(_command(_event("signal-1", EpisodeEventType.SIGNAL, rising=True), "signal", 0))
    ledger.close()
    reopened = _ledger(path)
    assert reopened.hydrate_episode(episode_id="episode-1").snapshot.state is EpisodeState.ACTIVE
    reopened.close()

    connection = __import__("sqlite3").connect(path)
    connection.execute("DROP TRIGGER swing_episode_snapshots_v1_immutable_update")
    connection.execute("UPDATE swing_episode_snapshots_v1 SET payload_json='{}'")
    connection.commit()
    connection.close()
    with pytest.raises(Exception):
        _ledger(path).hydrate_episode(episode_id="episode-1")


def test_episode_semantic_version_mismatch_is_rejected(tmp_path):
    ledger = _ledger(tmp_path / "episode.sqlite3")
    _register(ledger)
    with pytest.raises(SwingTransitionConflictError):
        ledger.append_episode(
            _command(
                _event("old-version", EpisodeEventType.SIGNAL, rising=True, version="old-version"),
                "old-version",
                0,
            )
        )
    assert ledger.hydrate_episode(episode_id="episode-1").verified_sequence == 0
    ledger.close()
