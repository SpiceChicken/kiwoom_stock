"""Bound, append-only candidate ledger backed by the P1 accounting reducer."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping, cast

from kiwoom_stock.application.ports import (
    SwingAccountingDivergenceError, SwingCommandKind,
    SwingCommitReceipt, SwingFillCommand, SwingHydration, SwingIdentityConflictError,
    SwingIdempotencyConflictError, SwingIntegrityError, SwingLedgerPort,
    SwingMarkCommand, SwingPersistenceError, SwingPortfolioNotRegisteredError,
    SwingTransitionConflictError, SwingCorporateActionCommand, SwingEpisodeAppendCommand,
    SwingEpisodeHydration,
)
from kiwoom_stock.core.swing_schema import GENESIS_HASH, migrate_swing_schema
from kiwoom_stock.core.swing_commands import register_portfolio_write
from kiwoom_stock.core.swing_queries import (
    fetch_daily_mark_revisions,
    fetch_latest_portfolio_snapshot,
    fetch_latest_position_sequences,
)
from kiwoom_stock.infrastructure.sqlite_write_owner import SqliteWriterOwner
from kiwoom_stock.application.swing_session import SwingSessionCoordinator
from kiwoom_stock.domain.accounting import (
    AccountingPolicy, ApplyCorporateAction, ApplyDailyMark, ApplyFill, CostScenario, Fill,
    PortfolioState, apply_fill, initial_state, reduce_portfolio,
)
from kiwoom_stock.domain.swing_contracts import (
    AdmissionEvent, AdmissionResult, ContractError, CorporateAction, EpisodeEventType,
    EpisodeRearmEvidence, EpisodeSnapshot, EpisodeState, FillTiming, Mark, MarkQuality,
    SessionMarkEvidence, reduce_episode,
)
from kiwoom_stock.domain.episodes import EPISODE_SEMANTIC_VERSION


def _wire(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {key: _wire(item) for key, item in asdict(cast(Any, value)).items()}
    if isinstance(value, Mapping):
        return {str(key): _wire(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [_wire(item) for item in value]
    return value


def _json(value: Any) -> str:
    return json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_payload(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _instant(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _policy_hash(policy: AccountingPolicy) -> str:
    return canonical_payload(policy)


def _state_json(state: PortfolioState) -> str:
    return _json(state)


class SwingLedger(SwingLedgerPort):
    def __init__(
        self,
        database_path: str | Path,
        *,
        portfolio_id: str,
        policy: AccountingPolicy,
        read_only: bool = False,
    ) -> None:
        if not isinstance(portfolio_id, str) or not portfolio_id.strip():
            raise ValueError("portfolio_id is required")
        if not isinstance(read_only, bool):
            raise TypeError("read_only must be boolean")
        self._path, self._portfolio_id, self._policy = Path(database_path), portfolio_id, policy
        self._read_only = read_only
        self._writer_owner = None
        if read_only:
            if not self._path.is_absolute() or not self._path.is_file():
                raise ValueError("read-only candidate database must be an existing absolute file")
            self._connection = sqlite3.connect(
                f"file:{self._path}?mode=ro",
                uri=True,
            )
        else:
            self._writer_owner = SqliteWriterOwner(self._path)
            try:
                self._connection = sqlite3.connect(self._path)
            except BaseException:
                self._writer_owner.close()
                raise
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys=ON")
            if not read_only:
                migrate_swing_schema(self._connection)
        except Exception:
            self._connection.close()
            if self._writer_owner is not None:
                self._writer_owner.close()
            raise

    @property
    def portfolio_id(self) -> str:
        return self._portfolio_id

    def close(self) -> None:
        try:
            self._connection.close()
        finally:
            if self._writer_owner is not None:
                self._writer_owner.close()

    def _begin(self) -> None:
        self._connection.execute("BEGIN" if self._read_only else "BEGIN IMMEDIATE")

    def _assert_writable(self) -> None:
        if self._read_only:
            raise SwingPersistenceError("read-only candidate hydration cannot write")

    def _registered(self) -> sqlite3.Row:
        row = self._connection.execute(
            "SELECT * FROM swing_portfolios_v1 WHERE portfolio_id=?",
            (self._portfolio_id,
             )).fetchone()
        if row is None:
            raise SwingPortfolioNotRegisteredError(self._portfolio_id)
        if row["policy_hash"] != _policy_hash(self._policy):
            raise SwingIntegrityError("registered policy differs")
        return cast(sqlite3.Row, row)

    def _envelope(self, kind: SwingCommandKind, key: str, expected: dict[str, int], payload: Any) -> tuple[str, str]:
        if not isinstance(key, str) or not key.strip():
            raise ValueError("idempotency_key is required")
        envelope = {
            "schema_version": "swing-command-v1",
            "command_kind": kind.value,
            "portfolio_id": self._portfolio_id,
            "expected": expected,
            "payload": _wire(payload)}
        return _json(envelope), canonical_payload(envelope)

    def _existing_command(self, key: str, digest: str) -> SwingCommitReceipt | None:
        row = self._connection.execute(
            "SELECT * FROM swing_commands_v1 WHERE portfolio_id=? AND idempotency_key=?",
            (self._portfolio_id,
             key)).fetchone()
        if row is None:
            return None
        if row["payload_hash"] != digest:
            raise SwingIdempotencyConflictError("idempotency envelope changed")
        return SwingCommitReceipt(
            self._portfolio_id,
            SwingCommandKind(
                row["command_kind"]),
            key,
            digest,
            row["committed_portfolio_sequence"],
            row["committed_position_sequence"],
            row["committed_mark_revision"],
            row["committed_event_sequence"],
            True)

    def _run(self, action: Any) -> Any:
        try:
            self._begin()
            result = action()
            self._connection.commit()
            return result
        except (SwingIntegrityError, SwingIdentityConflictError, SwingIdempotencyConflictError, SwingPortfolioNotRegisteredError, SwingTransitionConflictError, SwingAccountingDivergenceError, ValueError):
            self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise SwingPersistenceError("candidate persistence failed") from exc
        except Exception:
            self._connection.rollback()
            raise

    def register_portfolio(self, *, idempotency_key: str, expected_portfolio_sequence: int = 0) -> SwingCommitReceipt:
        self._assert_writable()
        if expected_portfolio_sequence != 0:
            raise SwingTransitionConflictError("registration high-water must be zero")
        expected = {"portfolio_sequence": 0, "position_sequence": 0, "mark_revision": 0}
        envelope, digest = self._envelope(SwingCommandKind.REGISTER_PORTFOLIO, idempotency_key, expected, {
                                          "initial_cash_krw": self._policy.initial_cash_krw, "policy": self._policy})

        def write() -> SwingCommitReceipt:
            return register_portfolio_write(
                self._connection,
                portfolio_id=self._portfolio_id,
                policy=self._policy,
                idempotency_key=idempotency_key,
                envelope=envelope,
                digest=digest,
                policy_hash=_policy_hash(self._policy),
                existing_command=self._existing_command,
            )
        return cast(SwingCommitReceipt, self._run(write))

    def append_fill(self, command: SwingFillCommand) -> SwingCommitReceipt:
        self._assert_writable()
        if not isinstance(command, SwingFillCommand) or not isinstance(command.fill, Fill):
            raise TypeError("typed SwingFillCommand is required")
        fill = command.fill
        timing = fill.timing
        if timing is None or timing.session_evidence is None:
            raise SwingTransitionConflictError("fill timing calendar evidence is required")
        try:
            SwingSessionCoordinator().validate_fill_timing(timing)
        except Exception as exc:
            raise SwingTransitionConflictError("fill timing lacks valid calendar evidence") from exc
        if fill.portfolio_id != self._portfolio_id:
            raise SwingIdentityConflictError("fill is outside bound portfolio")
        expected = {"portfolio_sequence": command.expected_portfolio_sequence,
                    "position_sequence": command.expected_position_sequence, "mark_revision": 0}
        envelope, digest = self._envelope(SwingCommandKind.APPEND_FILL, command.idempotency_key, expected, fill)

        def write() -> SwingCommitReceipt:
            self._registered()
            replay = self._existing_command(command.idempotency_key, digest)
            if replay:
                return replay
            previous = self._hydrate_state()
            ps = self._latest_portfolio_sequence()
            qs = self._latest_position_sequence(fill.position_id, fill.symbol)
            if (ps, qs) != (command.expected_portfolio_sequence, command.expected_position_sequence):
                raise SwingTransitionConflictError("fill high-water is stale")
            application = apply_fill(previous, fill, self._policy)
            reduced = reduce_portfolio(previous, (ApplyFill(fill),), self._policy, portfolio_id=self._portfolio_id)
            expected_state = replace(application.state, gate=reduced.state.gate)
            if reduced.state != expected_state or reduced.snapshot.portfolio_id != self._portfolio_id:
                raise SwingAccountingDivergenceError("P1 fill results diverged")
            self._ensure_identity(fill.portfolio_id, fill.position_id, fill.symbol)
            seq, event_seq = ps + 1, self._latest_event_sequence() + 1
            old_pos = self._latest_position_hash(fill.position_id, fill.symbol)
            old_fill = self._latest_fill_hash()
            old_event = self._latest_event_hash()
            old_port = self._latest_portfolio_hash()
            costs = application.cost_bundle
            row = (
                fill.fill_id,
                self._portfolio_id,
                fill.position_id,
                fill.symbol,
                fill.side,
                fill.quantity,
                fill.raw_price_krw,
                _instant(
                    fill.decision_at),
                _instant(
                    fill.fill_at),
                fill.cost_scenario.value,
                _policy_hash(
                    self._policy),
                costs.gross.commission_krw,
                costs.gross.tax_krw,
                costs.gross.slippage_krw,
                costs.base.commission_krw,
                costs.base.tax_krw,
                costs.base.slippage_krw,
                costs.stress.commission_krw,
                costs.stress.tax_krw,
                costs.stress.slippage_krw,
                application.gross_cash_delta_krw,
                application.net_cash_delta_krw,
                digest,
                old_fill,
                "")
            row = row[:-1] + (canonical_payload(row[:-1]),)
            self._connection.execute(
                "INSERT INTO swing_fills_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            lot = reduced.state.lot_for(fill.symbol, fill.position_id)
            oldlot = previous.lot_for(fill.symbol, fill.position_id)
            state_json = _state_json(reduced.state)
            prow = (
                self._portfolio_id,
                fill.position_id,
                fill.symbol,
                qs + 1,
                lot.quantity if lot else 0,
                "OPEN" if lot else "CLOSED",
                lot.cost_basis_krw if lot else (
                    oldlot.cost_basis_krw if oldlot else 0),
                None,
                state_json,
                old_pos,
                "")
            prow = prow[:-1] + (canonical_payload(prow[:-1]),)
            self._connection.execute("INSERT INTO swing_position_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?)", prow)
            event_payload = envelope
            erow = (
                self._portfolio_id,
                event_seq,
                f"fill:{fill.fill_id}",
                "FILL",
                fill.position_id,
                fill.symbol,
                event_payload,
                old_event,
                "")
            erow = erow[:-1] + (canonical_payload(erow[:-1]),)
            self._connection.execute("INSERT INTO swing_lifecycle_events_v1 VALUES (?,?,?,?,?,?,?,?,?)", erow)
            snap_json = _json(reduced.snapshot)
            srow = (
                self._portfolio_id,
                seq,
                reduced.snapshot.cash_krw,
                reduced.snapshot.market_value_krw,
                reduced.snapshot.equity_krw,
                reduced.snapshot.receivables_krw,
                reduced.snapshot.liabilities_krw,
                reduced.snapshot.completeness.value,
                reduced.snapshot.gate.value if reduced.snapshot.gate else None,
                state_json,
                snap_json,
                old_port,
                "")
            srow = srow[:-1] + (canonical_payload(srow[:-1]),)
            self._connection.execute(
                "INSERT INTO swing_portfolio_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", srow)
            self._insert_command(command.idempotency_key, SwingCommandKind.APPEND_FILL,
                                 envelope, digest, expected, seq, qs + 1, None, event_seq)
            return SwingCommitReceipt(
                self._portfolio_id,
                SwingCommandKind.APPEND_FILL,
                command.idempotency_key,
                digest,
                seq,
                qs + 1,
                None,
                event_seq)
        return cast(SwingCommitReceipt, self._run(write))

    def append_mark(self, command: SwingMarkCommand) -> SwingCommitReceipt:
        self._assert_writable()
        if not isinstance(command, SwingMarkCommand) or not isinstance(command.mark, Mark):
            raise TypeError("typed SwingMarkCommand is required")
        mark = command.mark
        if mark.portfolio_id != self._portfolio_id:
            raise SwingIdentityConflictError("mark is outside bound portfolio")
        if command.current_session is None or command.session_evidence is None:
            raise SwingTransitionConflictError("current session and mark evidence are required")
        if mark.session_evidence != command.session_evidence:
            raise SwingTransitionConflictError("mark evidence is not bound to command evidence")
        assessment = SwingSessionCoordinator(
            max_stale_sessions=0).assess_mark(
            mark,
            current_session=command.current_session,
            evidence=command.session_evidence)
        if not assessment.nav_allowed:
            raise SwingTransitionConflictError("mark is stale or lacks valid session evidence")
        expected = {
            "portfolio_sequence": command.expected_portfolio_sequence,
            "position_sequence": command.expected_position_sequence,
            "mark_revision": command.expected_mark_revision}
        envelope, digest = self._envelope(SwingCommandKind.APPEND_MARK, command.idempotency_key, expected, {
                                          "mark": mark, "current_session": command.current_session, "session_evidence": command.session_evidence})

        def write() -> SwingCommitReceipt:
            self._registered()
            replay = self._existing_command(command.idempotency_key, digest)
            if replay:
                return replay
            previous = self._hydrate_state()
            lot = previous.lot_for(mark.symbol, mark.position_id)
            ps = self._latest_portfolio_sequence()
            qs = self._latest_position_sequence(mark.position_id, mark.symbol)
            if lot is None:
                raise SwingTransitionConflictError("mark identity is stale")
            if (ps, qs) != (command.expected_portfolio_sequence, command.expected_position_sequence):
                raise SwingTransitionConflictError("mark high-water is stale")
            oldrow = self._connection.execute(
                "SELECT * FROM swing_daily_marks_v1 WHERE portfolio_id=? AND position_id=? AND symbol=? AND session_date=? ORDER BY revision DESC LIMIT 1",
                (self._portfolio_id,
                 mark.position_id,
                 mark.symbol,
                 mark.session_date.isoformat())).fetchone()
            current = oldrow["revision"] if oldrow else 0
            if current != command.expected_mark_revision or mark.revision != current + \
                    1 or (oldrow and mark.supersedes_id != oldrow["mark_id"]):
                raise SwingTransitionConflictError("mark predecessor or revision is not exact")
            # P1 stores the latest mark per lot; remove another session before reducing this session's revision chain.
            base = PortfolioState(
                previous.portfolio_id,
                previous.cash_krw,
                previous.lots,
                previous.external_flow_krw,
                previous.gate,
                tuple(
                    x for x in previous.marks if not (
                        x.position_id == mark.position_id and x.symbol == mark.symbol)))
            if oldrow:
                old_payload = json.loads(oldrow["payload_json"])["payload"]
                oldmark = self._mark_from_payload(dict(old_payload.get("mark", old_payload)))
                base = PortfolioState(base.portfolio_id, base.cash_krw, base.lots,
                                      base.external_flow_krw, base.gate, base.marks + (oldmark,))
            reduced = reduce_portfolio(base, (ApplyDailyMark(mark),), self._policy, portfolio_id=self._portfolio_id)
            seq, event_seq = ps + 1, self._latest_event_sequence() + 1
            prev_mark = oldrow["row_hash"] if oldrow else GENESIS_HASH
            row = (
                self._portfolio_id,
                mark.position_id,
                mark.symbol,
                mark.session_date.isoformat(),
                mark.revision,
                mark.mark_id,
                mark.price_krw,
                mark.quality.value,
                mark.source_id,
                _instant(
                    mark.available_at),
                _instant(
                    mark.computed_at),
                mark.supersedes_id,
                envelope,
                prev_mark,
                "")
            row = row[:-1] + (canonical_payload(row[:-1]),)
            self._connection.execute("INSERT INTO swing_daily_marks_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            state_json = _state_json(reduced.state)
            lot = reduced.state.lot_for(mark.symbol, mark.position_id)
            if lot is None:
                raise SwingAccountingDivergenceError("mark closed its active lot")
            ph = self._latest_position_hash(mark.position_id, mark.symbol)
            prow = (
                self._portfolio_id,
                mark.position_id,
                mark.symbol,
                qs + 1,
                lot.quantity,
                "OPEN",
                lot.cost_basis_krw,
                mark.mark_id,
                state_json,
                ph,
                "")
            prow = prow[:-1] + (canonical_payload(prow[:-1]),)
            self._connection.execute("INSERT INTO swing_position_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?)", prow)
            erow = (
                self._portfolio_id,
                event_seq,
                f"mark:{mark.mark_id}",
                "MARK",
                mark.position_id,
                mark.symbol,
                envelope,
                self._latest_event_hash(),
                "")
            erow = erow[:-1] + (canonical_payload(erow[:-1]),)
            self._connection.execute("INSERT INTO swing_lifecycle_events_v1 VALUES (?,?,?,?,?,?,?,?,?)", erow)
            snap_json = _json(reduced.snapshot)
            srow = (
                self._portfolio_id,
                seq,
                reduced.snapshot.cash_krw,
                reduced.snapshot.market_value_krw,
                reduced.snapshot.equity_krw,
                reduced.snapshot.receivables_krw,
                reduced.snapshot.liabilities_krw,
                reduced.snapshot.completeness.value,
                reduced.snapshot.gate.value if reduced.snapshot.gate else None,
                state_json,
                snap_json,
                self._latest_portfolio_hash(),
                "")
            srow = srow[:-1] + (canonical_payload(srow[:-1]),)
            self._connection.execute(
                "INSERT INTO swing_portfolio_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", srow)
            self._insert_command(
                command.idempotency_key,
                SwingCommandKind.APPEND_MARK,
                envelope,
                digest,
                expected,
                seq,
                qs + 1,
                mark.revision,
                event_seq)
            return SwingCommitReceipt(
                self._portfolio_id,
                SwingCommandKind.APPEND_MARK,
                command.idempotency_key,
                digest,
                seq,
                qs + 1,
                mark.revision,
                event_seq)
        return cast(SwingCommitReceipt, self._run(write))

    def append_corporate_action(self, command: SwingCorporateActionCommand) -> SwingCommitReceipt:
        self._assert_writable()
        if not isinstance(command, SwingCorporateActionCommand) or not isinstance(command.action, CorporateAction):
            raise TypeError("typed SwingCorporateActionCommand is required")
        action = command.action
        if action.portfolio_id != self._portfolio_id:
            raise SwingIdentityConflictError("corporate action is outside bound portfolio")
        expected = {"portfolio_sequence": command.expected_portfolio_sequence,
                    "position_sequence": command.expected_position_sequence, "mark_revision": 0}
        payload = {"action": action, "decision_at": command.decision_at, "session_date": command.effective_session}
        envelope, digest = self._envelope(SwingCommandKind.APPEND_CORPORATE_ACTION,
                                          command.idempotency_key, expected, payload)

        def write() -> SwingCommitReceipt:
            self._registered()
            replay = self._existing_command(command.idempotency_key, digest)
            if replay:
                return replay
            previous = self._hydrate_state()
            lot = previous.lot_for(action.symbol, action.position_id)
            ps, qs = self._latest_portfolio_sequence(), self._latest_position_sequence(action.position_id, action.symbol)
            if lot is None or (ps, qs) != (command.expected_portfolio_sequence, command.expected_position_sequence):
                raise SwingTransitionConflictError("corporate-action high-water or identity is stale")
            reduced = reduce_portfolio(
                previous,
                (ApplyCorporateAction(
                    action,
                    command.decision_at,
                    command.effective_session),
                 ),
                self._policy,
                portfolio_id=self._portfolio_id)
            seq, event_seq = ps + 1, self._latest_event_sequence() + 1
            erow = (
                self._portfolio_id,
                event_seq,
                f"action:{action.action_id}",
                "CORPORATE_ACTION",
                action.position_id,
                action.symbol,
                envelope,
                self._latest_event_hash(),
                "")
            erow = erow[:-1] + (canonical_payload(erow[:-1]),)
            self._connection.execute("INSERT INTO swing_lifecycle_events_v1 VALUES (?,?,?,?,?,?,?,?,?)", erow)
            current_lot = reduced.state.lot_for(action.symbol, action.position_id)
            if current_lot is None:
                raise SwingAccountingDivergenceError("corporate action closed active lot")
            state_json = _state_json(reduced.state)
            prow = (
                self._portfolio_id,
                action.position_id,
                action.symbol,
                qs + 1,
                current_lot.quantity,
                "OPEN",
                current_lot.cost_basis_krw,
                None,
                state_json,
                self._latest_position_hash(
                    action.position_id,
                    action.symbol),
                "")
            prow = prow[:-1] + (canonical_payload(prow[:-1]),)
            self._connection.execute("INSERT INTO swing_position_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?)", prow)
            snap_json = _json(reduced.snapshot)
            srow = (
                self._portfolio_id,
                seq,
                reduced.snapshot.cash_krw,
                reduced.snapshot.market_value_krw,
                reduced.snapshot.equity_krw,
                reduced.snapshot.receivables_krw,
                reduced.snapshot.liabilities_krw,
                reduced.snapshot.completeness.value,
                reduced.snapshot.gate.value if reduced.snapshot.gate else None,
                state_json,
                snap_json,
                self._latest_portfolio_hash(),
                "")
            srow = srow[:-1] + (canonical_payload(srow[:-1]),)
            self._connection.execute(
                "INSERT INTO swing_portfolio_snapshots_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", srow)
            self._insert_command(command.idempotency_key, SwingCommandKind.APPEND_CORPORATE_ACTION,
                                 envelope, digest, expected, seq, qs + 1, None, event_seq)
            return SwingCommitReceipt(
                self._portfolio_id,
                SwingCommandKind.APPEND_CORPORATE_ACTION,
                command.idempotency_key,
                digest,
                seq,
                qs + 1,
                None,
                event_seq)
        return cast(SwingCommitReceipt, self._run(write))

    def _insert_command(self, key: str, kind: SwingCommandKind, envelope: str, digest: str,
                        expected: dict[str, int], ps: int, qs: int, mr: int | None, event: int) -> None:
        prev = self._latest_command_hash()
        row = (
            self._portfolio_id,
            key,
            kind.value,
            envelope,
            digest,
            expected["portfolio_sequence"],
            expected["position_sequence"],
            expected["mark_revision"],
            ps,
            qs,
            mr,
            event,
            _instant(
                datetime.now(
                    timezone.utc)),
            prev,
            "")
        row = row[:-1] + (canonical_payload(row[:-1]),)
        self._connection.execute("INSERT INTO swing_commands_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)

    def _ensure_identity(self, portfolio: str, position: str, symbol: str) -> None:
        row = self._connection.execute(
            "SELECT symbol FROM swing_position_identities_v1 WHERE portfolio_id=? AND position_id=?",
            (portfolio,
             position)).fetchone()
        if row and row[0] != symbol:
            raise SwingIdentityConflictError("position is bound to another symbol")
        if row is None:
            now = _instant(datetime.now(timezone.utc))
            base = (portfolio, position, symbol, now, GENESIS_HASH)
            self._connection.execute("INSERT INTO swing_position_identities_v1 VALUES (?,?,?,?,?,?)",
                                     base + (canonical_payload(base),))

    def _max(self, table: str, column: str, where: str = "", args: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            self._connection.execute(
                f"SELECT * FROM {table} {where} ORDER BY {column} DESC LIMIT 1",
                args).fetchone())

    def _latest_portfolio_sequence(self) -> int:
        row = self._max(
            "swing_portfolio_snapshots_v1", "sequence", "WHERE portfolio_id=?", (self._portfolio_id,)
        )
        return int(row["sequence"]) if row else 0

    def _latest_position_sequence(self, p: str, s: str) -> int:
        row = self._max(
            "swing_position_snapshots_v1",
            "sequence",
            "WHERE portfolio_id=? AND position_id=? AND symbol=?",
            (self._portfolio_id, p, s),
        )
        return int(row["sequence"]) if row else 0

    def _latest_event_sequence(self) -> int:
        row = self._max(
            "swing_lifecycle_events_v1",
            "event_sequence",
            "WHERE portfolio_id=?",
            (self._portfolio_id,),
        )
        return int(row["event_sequence"]) if row else 0

    def _latest_hash(self, t: str, c: str) -> str:
        row = self._max(t, "rowid", "WHERE portfolio_id=?", (self._portfolio_id,))
        return row[c] if row else GENESIS_HASH

    def _latest_portfolio_hash(self) -> str:
        return self._latest_hash("swing_portfolio_snapshots_v1", "snapshot_hash")

    def _latest_event_hash(self) -> str:
        return self._latest_hash("swing_lifecycle_events_v1", "event_hash")

    def _latest_command_hash(self) -> str:
        return self._latest_hash("swing_commands_v1", "row_hash")

    def _latest_fill_hash(self) -> str:
        return self._latest_hash("swing_fills_v1", "row_hash")

    def _latest_position_hash(self, p: str, s: str):
        if self._latest_position_sequence(p, s) == 0:
            return GENESIS_HASH
        row = self._connection.execute(
            "SELECT snapshot_hash FROM swing_position_snapshots_v1 WHERE portfolio_id=? AND position_id=? AND symbol=? ORDER BY sequence DESC LIMIT 1",
            (self._portfolio_id, p, s),
        ).fetchone()
        return row[0] if row else GENESIS_HASH

    def _hydrate_state(self) -> PortfolioState:
        self._verify_history()
        row = self._connection.execute(
            "SELECT state_json FROM swing_portfolio_snapshots_v1 WHERE portfolio_id=? ORDER BY sequence DESC LIMIT 1",
            (self._portfolio_id,
             )).fetchone()
        return initial_state(self._policy, self._portfolio_id) if row is None else self._state_from_json(row[0])

    def _verify_history(self) -> None:
        self._registered()
        for table, hashcol in (("swing_portfolios_v1", "row_hash"), ("swing_position_identities_v1", "row_hash")):
            for row in self._connection.execute(f"SELECT * FROM {table} WHERE portfolio_id=?", (self._portfolio_id,)):
                if row[hashcol] != canonical_payload(tuple(row[x] for x in row.keys() if x != hashcol)):
                    raise SwingIntegrityError(f"{table} row hash mismatch")
        for table, seq, hashcol, prevcol in (("swing_portfolio_snapshots_v1", "sequence", "snapshot_hash", "previous_hash"), (
                "swing_lifecycle_events_v1", "event_sequence", "event_hash", "previous_hash")):
            prev = GENESIS_HASH
            rows = self._connection.execute(
                f"SELECT * FROM {table} WHERE portfolio_id=? ORDER BY {seq}", (self._portfolio_id,)).fetchall()
            for expected, row in enumerate(rows, 1):
                if row[seq] != expected or row[prevcol] != prev:
                    raise SwingIntegrityError(f"{table} predecessor/high-water mismatch")
                if table.startswith("swing_portfolio"):
                    got = canonical_payload(tuple(row[x] for x in row.keys() if x != hashcol))
                else:
                    got = canonical_payload(tuple(row[x] for x in row.keys() if x != hashcol))
                if row[hashcol] != got:
                    raise SwingIntegrityError(f"{table} hash mismatch")
                if table == "swing_lifecycle_events_v1":
                    command = self._connection.execute(
                        "SELECT payload_json FROM swing_commands_v1 WHERE committed_event_sequence=?", (row[seq],)).fetchone()
                    if command is None or command[0] != row["payload_json"]:
                        raise SwingIntegrityError("event tail is not bound to its command")
                prev = row[hashcol]
        for table, keycols, hashcol, prevcol in (("swing_commands_v1", (), "row_hash", "previous_hash"), ("swing_fills_v1", (), "row_hash", "previous_hash"), ("swing_position_snapshots_v1", (
                "position_id", "symbol"), "snapshot_hash", "previous_hash"), ("swing_daily_marks_v1", ("position_id", "symbol", "session_date"), "row_hash", "previous_hash")):
            groups: dict[tuple[Any, ...], str] = {}
            global_prev = GENESIS_HASH
            rows = self._connection.execute(
                f"SELECT * FROM {table} WHERE portfolio_id=? ORDER BY rowid", (self._portfolio_id,)).fetchall()
            for row in rows:
                key = tuple(row[x] for x in keycols)
                prev = global_prev if not keycols else groups.get(key, GENESIS_HASH)
                if row[prevcol] != prev or row[hashcol] != canonical_payload(
                        tuple(row[x] for x in row.keys() if x != hashcol)):
                    raise SwingIntegrityError(f"{table} row integrity mismatch")
                if keycols:
                    groups[key] = row[hashcol]
                else:
                    global_prev = row[hashcol]
        self._verify_projection_replay()
        self._verify_all_episode_history()

    @staticmethod
    def _episode_snapshot_payload(snapshot: EpisodeSnapshot) -> dict[str, Any]:
        return {
            "state": snapshot.state.value,
            "semantic_version": snapshot.semantic_version,
            "consumed_event_ids": sorted(snapshot.consumed_event_ids),
            "admission_results": [
                [event_id, result.value] for event_id, result in snapshot.admission_results
            ],
        }

    @classmethod
    def _episode_snapshot_from_payload(cls, payload: Mapping[str, Any]) -> EpisodeSnapshot:
        try:
            return EpisodeSnapshot(
                EpisodeState(payload["state"]),
                str(payload["semantic_version"]),
                frozenset(payload.get("consumed_event_ids", [])),
                tuple(
                    (str(item[0]), AdmissionResult(item[1]))
                    for item in payload.get("admission_results", [])
                ),
            )
        except Exception as exc:
            raise SwingIntegrityError("episode snapshot cannot be reconstructed") from exc

    @staticmethod
    def _episode_event_from_payload(
        payload: Mapping[str, Any],
    ) -> tuple[AdmissionEvent, EpisodeRearmEvidence | None]:
        try:
            raw_event = dict(payload["event"])
            raw_event["result"] = AdmissionResult(raw_event["result"])
            raw_event["event_type"] = EpisodeEventType(raw_event["event_type"])
            event = AdmissionEvent(**raw_event)
            raw_evidence = payload.get("rearm_evidence")
            evidence = EpisodeRearmEvidence(**raw_evidence) if isinstance(raw_evidence, Mapping) else None
            return event, evidence
        except Exception as exc:
            raise SwingIntegrityError("episode event cannot be reconstructed") from exc

    @staticmethod
    def _episode_command_payload(command: SwingEpisodeAppendCommand) -> dict[str, Any]:
        return {
            "episode_id": command.episode_id,
            "event": command.event,
            "expected_episode_sequence": command.expected_episode_sequence,
            "rearm_evidence": command.rearm_evidence,
            "current_session": command.current_session,
            "previous_session": command.previous_session,
        }

    def _read_episode(self, episode_id: str) -> SwingEpisodeHydration:
        event_rows = self._connection.execute(
            "SELECT event_id, payload_json FROM swing_episode_events_v1 "
            "WHERE portfolio_id=? ORDER BY rowid",
            (self._portfolio_id,),
        ).fetchall()
        selected: list[sqlite3.Row] = []
        for row in event_rows:
            try:
                payload = json.loads(row["payload_json"])
            except Exception as exc:
                raise SwingIntegrityError("episode event payload is not JSON") from exc
            if payload.get("episode_id") == episode_id:
                selected.append(row)

        snapshot = EpisodeSnapshot(EpisodeState.ARMED, EPISODE_SEMANTIC_VERSION)
        state_payloads: list[str] = []
        event_ids: list[str] = []
        for expected_sequence, row in enumerate(selected, 1):
            try:
                payload = json.loads(row["payload_json"])
                if payload.get("sequence") != expected_sequence:
                    raise ValueError("episode sequence is not contiguous")
                if payload.get("event_id") != row["event_id"]:
                    raise ValueError("episode event identity differs from payload")
                event, evidence = self._episode_event_from_payload(payload)
                if event.episode_id != episode_id:
                    raise ValueError("episode event identity differs from query")
                if payload.get("expected_episode_sequence") != expected_sequence - 1:
                    raise ValueError("episode expected sequence is not bound")
                command_hash = payload.get("command_hash")
                command_payload = {
                    "episode_id": episode_id,
                    "event": event,
                    "expected_episode_sequence": expected_sequence - 1,
                    "rearm_evidence": evidence,
                    "current_session": self._date_from_wire(payload["current_session"])
                    if payload.get("current_session") is not None else None,
                    "previous_session": self._date_from_wire(payload["previous_session"])
                    if payload.get("previous_session") is not None else None,
                }
                if command_hash != canonical_payload(command_payload):
                    raise ValueError("episode command hash mismatch")
                next_snapshot = reduce_episode(
                    snapshot,
                    event,
                    current_version=EPISODE_SEMANTIC_VERSION,
                    evidence=evidence,
                )
                if not isinstance(next_snapshot, EpisodeSnapshot):
                    raise ValueError("episode reducer did not return a snapshot")
                if self._episode_snapshot_payload(next_snapshot) != payload.get("state_after"):
                    raise ValueError("episode state projection diverged from event replay")
                snapshot = next_snapshot
                state_payloads.append(_json(self._episode_snapshot_payload(next_snapshot)))
                event_ids.append(row["event_id"])
            except SwingIntegrityError:
                raise
            except Exception as exc:
                raise SwingIntegrityError("episode replay failed closed") from exc

        snapshot_rows = self._connection.execute(
            "SELECT sequence, payload_json FROM swing_episode_snapshots_v1 "
            "WHERE portfolio_id=? AND episode_id=? ORDER BY sequence",
            (self._portfolio_id, episode_id),
        ).fetchall()
        if len(snapshot_rows) != len(selected):
            raise SwingIntegrityError("episode event/snapshot cardinality differs")
        for expected_sequence, row in enumerate(snapshot_rows, 1):
            if row["sequence"] != expected_sequence or row["payload_json"] != state_payloads[expected_sequence - 1]:
                raise SwingIntegrityError("episode snapshot projection differs from event replay")
        return SwingEpisodeHydration(
            episode_id,
            snapshot,
            len(selected),
            tuple(event_ids),
            canonical_payload(tuple(row["payload_json"] for row in selected)),
        )

    def _verify_all_episode_history(self) -> None:
        episode_ids: set[str] = set()
        for row in self._connection.execute(
            "SELECT payload_json FROM swing_episode_events_v1 WHERE portfolio_id=?",
            (self._portfolio_id,),
        ):
            try:
                payload = json.loads(row[0])
                episode_id = payload.get("episode_id")
            except Exception as exc:
                raise SwingIntegrityError("episode event payload is not JSON") from exc
            if not isinstance(episode_id, str) or not episode_id.strip():
                raise SwingIntegrityError("episode event lacks episode identity")
            episode_ids.add(episode_id)
        episode_ids.update(
            row[0]
            for row in self._connection.execute(
                "SELECT DISTINCT episode_id FROM swing_episode_snapshots_v1 WHERE portfolio_id=?",
                (self._portfolio_id,),
            )
        )
        for episode_id in episode_ids:
            self._read_episode(episode_id)

    @staticmethod
    def _datetime_from_wire(value: str) -> datetime:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise SwingIntegrityError("candidate event contains an invalid datetime") from exc

    @staticmethod
    def _date_from_wire(value: str) -> date:
        try:
            return date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise SwingIntegrityError("candidate event contains an invalid date") from exc

    def _fill_from_payload(self, payload: Mapping[str, Any]) -> Fill:
        try:
            data = dict(payload)
            timing = dict(data["timing"])
            for key in ("decision_at", "fill_at"):
                data[key] = self._datetime_from_wire(data[key])
            for key in ("decision_at", "fill_at", "bar_open_at", "previous_completed_at"):
                if timing.get(key) is not None:
                    timing[key] = self._datetime_from_wire(timing[key])
            for key in ("decision_session", "eligible_session", "previous_completed_session"):
                if timing.get(key) is not None:
                    timing[key] = self._date_from_wire(timing[key])
            if isinstance(timing.get("session_evidence"), dict):
                from kiwoom_stock.domain.swing_contracts import FillTimingEvidence
                timing["session_evidence"] = FillTimingEvidence(**timing["session_evidence"])
            data["timing"] = FillTiming(**timing)
            data["cost_scenario"] = CostScenario(data["cost_scenario"])
            return Fill(**data)
        except SwingIntegrityError:
            raise
        except Exception as exc:
            raise SwingIntegrityError("candidate fill event cannot be reconstructed") from exc

    def _event_envelope(self, raw: str) -> tuple[SwingCommandKind, Mapping[str, Any]]:
        try:
            envelope = json.loads(raw)
            kind = SwingCommandKind(envelope["command_kind"])
            if envelope["schema_version"] != "swing-command-v1":
                raise ValueError("unsupported command schema")
            if envelope["portfolio_id"] != self._portfolio_id:
                raise ValueError("event portfolio differs from bound portfolio")
            payload = envelope["payload"]
            if not isinstance(payload, Mapping):
                raise ValueError("event payload must be an object")
            return kind, envelope
        except Exception as exc:
            raise SwingIntegrityError("candidate event envelope cannot be reconstructed") from exc

    def _verify_projection_replay(self) -> None:
        """Replay the append-only event stream and compare every projection row."""
        state = initial_state(self._policy, self._portfolio_id)
        position_sequences: dict[tuple[str, str], int] = {}
        events = self._connection.execute(
            "SELECT * FROM swing_lifecycle_events_v1 WHERE portfolio_id=? ORDER BY event_sequence",
            (self._portfolio_id,),
        ).fetchall()
        event_by_sequence = {int(row["event_sequence"]): row for row in events}
        commands = self._connection.execute(
            "SELECT * FROM swing_commands_v1 WHERE portfolio_id=? ORDER BY rowid", (self._portfolio_id,)
        ).fetchall()
        if len(commands) != len(events) + 1:
            raise SwingIntegrityError("orphan command or lifecycle event detected")
        for command in commands:
            sequence = int(command["committed_event_sequence"])
            if sequence == 0:
                if command["command_kind"] != SwingCommandKind.REGISTER_PORTFOLIO.value:
                    raise SwingIntegrityError("non-registration command has no lifecycle event")
                continue
            event = event_by_sequence.get(sequence)
            if event is None or event["payload_json"] != command["payload_json"]:
                raise SwingIntegrityError("command is orphaned from lifecycle event")
            expected_type = {
                SwingCommandKind.APPEND_FILL.value: "FILL",
                SwingCommandKind.APPEND_MARK.value: "MARK",
                SwingCommandKind.APPEND_CORPORATE_ACTION.value: "CORPORATE_ACTION"}.get(
                command["command_kind"])
            if expected_type != event["event_type"]:
                raise SwingIntegrityError("command and lifecycle event types differ")
        counts = {
            "FILL": self._connection.execute(
                "SELECT COUNT(*) FROM swing_fills_v1 WHERE portfolio_id=?",
                (self._portfolio_id,
                 )).fetchone()[0],
            "MARK": self._connection.execute(
                "SELECT COUNT(*) FROM swing_daily_marks_v1 WHERE portfolio_id=?",
                (self._portfolio_id,
                 )).fetchone()[0],
        }
        for kind, count in counts.items():
            if count != sum(1 for event in events if event["event_type"] == kind):
                raise SwingIntegrityError(f"orphan {kind.lower()} projection detected")
        identity_rows = {(row["position_id"], row["symbol"]) for row in self._connection.execute(
            "SELECT position_id, symbol FROM swing_position_identities_v1 WHERE portfolio_id=?", (self._portfolio_id,))}
        event_identities = {(row["position_id"], row["symbol"]) for row in events}
        if identity_rows != event_identities:
            raise SwingIntegrityError("orphan position identity detected")
        last_event_at: datetime | None = None
        for event in events:
            kind, envelope = self._event_envelope(event["payload_json"])
            payload = envelope["payload"]
            mark_payload = payload.get("mark", payload) if isinstance(payload, Mapping) else payload
            raw_event_at = payload.get("fill_at") if kind is SwingCommandKind.APPEND_FILL else mark_payload.get(
                "computed_at") if kind is SwingCommandKind.APPEND_MARK else payload.get("decision_at")
            if not isinstance(raw_event_at, str):
                raise SwingIntegrityError("lifecycle event lacks temporal evidence")
            event_at = self._datetime_from_wire(raw_event_at)
            if last_event_at is not None and event_at < last_event_at:
                raise SwingIntegrityError("lifecycle event temporal ordering is invalid")
            last_event_at = event_at
            previous = state
            latest_mark_id: str | None = None
            if kind is SwingCommandKind.APPEND_FILL:
                fill = self._fill_from_payload(payload)
                if event["event_id"] != f"fill:{fill.fill_id}":
                    raise SwingIntegrityError("fill event identity is not bound to its payload")
                stored = self._connection.execute(
                    "SELECT * FROM swing_fills_v1 WHERE fill_id=?", (fill.fill_id,)
                ).fetchone()
                if stored is None or stored["command_hash"] != canonical_payload(envelope):
                    raise SwingIntegrityError("fill projection is not bound to its event")
                if tuple(
                    stored[key] for key in (
                        "portfolio_id",
                        "position_id",
                        "symbol",
                        "side",
                        "quantity",
                        "raw_price_krw")) != (
                    fill.portfolio_id,
                    fill.position_id,
                    fill.symbol,
                    fill.side,
                    fill.quantity,
                        fill.raw_price_krw):
                    raise SwingIntegrityError("fill projection identity differs from its event")
                if tuple(
                    stored[key] for key in (
                        "decision_at",
                        "fill_at",
                        "cost_scenario")) != (
                    _instant(
                        fill.decision_at),
                    _instant(
                        fill.fill_at),
                        fill.cost_scenario.value):
                    raise SwingIntegrityError("fill timing or selected cost scenario differs from its event")
                application = apply_fill(previous, fill, self._policy)
                reduced = reduce_portfolio(previous, (ApplyFill(fill),), self._policy, portfolio_id=self._portfolio_id)
                if stored["policy_hash"] != _policy_hash(self._policy):
                    raise SwingIntegrityError("fill policy binding differs from the registered policy")
                expected_costs = application.cost_bundle
                for prefix, view in (("gross", expected_costs.gross), ("base", expected_costs.base),
                                     ("stress", expected_costs.stress)):
                    for field in ("commission_krw", "tax_krw", "slippage_krw"):
                        if stored[f"{prefix}_{field}"] != getattr(view, field):
                            raise SwingIntegrityError("fill cost bundle differs from its event")
                selected = expected_costs.gross if fill.cost_scenario is CostScenario.GROSS else expected_costs.base if fill.cost_scenario is CostScenario.BASE else expected_costs.stress
                if stored["gross_cash_delta_krw"] != application.gross_cash_delta_krw or stored["net_cash_delta_krw"] != application.net_cash_delta_krw:
                    raise SwingIntegrityError("fill cash projection differs from the reducer")
                if selected.total_krw != application.costs.total_krw or application.costs.scenario is not fill.cost_scenario:
                    raise SwingIntegrityError("selected fill cost view differs from its scenario")
            elif kind is SwingCommandKind.APPEND_MARK:
                mark_payload = payload.get("mark", payload)
                mark = self._mark_from_payload(dict(mark_payload))
                if payload.get("current_session") is not None:
                    current_session = self._date_from_wire(payload["current_session"])
                    if mark.session_evidence is None:
                        raise SwingIntegrityError("mark command session evidence is missing")
                    assessment = SwingSessionCoordinator(max_stale_sessions=0).assess_mark(
                        mark, current_session=current_session, evidence=mark.session_evidence)
                    if not assessment.nav_allowed:
                        raise SwingIntegrityError("replayed mark is stale or session-invalid")
                latest_mark_id = mark.mark_id
                previous_mark = previous.mark_for(mark.symbol, mark.position_id)
                if previous_mark is not None and mark.session_date < previous_mark.session_date:
                    raise SwingIntegrityError("mark session ordering is invalid")
                if event["event_id"] != f"mark:{mark.mark_id}":
                    raise SwingIntegrityError("mark event identity is not bound to its payload")
                stored = self._connection.execute(
                    "SELECT * FROM swing_daily_marks_v1 WHERE mark_id=?", (mark.mark_id,)
                ).fetchone()
                if stored is None or stored["payload_json"] != event["payload_json"]:
                    raise SwingIntegrityError("mark projection is not bound to its event")
                if tuple(
                    stored[key] for key in (
                        "portfolio_id",
                        "position_id",
                        "symbol",
                        "session_date",
                        "revision",
                        "price_krw",
                        "quality",
                        "source_id")) != (
                    mark.portfolio_id,
                    mark.position_id,
                    mark.symbol,
                    mark.session_date.isoformat(),
                    mark.revision,
                    mark.price_krw,
                    mark.quality.value,
                        mark.source_id):
                    raise SwingIntegrityError("mark projection identity differs from its event")
                marks = tuple(
                    item for item in previous.marks
                    if not (item.position_id == mark.position_id and item.symbol == mark.symbol)
                )
                if mark.supersedes_id is not None:
                    old = self._connection.execute(
                        "SELECT payload_json FROM swing_daily_marks_v1 WHERE mark_id=?",
                        (mark.supersedes_id,),
                    ).fetchone()
                    if old is None:
                        raise SwingIntegrityError("mark predecessor is missing from the projection")
                    old_kind, old_envelope = self._event_envelope(old["payload_json"])
                    if old_kind is not SwingCommandKind.APPEND_MARK:
                        raise SwingIntegrityError("mark predecessor is not a mark event")
                    old_payload = old_envelope["payload"]
                    marks += (self._mark_from_payload(dict(old_payload.get("mark", old_payload))),)
                replay_base = replace(previous, marks=marks)
                reduced = reduce_portfolio(replay_base, (ApplyDailyMark(mark),),
                                           self._policy, portfolio_id=self._portfolio_id)
            elif kind is SwingCommandKind.APPEND_CORPORATE_ACTION:
                data = dict(payload)
                raw = dict(data["action"])
                raw["effective_session"] = self._date_from_wire(raw["effective_session"])
                if raw.get("available_at") is not None:
                    raw["available_at"] = self._datetime_from_wire(raw["available_at"])
                from kiwoom_stock.domain.swing_contracts import CorporateActionKind
                raw["kind"] = CorporateActionKind(raw["kind"])
                action = CorporateAction(**raw)
                if event["event_id"] != f"action:{action.action_id}":
                    raise SwingIntegrityError("corporate-action event identity is not bound to its payload")
                if (event["position_id"], event["symbol"]) != (action.position_id, action.symbol):
                    raise SwingIntegrityError("corporate-action event identity differs from its payload")
                reduced = reduce_portfolio(
                    previous, (ApplyCorporateAction(
                        action, self._datetime_from_wire(
                            data["decision_at"]), self._date_from_wire(
                            data["session_date"])),), self._policy, portfolio_id=self._portfolio_id)
            else:
                raise SwingIntegrityError("unsupported lifecycle event kind")
            state = reduced.state
            sequence = int(event["event_sequence"])
            portfolio_snapshot = self._connection.execute(
                "SELECT * FROM swing_portfolio_snapshots_v1 WHERE portfolio_id=? AND sequence=?",
                (self._portfolio_id, sequence),
            ).fetchone()
            if portfolio_snapshot is None:
                raise SwingIntegrityError("portfolio snapshot is missing from the event projection")
            if portfolio_snapshot["state_json"] != _state_json(
                    state) or portfolio_snapshot["snapshot_json"] != _json(reduced.snapshot):
                raise SwingIntegrityError("portfolio projection cannot be reproduced from events")
            key = (event["position_id"], event["symbol"])
            position_sequences[key] = position_sequences.get(key, 0) + 1
            position_snapshot = self._connection.execute(
                "SELECT * FROM swing_position_snapshots_v1 WHERE portfolio_id=? AND position_id=? AND symbol=? AND sequence=?",
                (self._portfolio_id, key[0], key[1], position_sequences[key]),
            ).fetchone()
            if position_snapshot is None:
                raise SwingIntegrityError("position snapshot is missing from the event projection")
            lot = state.lot_for(key[1], key[0])
            prior_lot = previous.lot_for(key[1], key[0])
            expected_quantity = lot.quantity if lot is not None else 0
            expected_status = "OPEN" if lot is not None else "CLOSED"
            expected_cost_basis = lot.cost_basis_krw if lot is not None else (
                prior_lot.cost_basis_krw if prior_lot else 0)
            if tuple(
                position_snapshot[key] for key in (
                    "quantity",
                    "status",
                    "cost_basis_krw",
                    "latest_mark_id",
                    "state_json")) != (
                expected_quantity,
                expected_status,
                expected_cost_basis,
                latest_mark_id,
                    _state_json(state)):
                raise SwingIntegrityError("position projection cannot be reproduced from events")
        snapshot_count = self._connection.execute(
            "SELECT COUNT(*) FROM swing_portfolio_snapshots_v1 WHERE portfolio_id=?", (self._portfolio_id,)
        ).fetchone()[0]
        if snapshot_count != len(events):
            raise SwingIntegrityError("portfolio snapshot count differs from the lifecycle event count")
        position_count = self._connection.execute(
            "SELECT COUNT(*) FROM swing_position_snapshots_v1 WHERE portfolio_id=?", (self._portfolio_id,)
        ).fetchone()[0]
        if position_count != len(events):
            raise SwingIntegrityError("orphan position snapshot detected")

    def _state_from_json(self, raw: str) -> PortfolioState:
        try:
            data = json.loads(raw)
            from kiwoom_stock.domain.accounting import IncompleteGate, Lot
            lots = tuple(Lot(**item) for item in data.get("lots", []))
            marks = []
            for item in data.get("marks", []):
                marks.append(self._mark_from_payload(item))
            return PortfolioState(
                data["portfolio_id"], data["cash_krw"], lots, data.get(
                    "external_flow_krw", 0), IncompleteGate(
                    data["gate"]) if data.get("gate") else None, tuple(marks))
        except Exception as exc:
            raise SwingIntegrityError("candidate state cannot be hydrated") from exc

    def _mark_from_payload(self, data: dict[str, Any]) -> Mark:
        data = dict(data)
        data["session_date"] = date.fromisoformat(data["session_date"])
        data["available_at"] = datetime.fromisoformat(data["available_at"].replace("Z", "+00:00"))
        data["computed_at"] = datetime.fromisoformat(data["computed_at"].replace("Z", "+00:00"))
        data["quality"] = MarkQuality(data["quality"])
        evidence = data.get("session_evidence")
        if isinstance(evidence, dict):
            evidence["session_date"] = date.fromisoformat(evidence["session_date"])
            if evidence.get("previous_session") is not None:
                evidence["previous_session"] = date.fromisoformat(evidence["previous_session"])
            data["session_evidence"] = SessionMarkEvidence(**evidence)
        return Mark(**data)

    def hydrate(self, *, portfolio_id: str, position_id: str | None = None) -> SwingHydration:
        if portfolio_id != self._portfolio_id:
            raise SwingIdentityConflictError("hydration is outside bound portfolio")

        def read() -> SwingHydration:
            self._registered()
            state = self._hydrate_state()
            if position_id is not None and not any(lot.position_id == position_id for lot in state.lots):
                raise SwingIdentityConflictError("unknown position")
            row = fetch_latest_portfolio_snapshot(
                self._connection,
                self._portfolio_id,
            )
            snapshot = reduce_portfolio(state, (), self._policy, portfolio_id=self._portfolio_id).snapshot
            positions = fetch_latest_position_sequences(
                self._connection,
                self._portfolio_id,
            )
            marks = fetch_daily_mark_revisions(
                self._connection,
                self._portfolio_id,
            )
            return SwingHydration(
                self._portfolio_id,
                state,
                snapshot,
                row["sequence"] if row else 0,
                positions,
                marks,
                row["snapshot_hash"] if row else GENESIS_HASH)
        return cast(SwingHydration, self._run(read))

    def hydrate_episode(self, *, episode_id: str) -> SwingEpisodeHydration:
        if not isinstance(episode_id, str) or not episode_id.strip():
            raise ValueError("episode_id is required")

        def read() -> SwingEpisodeHydration:
            self._registered()
            return self._read_episode(episode_id)

        return cast(SwingEpisodeHydration, self._run(read))

    def append_episode(self, command: SwingEpisodeAppendCommand) -> SwingCommitReceipt:
        self._assert_writable()
        if not isinstance(command, SwingEpisodeAppendCommand):
            raise TypeError("typed SwingEpisodeAppendCommand is required")
        command_payload = self._episode_command_payload(command)
        command_hash = canonical_payload(command_payload)

        def write() -> SwingCommitReceipt:
            self._registered()
            rows = self._connection.execute(
                "SELECT event_id, payload_json FROM swing_episode_events_v1 WHERE portfolio_id=? ORDER BY rowid",
                (self._portfolio_id,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                if payload.get("idempotency_key") != command.idempotency_key:
                    continue
                if payload.get("command_hash") != command_hash:
                    raise SwingIdempotencyConflictError("episode idempotency envelope changed")
                return SwingCommitReceipt(
                    self._portfolio_id,
                    SwingCommandKind.APPEND_EPISODE,
                    command.idempotency_key,
                    command_hash,
                    self._latest_portfolio_sequence(),
                    None,
                    None,
                    int(payload["sequence"]),
                    True,
                )

            event_row = self._connection.execute(
                "SELECT payload_json FROM swing_episode_events_v1 WHERE event_id=?",
                (command.event.event_id,),
            ).fetchone()
            if event_row is not None:
                raise SwingTransitionConflictError("episode event id is already consumed")
            current = self._read_episode(command.episode_id)
            if current.snapshot.state is EpisodeState.TERMINAL:
                raise SwingTransitionConflictError("episode semantic version is terminal")
            if current.verified_sequence != command.expected_episode_sequence:
                raise SwingTransitionConflictError("episode high-water is stale")
            if command.event.semantic_version != EPISODE_SEMANTIC_VERSION:
                raise SwingTransitionConflictError("episode semantic version is unsupported")
            if command.event.event_type is EpisodeEventType.REARM:
                if (
                    command.rearm_evidence is None
                    or command.current_session is None
                    or command.previous_session is None
                ):
                    raise SwingTransitionConflictError("episode re-arm session evidence is required")
                try:
                    SwingSessionCoordinator().validate_episode_rearm(
                        previous_session=command.previous_session,
                        current_session=command.current_session,
                    )
                except Exception as exc:
                    raise SwingTransitionConflictError("episode re-arm session evidence is invalid") from exc
            try:
                next_snapshot = reduce_episode(
                    current.snapshot,
                    command.event,
                    current_version=EPISODE_SEMANTIC_VERSION,
                    evidence=command.rearm_evidence,
                )
            except ContractError as exc:
                raise SwingTransitionConflictError("episode transition is invalid") from exc
            if not isinstance(next_snapshot, EpisodeSnapshot):
                raise SwingAccountingDivergenceError("episode reducer returned an unbound state")
            sequence = current.verified_sequence + 1
            payload = {
                "portfolio_id": self._portfolio_id,
                "episode_id": command.episode_id,
                "event_id": command.event.event_id,
                "idempotency_key": command.idempotency_key,
                "sequence": sequence,
                "expected_episode_sequence": command.expected_episode_sequence,
                "command_hash": command_hash,
                "current_session": command.current_session,
                "previous_session": command.previous_session,
                "event": command.event,
                "rearm_evidence": command.rearm_evidence,
                "state_after": self._episode_snapshot_payload(next_snapshot),
            }
            self._connection.execute(
                "INSERT INTO swing_episode_events_v1(event_id, portfolio_id, payload_json) VALUES (?,?,?)",
                (command.event.event_id, self._portfolio_id, _json(payload)),
            )
            self._connection.execute(
                "INSERT INTO swing_episode_snapshots_v1(portfolio_id, episode_id, sequence, payload_json) VALUES (?,?,?,?)",
                (
                    self._portfolio_id,
                    command.episode_id,
                    sequence,
                    _json(self._episode_snapshot_payload(next_snapshot)),
                ),
            )
            return SwingCommitReceipt(
                self._portfolio_id,
                SwingCommandKind.APPEND_EPISODE,
                command.idempotency_key,
                command_hash,
                self._latest_portfolio_sequence(),
                None,
                None,
                sequence,
            )

        return cast(SwingCommitReceipt, self._run(write))
