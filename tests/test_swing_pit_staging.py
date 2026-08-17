import csv
from datetime import date
import io
import json
from pathlib import Path

from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy
from kiwoom_stock.infrastructure.point_in_time_replay import (
    CsvPITReplaySource,
    SWING_PIT_REPLAY_COLUMNS,
    SWING_PIT_REPLAY_SCHEMA,
)
from kiwoom_stock.infrastructure.swing_pit_staging import (
    run_csv_swing_staging_hash_parity,
)


def _context_payload() -> dict:
    return {
        "swing_context": {
            "schema_version": "swing-context-v1",
            "strategy_policy": {
                "semantic_version": "swing-v1",
                "hard_risk_threshold_version": "risk-v1",
                "minimum_holding_session": 2,
                "maximum_holding_session": 20,
            },
            "episode_id": "episode-1",
            "slow": {
                "session_date": "2026-08-17",
                "bar_closed_at": "2026-08-17T01:00:00+00:00",
                "available_at": "2026-08-17T01:00:00+00:00",
                "computed_at": "2026-08-17T01:00:00+00:00",
                "source_snapshot_id": "slow-1",
                "strategy_semantics_version": "swing-v1",
                "lookback_sessions": 20,
                "warmup_complete": True,
                "thesis_valid": True,
                "entry_eligible": True,
                "score": "1.0",
            },
            "fast": {
                "bar_id": "fast-1",
                "bar_closed_at": "2026-08-18T00:59:00+00:00",
                "available_at": "2026-08-18T00:59:00+00:00",
                "computed_at": "2026-08-18T00:59:00+00:00",
                "source_snapshot_id": "fast-1",
                "strategy_semantics_version": "swing-v1",
                "trigger_rising": True,
                "entry_eligible": True,
                "score": "1.0",
            },
            "risk": {
                "raw_executable_price_krw": 70_000,
                "holding_session_number": 1,
                "mark_complete": True,
                "entry_capacity_available": True,
                "hard_risk_reason": None,
                "target_hit": False,
                "stop_hit": False,
                "same_bar_ambiguous": False,
            },
            "position": {"active": False, "position_id": "", "symbol": ""},
            "episode": {
                "state": "ARMED",
                "semantic_version": "swing-v1",
                "consumed_event_ids": [],
                "admission_results": [],
            },
            "rearm_evidence": None,
        }
    }


def _write_pit_csv(path: Path) -> None:
    path.parent.mkdir(parents=True)
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(SWING_PIT_REPLAY_COLUMNS)
    writer.writerow(
        (
            SWING_PIT_REPLAY_SCHEMA,
            "event-1",
            "2026-08-18",
            "2026-08-18T01:00:00+00:00",
            "2026-08-18T00:58:00+00:00",
            "snapshot-ctx",
            json.dumps(_context_payload(), separators=(",", ":")),
        )
    )
    path.write_text(stream.getvalue(), encoding="utf-8-sig")


def test_csv_staging_composition_connects_loader_provider_and_real_evaluator(
    tmp_path,
    monkeypatch,
):
    csv_path = tmp_path / "output" / "20260818" / "pit.csv"
    _write_pit_csv(csv_path)
    source = CsvPITReplaySource.from_artifact(
        output_root=tmp_path,
        session_date=date(2026, 8, 18),
        filename="pit.csv",
        dataset_id="approved-pit-v1",
    )
    candidate_path = tmp_path / "candidate.sqlite3"
    policy = AccountingPolicy(
        "accounting-v1",
        1_000_000,
        CostPolicy("base-v1"),
        CostPolicy("stress-v1"),
    )
    writable = SwingLedger(
        candidate_path,
        portfolio_id="portfolio-v1",
        policy=policy,
    )
    writable.register_portfolio(idempotency_key="register-1")
    writable.close()

    open_calls = []
    import kiwoom_stock.infrastructure.swing_pit_staging as staging

    real_open = staging.open_read_only_swing_candidate_state_provider

    def observe_open(*args, **kwargs):
        open_calls.append(True)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(staging, "open_read_only_swing_candidate_state_provider", observe_open)
    evidence = run_csv_swing_staging_hash_parity(
        source=source,
        strategy_semantics_version="swing-v1",
        candidate_enabled=True,
        candidate_database_path=candidate_path,
        candidate_portfolio_id="portfolio-v1",
        candidate_episode_id="episode-1",
        accounting_policy=policy,
        legacy_evaluator=lambda snapshot: {"action": "HOLD", "snapshot_id": snapshot.snapshot_id},
    )

    assert len(open_calls) == 2
    assert evidence.first_run.dataset_id == "approved-pit-v1"
    assert evidence.first_run.input_hash == evidence.second_run.input_hash
    assert evidence.first_run.output_hash == evidence.second_run.output_hash
    assert evidence.candidate_call_count_first == 1
    assert evidence.candidate_call_count_second == 1
    assert evidence.side_effects is False


def test_csv_staging_composition_disabled_candidate_has_no_provider_or_candidate_calls(
    tmp_path,
):
    csv_path = tmp_path / "output" / "20260818" / "pit.csv"
    _write_pit_csv(csv_path)
    source = CsvPITReplaySource.from_artifact(
        output_root=tmp_path,
        session_date=date(2026, 8, 18),
        filename="pit.csv",
        dataset_id="approved-pit-disabled-v1",
    )

    evidence = run_csv_swing_staging_hash_parity(
        source=source,
        strategy_semantics_version="swing-v1",
        candidate_enabled=False,
        legacy_evaluator=lambda _: {"action": "HOLD"},
    )

    assert evidence.candidate_call_count_first == 0
    assert evidence.candidate_call_count_second == 0
    assert evidence.first_run.output_hash == evidence.second_run.output_hash
