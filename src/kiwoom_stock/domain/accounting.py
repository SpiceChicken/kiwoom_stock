"""Deterministic, side-effect-free portfolio accounting and event reduction."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .swing_contracts import (
    AdmissionResult, CorporateAction, CorporateActionKind, ContractError, FillTiming,
    InsufficientDataError, Mark, MarkCompleteness, TemporalCausalityError,
    require_aware, require_positive_int,
)


class AccountingInvariantError(ContractError):
    pass


class UnitValidationError(ContractError):
    pass


class IncompleteGate(StrEnum):
    UNKNOWN_CORPORATE_ACTION = "unknown corporate action"
    PIT_CORPORATE_ACTION = "corporate action is not point-in-time available at the effective boundary"
    INCOMPLETE_MARK = "incomplete mark"
    MARK_IDENTITY = "mark identity mismatch"
    MARK_REVISION = "mark revision chain is invalid"
    ORPHAN_MARK = "orphan mark revision"
    CORPORATE_ACTION_IDENTITY = "corporate action identity mismatch"
    STALE_MARK = "mark is stale"
    SESSION_EVIDENCE = "session evidence is incomplete"


class CostScenario(StrEnum):
    GROSS = "gross"
    BASE = "base"
    STRESS = "stress"


def _bps(value: int | Decimal, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise UnitValidationError(f"{name} must be integer bps or Decimal")
    result = Decimal(value)
    if not result.is_finite() or result < 0:
        raise UnitValidationError(f"{name} must be finite and non-negative")
    return result


def _krw(value: int, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or (value <= 0 if positive else value < 0):
        raise UnitValidationError(f"{name} must be {'positive' if positive else 'non-negative'} integer KRW")
    return value


def _id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AccountingInvariantError(f"{name} is required")


@dataclass(frozen=True)
class CostPolicy:
    version: str
    commission_bps: int | Decimal = 0
    tax_bps: int | Decimal = 0
    slippage_bps: int | Decimal = 0

    def __post_init__(self) -> None:
        _id(self.version, "cost policy version")
        for name in ("commission_bps", "tax_bps", "slippage_bps"):
            _bps(getattr(self, name), name)


@dataclass(frozen=True)
class AccountingPolicy:
    policy_version: str
    initial_cash_krw: int
    base: CostPolicy
    stress: CostPolicy
    external_flow_krw: int = 0

    def __post_init__(self) -> None:
        _id(self.policy_version, "accounting policy version")
        _krw(self.initial_cash_krw, "initial_cash_krw")
        if isinstance(
                self.external_flow_krw,
                bool) or not isinstance(
                self.external_flow_krw,
                int) or self.external_flow_krw != 0:
            raise AccountingInvariantError("external flow is fixed at exact integer zero")
        for name in ("commission_bps", "tax_bps", "slippage_bps"):
            if _bps(getattr(self.stress, name), name) < _bps(getattr(self.base, name), name):
                raise AccountingInvariantError("stress cost policy cannot be below base policy")


@dataclass(frozen=True)
class Fill:
    fill_id: str
    portfolio_id: str
    symbol: str
    side: str
    quantity: int
    raw_price_krw: int
    decision_at: datetime
    fill_at: datetime
    cost_scenario: CostScenario = CostScenario.BASE
    commission_krw: int | None = None
    tax_krw: int | None = None
    slippage_krw: int | None = None
    timing: FillTiming | None = None
    position_id: str = ""

    @property
    def execution_price_krw(self) -> int:
        """Raw execution/accounting price; corporate actions never rewrite it."""
        return self.raw_price_krw

    def __post_init__(self) -> None:
        for value, name in ((self.fill_id, "fill_id"), (self.portfolio_id, "portfolio_id"),
                            (self.position_id, "position_id"), (self.symbol, "symbol")):
            _id(value, name)
        if self.side not in {"BUY", "SELL"} or not isinstance(self.cost_scenario, CostScenario):
            raise AccountingInvariantError("fill side and typed scenario are required")
        require_positive_int(self.quantity, "quantity")
        _krw(self.raw_price_krw, "raw_price_krw", positive=True)
        require_aware(self.decision_at, "decision_at")
        require_aware(self.fill_at, "fill_at")
        if self.timing is None or self.timing.decision_at != self.decision_at or self.timing.fill_at != self.fill_at:
            raise TemporalCausalityError("validated FillTiming is required")
        if any(value is not None for value in (self.commission_krw, self.tax_krw, self.slippage_krw)):
            raise AccountingInvariantError("unbound explicit fill costs are forbidden")


@dataclass(frozen=True)
class AdjustedFeatureView:
    """Feature-only adjusted view. It is deliberately not accepted by Fill."""
    symbol: str
    session_date: date
    raw_price_krw: int
    adjusted_price_krw: int
    action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _id(self.symbol, "symbol")
        _krw(self.raw_price_krw, "raw_price_krw", positive=True)
        _krw(self.adjusted_price_krw, "adjusted_price_krw", positive=True)
        if not isinstance(self.session_date, date) or any(not isinstance(v, str) or not v.strip()
                                                          for v in self.action_ids):
            raise UnitValidationError("adjusted feature evidence is invalid")


def adjusted_feature_view(
        raw_price_krw: int,
        action: CorporateAction | None,
        *,
        symbol: str,
        session_date: date) -> AdjustedFeatureView:
    """Apply a known action only to feature values, never to execution cash."""
    _krw(raw_price_krw, "raw_price_krw", positive=True)
    adjusted = raw_price_krw
    ids: tuple[str, ...] = ()
    if action is not None:
        if not action.known or action.effective_session != session_date:
            raise InsufficientDataError("corporate-action feature evidence is unavailable")
        if action.kind is CorporateActionKind.SPLIT:
            adjusted = (raw_price_krw * action.quantity_denominator) // action.quantity_numerator
            if adjusted <= 0:
                raise AccountingInvariantError("adjusted feature price is non-positive")
        ids = (action.action_id,)
    return AdjustedFeatureView(symbol, session_date, raw_price_krw, adjusted, ids)


@dataclass(frozen=True)
class Lot:
    portfolio_id: str
    position_id: str
    symbol: str
    quantity: int
    cost_basis_krw: int

    def __post_init__(self) -> None:
        for value, name in ((self.portfolio_id, "portfolio_id"),
                            (self.position_id, "position_id"), (self.symbol, "symbol")):
            _id(value, name)
        require_positive_int(self.quantity, "quantity")
        _krw(self.cost_basis_krw, "cost_basis_krw", positive=True)


@dataclass(frozen=True)
class PortfolioState:
    portfolio_id: str
    cash_krw: int
    lots: tuple[Lot, ...] = ()
    external_flow_krw: int = 0
    gate: IncompleteGate | None = None
    marks: tuple[Mark, ...] = ()

    def __post_init__(self) -> None:
        _id(self.portfolio_id, "portfolio_id")
        _krw(self.cash_krw, "cash_krw")
        if isinstance(
                self.external_flow_krw,
                bool) or not isinstance(
                self.external_flow_krw,
                int) or self.external_flow_krw != 0:
            raise AccountingInvariantError("external flow is fixed at exact integer zero")
        if self.gate is not None and not isinstance(self.gate, IncompleteGate):
            raise UnitValidationError("gate must be IncompleteGate")
        symbols = [lot.symbol for lot in self.lots]
        positions = [lot.position_id for lot in self.lots]
        if len(symbols) != len(
            set(symbols)) or len(positions) != len(
            set(positions)) or any(
                lot.portfolio_id != self.portfolio_id for lot in self.lots):
            raise AccountingInvariantError("active lot identity is invalid")
        mark_keys = [(mark.portfolio_id, mark.position_id, mark.symbol) for mark in self.marks]
        if len(mark_keys) != len(set(mark_keys)) or any(mark.portfolio_id != self.portfolio_id for mark in self.marks):
            raise AccountingInvariantError("persisted mark identity is invalid")

    def lot_for(self, symbol: str, position_id: str | None = None) -> Lot | None:
        return next((lot for lot in self.lots if lot.symbol == symbol and (
            position_id is None or lot.position_id == position_id)), None)

    def mark_for(self, symbol: str, position_id: str) -> Mark | None:
        return next((mark for mark in self.marks if mark.symbol == symbol and mark.position_id == position_id), None)


@dataclass(frozen=True)
class CostView:
    scenario: CostScenario
    policy_version: str
    commission_krw: int
    tax_krw: int
    slippage_krw: int

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, CostScenario):
            raise UnitValidationError("cost scenario must be CostScenario")
        _id(self.policy_version, "cost policy version")
        for value, name in ((self.commission_krw, "commission_krw"),
                            (self.tax_krw, "tax_krw"), (self.slippage_krw, "slippage_krw")):
            _krw(value, name)

    @property
    def total_krw(self) -> int:
        return self.commission_krw + self.tax_krw + self.slippage_krw


@dataclass(frozen=True)
class CostBundle:
    gross: CostView
    base: CostView
    stress: CostView

    def __post_init__(self) -> None:
        if not all(isinstance(view, CostView) for view in (self.gross, self.base, self.stress)):
            raise UnitValidationError("cost bundle requires typed cost views")
        if not (self.stress.total_krw >= self.base.total_krw >= self.gross.total_krw):
            raise AccountingInvariantError("cost bundle must be monotonic stress >= base >= gross")


@dataclass(frozen=True)
class FillApplication:
    state: PortfolioState
    costs: CostView
    cost_bundle: CostBundle
    gross_cash_delta_krw: int
    net_cash_delta_krw: int
    admission: AdmissionResult = AdmissionResult.FILLED

    @property
    def cost_result(self) -> CostView:
        return self.costs


def _cost_view(fill: Fill, policy: CostPolicy, scenario: CostScenario) -> CostView:
    notional = fill.quantity * fill.raw_price_krw
    commission = int(Decimal(notional) * _bps(policy.commission_bps, "commission_bps") / 10000)
    tax = int(Decimal(notional) * _bps(policy.tax_bps, "tax_bps") / 10000) if fill.side == "SELL" else 0
    slippage = int(Decimal(notional) * _bps(policy.slippage_bps, "slippage_bps") / 10000)
    return CostView(scenario, policy.version, commission, tax, slippage)


def _assert_open_gate(state: PortfolioState) -> None:
    if state.gate is not None:
        raise InsufficientDataError(str(state.gate))


def apply_fill(state: PortfolioState, fill: Fill, policy: AccountingPolicy) -> FillApplication:
    _assert_open_gate(state)
    if fill.portfolio_id != state.portfolio_id:
        raise AccountingInvariantError("fill portfolio identity does not match state")
    if fill.side == "SELL" and state.lot_for(fill.symbol, fill.position_id) is None:
        raise AccountingInvariantError("sell position identity does not match state")
    gross_policy = CostPolicy(f"{policy.policy_version}:gross")
    bundle = CostBundle(
        _cost_view(
            fill, gross_policy, CostScenario.GROSS), _cost_view(
            fill, policy.base, CostScenario.BASE), _cost_view(
                fill, policy.stress, CostScenario.STRESS))
    costs = bundle.gross if fill.cost_scenario is CostScenario.GROSS else bundle.base if fill.cost_scenario is CostScenario.BASE else bundle.stress
    notional = fill.quantity * fill.raw_price_krw
    lot = state.lot_for(fill.symbol, fill.position_id)
    if fill.side == "BUY":
        if lot is not None:
            raise AccountingInvariantError("pyramiding is not allowed")
        total = notional + costs.total_krw
        if state.cash_krw < total:
            raise AccountingInvariantError("insufficient cash")
        new_lot = Lot(state.portfolio_id, fill.position_id, fill.symbol, fill.quantity, total)
        return FillApplication(replace(state, cash_krw=state.cash_krw - total,
                               lots=state.lots + (new_lot,)), costs, bundle, -notional, -total)
    if lot is None or lot.quantity != fill.quantity:
        raise AccountingInvariantError("sell must close the complete active lot")
    proceeds = notional - costs.total_krw
    return FillApplication(
        replace(
            state,
            cash_krw=state.cash_krw + proceeds,
            lots=tuple(x for x in state.lots if not (x.position_id == fill.position_id and x.symbol == fill.symbol)),
            marks=tuple(x for x in state.marks if not (x.position_id == fill.position_id and x.symbol == fill.symbol)),
        ),
        costs,
        bundle,
        notional,
        proceeds,
    )


def initial_state(policy: AccountingPolicy, portfolio_id: str = "paper") -> PortfolioState:
    return PortfolioState(portfolio_id, policy.initial_cash_krw)


@dataclass(frozen=True)
class CorporateActionApplication:
    state: PortfolioState
    cash_delta_krw: int
    action_id: str = ""
    kind: CorporateActionKind | None = None


def apply_corporate_action(
        state: PortfolioState,
        action: CorporateAction,
        *,
        decision_at: datetime,
        session_date: date) -> CorporateActionApplication:
    if action.portfolio_id != state.portfolio_id:
        raise AccountingInvariantError("corporate action portfolio identity does not match state")
    if not action.known:
        return CorporateActionApplication(
            replace(
                state,
                gate=IncompleteGate.UNKNOWN_CORPORATE_ACTION),
            0,
            action.action_id,
            action.kind)
    if not action.usable_at(decision_at, session_date, decision_session=decision_at.date()):
        return CorporateActionApplication(
            replace(
                state,
                gate=IncompleteGate.PIT_CORPORATE_ACTION),
            0,
            action.action_id,
            action.kind)
    lot = state.lot_for(action.symbol, action.position_id)
    if lot is None:
        return CorporateActionApplication(
            replace(
                state,
                gate=IncompleteGate.CORPORATE_ACTION_IDENTITY),
            0,
            action.action_id,
            action.kind)
    if action.kind is CorporateActionKind.SPLIT:
        if action.cash_per_share_krw != 0:
            raise AccountingInvariantError("split cannot contain dividend cash")
        numerator = lot.quantity * action.quantity_numerator
        if numerator % action.quantity_denominator:
            raise AccountingInvariantError("corporate action produces fractional quantity")
        new_qty = numerator // action.quantity_denominator
        if new_qty <= 0:
            raise AccountingInvariantError("corporate action produced non-positive quantity")
        cash = 0
    elif action.kind is CorporateActionKind.DIVIDEND:
        if (action.quantity_numerator, action.quantity_denominator) != (1, 1):
            raise AccountingInvariantError("dividend cannot change quantity")
        new_qty = lot.quantity
        cash = lot.quantity * action.cash_per_share_krw
    else:
        raise AccountingInvariantError("unsupported corporate action kind")
    new_lot = replace(lot, quantity=new_qty)
    # An action changes the price basis/quantity boundary. Any pre-action mark
    # is no longer a valid NAV mark until a fresh post-action mark arrives.
    marks = tuple(x for x in state.marks if not (x.position_id == lot.position_id and x.symbol == lot.symbol))
    return CorporateActionApplication(
        replace(
            state,
            cash_krw=state.cash_krw +
            cash,
            lots=tuple(
                new_lot if x.position_id == lot.position_id and x.symbol == lot.symbol else x for x in state.lots),
            marks=marks,
            gate=IncompleteGate.INCOMPLETE_MARK),
        cash,
        action.action_id,
        action.kind)


def apply_mark(state: PortfolioState, mark: Mark) -> int:
    _assert_open_gate(state)
    if mark.portfolio_id != state.portfolio_id:
        raise AccountingInvariantError("mark portfolio identity does not match state")
    lot = state.lot_for(mark.symbol, mark.position_id)
    if lot is None:
        raise AccountingInvariantError("mark position identity does not match state")
    if mark.price_krw is None or mark.completeness is MarkCompleteness.INCOMPLETE:
        raise InsufficientDataError("mark is INCOMPLETE; NAV acceptance is blocked")
    previous = state.mark_for(mark.symbol, mark.position_id)
    if previous is None and mark.revision != 1:
        raise AccountingInvariantError("orphan mark revision")
    if previous is not None and (mark.revision != previous.revision + 1 or mark.supersedes_id != previous.mark_id):
        raise AccountingInvariantError("mark revision chain is invalid")
    return lot.quantity * mark.price_krw


@dataclass(frozen=True)
class ApplyFill:
    fill: Fill
    result: AdmissionResult = AdmissionResult.FILLED

    def __post_init__(self) -> None:
        if not isinstance(self.result, AdmissionResult):
            raise ContractError("fill result must be AdmissionResult")


@dataclass(frozen=True)
class ApplyDailyMark:
    mark: Mark

    def __post_init__(self) -> None:
        if not isinstance(self.mark, Mark):
            raise ContractError("daily mark event requires Mark")


@dataclass(frozen=True)
class ApplyCorporateAction:
    action: CorporateAction
    decision_at: datetime
    session_date: date

    def __post_init__(self) -> None:
        if not isinstance(self.action, CorporateAction):
            raise ContractError("corporate-action event requires CorporateAction")
        require_aware(self.decision_at, "decision_at")
        if not isinstance(self.session_date, date):
            raise ContractError("corporate-action session must be a date")


@dataclass(frozen=True)
class PortfolioSnapshot:
    portfolio_id: str
    revision: int
    cash_krw: int
    market_value_krw: int
    receivables_krw: int
    liabilities_krw: int
    equity_krw: int
    completeness: MarkCompleteness
    gate: IncompleteGate | None = None

    def __post_init__(self) -> None:
        _id(self.portfolio_id, "portfolio_id")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 0:
            raise UnitValidationError("revision must be a non-negative integer")
        for value, name in ((self.cash_krw, "cash_krw"), (self.market_value_krw, "market_value_krw"), (self.receivables_krw,
                            "receivables_krw"), (self.liabilities_krw, "liabilities_krw"), (self.equity_krw, "equity_krw")):
            _krw(value, name)
        if not isinstance(self.completeness, MarkCompleteness):
            raise UnitValidationError("completeness must be MarkCompleteness")
        if self.equity_krw != self.cash_krw + self.market_value_krw + self.receivables_krw - self.liabilities_krw:
            raise AccountingInvariantError("equity identity violated")


@dataclass(frozen=True)
class ReductionResult:
    state: PortfolioState
    snapshot: PortfolioSnapshot
    marks: tuple[Mark, ...]


Event = ApplyFill | ApplyDailyMark | ApplyCorporateAction


def reduce_portfolio(previous: PortfolioState,
                     events: tuple[Event,
                                   ...],
                     policy: AccountingPolicy,
                     *,
                     portfolio_id: str,
                     receivables_krw: int = 0,
                     liabilities_krw: int = 0) -> ReductionResult:
    _id(portfolio_id, "portfolio_id")
    if previous.portfolio_id != portfolio_id:
        raise AccountingInvariantError("reducer portfolio identity does not match state")
    _krw(receivables_krw, "receivables_krw")
    _krw(liabilities_krw, "liabilities_krw")
    state = previous
    marks: dict[tuple[str, str, str], Mark] = {
        (mark.portfolio_id, mark.position_id, mark.symbol): mark for mark in previous.marks
    }
    revision = 0
    last_event_at: datetime | None = None
    for event in events:
        if not isinstance(event, (ApplyFill, ApplyDailyMark, ApplyCorporateAction)):
            raise AccountingInvariantError("unknown accounting event")
        event_at = event.fill.fill_at if isinstance(
            event, ApplyFill) else event.mark.computed_at if isinstance(
            event, ApplyDailyMark) else event.decision_at
        if last_event_at is not None and event_at < last_event_at:
            raise TemporalCausalityError("accounting events must be in deterministic time order")
        last_event_at = event_at
        if isinstance(event, ApplyFill):
            if event.fill.portfolio_id != portfolio_id:
                raise AccountingInvariantError("fill portfolio identity does not match reducer")
            if event.result is AdmissionResult.FILLED:
                state = apply_fill(state, event.fill, policy).state
                marks = {
                    key: mark
                    for key, mark in marks.items()
                    if state.lot_for(mark.symbol, mark.position_id) is not None
                }
        elif isinstance(event, ApplyCorporateAction):
            if event.action.portfolio_id != portfolio_id:
                raise AccountingInvariantError("corporate action portfolio identity does not match reducer")
            state = apply_corporate_action(
                state,
                event.action,
                decision_at=event.decision_at,
                session_date=event.session_date).state
            marks = {
                key: mark for key, mark in marks.items() if state.lot_for(
                    mark.symbol, mark.position_id) is not None and not (
                    mark.symbol == event.action.symbol and mark.position_id == event.action.position_id)}
            state = replace(state, marks=tuple(marks.values()))
        else:
            mark = event.mark
            key = (mark.portfolio_id, mark.position_id, mark.symbol)
            if mark.portfolio_id != portfolio_id or state.lot_for(mark.symbol, mark.position_id) is None:
                state = replace(state, gate=IncompleteGate.MARK_IDENTITY)
                continue
            previous_mark = marks.get(key)
            if previous_mark is not None and (
                    mark.supersedes_id != previous_mark.mark_id or mark.revision != previous_mark.revision + 1):
                state = replace(state, gate=IncompleteGate.MARK_REVISION)
                continue
            if previous_mark is None and mark.revision != 1:
                state = replace(state, gate=IncompleteGate.ORPHAN_MARK)
                continue
            marks[key] = mark
            revision += 1
            state = replace(state, marks=tuple(marks.values()))
            state = replace(
                state,
                gate=IncompleteGate.INCOMPLETE_MARK if mark.completeness is MarkCompleteness.INCOMPLETE else state.gate)
            if mark.completeness is MarkCompleteness.COMPLETE and state.gate is IncompleteGate.INCOMPLETE_MARK:
                state = replace(state, gate=None)
    complete = state.gate is None
    market_value = 0
    for lot in state.lots:
        lot_mark = marks.get((state.portfolio_id, lot.position_id, lot.symbol))
        if lot_mark is None or lot_mark.price_krw is None or lot_mark.completeness is MarkCompleteness.INCOMPLETE:
            complete = False
    if not complete and state.gate is None:
        state = replace(state, gate=IncompleteGate.INCOMPLETE_MARK)
    if complete:
        for lot in state.lots:
            lot_mark = marks[(state.portfolio_id, lot.position_id, lot.symbol)]
            if lot_mark.price_krw is None:
                raise InsufficientDataError("complete snapshot lacks a mark price")
            market_value += lot.quantity * lot_mark.price_krw
    state = replace(state, marks=tuple(marks.values()))
    snapshot = PortfolioSnapshot(
        portfolio_id,
        revision,
        state.cash_krw,
        market_value,
        receivables_krw,
        liabilities_krw,
        state.cash_krw +
        market_value +
        receivables_krw -
        liabilities_krw,
        MarkCompleteness.COMPLETE if complete else MarkCompleteness.INCOMPLETE,
        state.gate)
    return ReductionResult(state, snapshot, tuple(marks.values()))
