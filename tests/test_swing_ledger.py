from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from kiwoom_stock.application.ports import (
    SwingFillCommand, SwingIdentityConflictError, SwingMarkCommand,
    SwingPortfolioNotRegisteredError, SwingTransitionConflictError,
    SwingIntegrityError, SwingSchemaIncompatibleError,
)
from kiwoom_stock.core.swing_ledger import SwingLedger
from kiwoom_stock.core.swing_schema import migrate_swing_schema
from kiwoom_stock.domain.accounting import AccountingPolicy, CostPolicy, Fill
from kiwoom_stock.domain.swing_contracts import FillTiming, FillTimingEvidence, Mark, MarkQuality, SessionMarkEvidence
from kiwoom_stock.utils.market_cal import krx_session_ordinals

UTC = timezone.utc


def policy(cash=1_000):
    return AccountingPolicy("p1", cash, CostPolicy("base"), CostPolicy("stress"))


def fill(position="pos", side="BUY", portfolio="candidate", symbol="005930", price=100):
    decision = datetime(2026, 8, 18, 1, tzinfo=UTC)
    opened = decision + timedelta(days=1)
    ordinals = krx_session_ordinals(date(2026, 8, 14), opened.date())
    timing = FillTiming(decision, opened, "bar-1", decision.date(), opened.date(), opened,
                        "bar-0", date(2026, 8, 14), 1, 2,
                        previous_completed_at=datetime(2026, 8, 14, 7, tzinfo=UTC),
                        session_evidence=FillTimingEvidence(ordinals[decision.date()], ordinals[date(2026, 8, 14)], ordinals[opened.date()]))
    return Fill("fill-" + position + side, portfolio, symbol, side, 1, price,
                decision, opened, timing=timing, position_id=position)


def mark(session=None, revision=1, supersedes=None):
    if isinstance(session, datetime):
        session = session.date()
    session_date = session or datetime(2026, 8, 19).date()
    previous_session = datetime(
        2026,
        8,
        18).date() if session_date == datetime(
        2026,
        8,
        19).date() else datetime(
            2026,
            8,
        19).date()
    ordinals = krx_session_ordinals(previous_session, session_date)
    available = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    return Mark(
        available.date(),
        101,
        MarkQuality.OFFICIAL_CLOSE,
        "close-1",
        available,
        available +
        timedelta(
            hours=1),
        revision,
        supersedes,
        portfolio_id="candidate",
        position_id="pos",
        symbol="005930",
        session_evidence=SessionMarkEvidence(
            session_date,
            ordinals[session_date],
            previous_session,
            ordinals[previous_session]))


def ledger(path):
    return SwingLedger(path, portfolio_id="candidate", policy=policy())


def test_bound_register_fill_mark_hydrate_and_reopen(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    current = ledger(path)
    current.register_portfolio(idempotency_key="register")
    receipt = current.append_fill(SwingFillCommand(fill(), "buy-1", 0, 0))
    assert receipt.committed_portfolio_sequence == 1
    current.append_mark(SwingMarkCommand(mark(), "mark-1", 1, 1, 0))
    hydrated = current.hydrate(portfolio_id="candidate")
    assert hydrated.state.cash_krw == 900
    current.close()
    current = ledger(path)
    assert current.hydrate(portfolio_id="candidate").state.cash_krw == 900
    current.close()


def test_idempotency_envelope_replay_and_expected_conflict(tmp_path):
    current = ledger(tmp_path / "candidate.sqlite3")
    current.register_portfolio(idempotency_key="register")
    command = SwingFillCommand(fill(), "buy-1", 0, 0)
    assert current.append_fill(command).replayed is False
    assert current.append_fill(command).replayed is True
    with pytest.raises(Exception):
        current.append_fill(SwingFillCommand(fill(position="other"), "buy-1", 0, 0))
    with pytest.raises(SwingTransitionConflictError):
        current.append_fill(SwingFillCommand(fill(position="other"), "buy-2", 0, 0))


def test_invalid_sell_overspend_and_identity_are_atomic(tmp_path):
    current = ledger(tmp_path / "candidate.sqlite3")
    current.register_portfolio(idempotency_key="register")
    with pytest.raises(Exception):
        current.append_fill(SwingFillCommand(fill(side="SELL"), "sell", 0, 0))
    with pytest.raises(Exception):
        current.append_fill(SwingFillCommand(fill(position="other", price=2_000), "overspend", 0, 0))
    with pytest.raises(SwingIdentityConflictError):
        current.append_fill(SwingFillCommand(fill(portfolio="other"), "cross", 0, 0))
    assert current.hydrate(portfolio_id="candidate").state.cash_krw == 1_000


def test_schema_savepoint_and_legacy_coexistence(tmp_path):
    connection = sqlite3.connect(tmp_path / "candidate.sqlite3")
    connection.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, status TEXT)")
    connection.execute("INSERT INTO trades VALUES (1, 'OPEN')")
    connection.commit()
    connection.execute("BEGIN")
    connection.execute("CREATE TABLE sentinel (value TEXT)")
    with pytest.raises(SwingSchemaIncompatibleError):
        migrate_swing_schema(connection)
    assert connection.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='swing_portfolios_v1'").fetchone()[0] == 0
    connection.commit()


def test_swing_schema_catalog_and_representative_rows_are_exact(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    current = ledger(path)
    current.register_portfolio(idempotency_key="register")
    current.append_fill(SwingFillCommand(fill(), "buy-1", 0, 0))
    current.close()

    connection = sqlite3.connect(path)
    expected_tables = (
        "swing_schema_meta_v1", "swing_portfolios_v1",
        "swing_position_identities_v1", "swing_commands_v1", "swing_fills_v1",
        "swing_lifecycle_events_v1", "swing_daily_marks_v1",
        "swing_position_snapshots_v1", "swing_portfolio_snapshots_v1",
        "swing_episode_events_v1", "swing_episode_snapshots_v1",
    )
    actual_tables = tuple(
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'swing_%_v1' ORDER BY name"
        )
    )
    assert actual_tables == tuple(sorted(expected_tables))
    assert connection.execute(
        "SELECT schema_version, shape_hash FROM swing_schema_meta_v1 "
        "WHERE schema_name='swing'"
    ).fetchone() == (
        "swing-p4-episode-v1",
        "c4eff44a9be012b4748dde8cf29bcd3abd2197eb623ffe4461d942e5b66f8f63",
    )

    expected_columns = {
        "swing_schema_meta_v1": ("schema_name", "schema_version", "shape_hash", "created_at"),
        "swing_portfolios_v1": ("portfolio_id", "initial_cash_krw", "policy_version", "policy_hash", "created_at", "row_hash"),
        "swing_position_identities_v1": ("portfolio_id", "position_id", "symbol", "created_at", "previous_hash", "row_hash"),
        "swing_commands_v1": ("portfolio_id", "idempotency_key", "command_kind", "payload_json", "payload_hash", "expected_portfolio_sequence", "expected_position_sequence", "expected_mark_revision", "committed_portfolio_sequence", "committed_position_sequence", "committed_mark_revision", "committed_event_sequence", "created_at", "previous_hash", "row_hash"),
        "swing_fills_v1": ("fill_id", "portfolio_id", "position_id", "symbol", "side", "quantity", "raw_price_krw", "decision_at", "fill_at", "cost_scenario", "policy_hash", "gross_commission_krw", "gross_tax_krw", "gross_slippage_krw", "base_commission_krw", "base_tax_krw", "base_slippage_krw", "stress_commission_krw", "stress_tax_krw", "stress_slippage_krw", "gross_cash_delta_krw", "net_cash_delta_krw", "command_hash", "previous_hash", "row_hash"),
        "swing_lifecycle_events_v1": ("portfolio_id", "event_sequence", "event_id", "event_type", "position_id", "symbol", "payload_json", "previous_hash", "event_hash"),
        "swing_daily_marks_v1": ("portfolio_id", "position_id", "symbol", "session_date", "revision", "mark_id", "price_krw", "quality", "source_id", "available_at", "computed_at", "supersedes_id", "payload_json", "previous_hash", "row_hash"),
        "swing_position_snapshots_v1": ("portfolio_id", "position_id", "symbol", "sequence", "quantity", "status", "cost_basis_krw", "latest_mark_id", "state_json", "previous_hash", "snapshot_hash"),
        "swing_portfolio_snapshots_v1": ("portfolio_id", "sequence", "cash_krw", "market_value_krw", "equity_krw", "receivables_krw", "liabilities_krw", "completeness", "gate", "state_json", "snapshot_json", "previous_hash", "snapshot_hash"),
        "swing_episode_events_v1": ("event_id", "portfolio_id", "payload_json"),
        "swing_episode_snapshots_v1": ("portfolio_id", "episode_id", "sequence", "payload_json"),
    }
    for table, columns in expected_columns.items():
        actual_columns = tuple(
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        )
        assert actual_columns == columns

    command_row = connection.execute(
        "SELECT portfolio_id, idempotency_key, command_kind, expected_portfolio_sequence, "
        "expected_position_sequence, expected_mark_revision, committed_portfolio_sequence, "
        "committed_position_sequence, committed_mark_revision, committed_event_sequence "
        "FROM swing_commands_v1 WHERE idempotency_key='register'"
    ).fetchone()
    assert command_row == ("candidate", "register", "REGISTER_PORTFOLIO", 0, 0, 0, 0, None, None, 0)
    fill_row = connection.execute(
        "SELECT fill_id, portfolio_id, position_id, symbol, side, quantity, raw_price_krw "
        "FROM swing_fills_v1"
    ).fetchone()
    assert fill_row == ("fill-posBUY", "candidate", "pos", "005930", "BUY", 1, 100)
    connection.close()


def test_swing_schema_trigger_catalog_is_exact(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    current = ledger(path)
    current.register_portfolio(idempotency_key="register")
    current.close()

    connection = sqlite3.connect(path)
    expected_tables = (
        "swing_schema_meta_v1", "swing_portfolios_v1",
        "swing_position_identities_v1", "swing_commands_v1", "swing_fills_v1",
        "swing_lifecycle_events_v1", "swing_daily_marks_v1",
        "swing_position_snapshots_v1", "swing_portfolio_snapshots_v1",
        "swing_episode_events_v1", "swing_episode_snapshots_v1",
    )
    expected_names = tuple(sorted(
        [
            *(f"{table}_immutable_update" for table in expected_tables),
            *(f"{table}_immutable_delete" for table in expected_tables),
            "swing_one_open_symbol_v1",
        ]
    ))
    actual = {
        name: sql
        for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        )
    }
    assert tuple(sorted(actual)) == expected_names
    for table in expected_tables:
        assert f"before update on {table}" in actual[f"{table}_immutable_update"].lower()
        assert f"before delete on {table}" in actual[f"{table}_immutable_delete"].lower()
    assert "before insert on swing_position_snapshots_v1" in actual[
        "swing_one_open_symbol_v1"
    ].lower()
    connection.close()


def test_unregistered_write_and_p3_gap(tmp_path):
    current = ledger(tmp_path / "candidate.sqlite3")
    with pytest.raises(SwingPortfolioNotRegisteredError):
        current.append_fill(SwingFillCommand(fill(), "buy", 0, 0))


def test_multi_session_revision_one_and_history_are_preserved(tmp_path):
    current = ledger(tmp_path / "candidate.sqlite3")
    current.register_portfolio(idempotency_key="register")
    current.append_fill(SwingFillCommand(fill(), "buy", 0, 0))
    first = mark()
    current.append_mark(SwingMarkCommand(first, "mark-1", 1, 1, 0))
    second = mark(datetime(2026, 8, 20), 1)
    current.append_mark(SwingMarkCommand(second, "mark-2", 2, 2, 0))
    hydrated = current.hydrate(portfolio_id="candidate")
    assert {(item[2], item[3]) for item in hydrated.verified_mark_revisions} == {
        (first.session_date, 1), (second.session_date, 1)}


def test_position_projection_tamper_fails_closed(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    current = ledger(path)
    current.register_portfolio(idempotency_key="register")
    current.append_fill(SwingFillCommand(fill(), "buy", 0, 0))
    current.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TRIGGER swing_position_snapshots_v1_immutable_update")
    connection.execute("UPDATE swing_position_snapshots_v1 SET quantity=999")
    connection.commit()
    connection.close()
    with pytest.raises((SwingIntegrityError, SwingSchemaIncompatibleError)):
        ledger(path).hydrate(portfolio_id="candidate")


def test_buy_mark_sell_reopens_with_verified_projection(tmp_path):
    path = tmp_path / "candidate.sqlite3"
    current = ledger(path)
    current.register_portfolio(idempotency_key="register")
    current.append_fill(SwingFillCommand(fill(), "buy", 0, 0))
    current.append_mark(SwingMarkCommand(mark(), "mark", 1, 1, 0))
    current.append_fill(SwingFillCommand(fill(side="SELL"), "sell", 2, 2))
    current.close()

    reopened = ledger(path)
    hydrated = reopened.hydrate(portfolio_id="candidate")
    assert hydrated.state.lots == ()
    assert hydrated.state.cash_krw == 1_000
    reopened.close()
