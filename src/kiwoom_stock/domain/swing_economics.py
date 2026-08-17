"""Pure after-cost episode economics for offline swing research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

from kiwoom_stock.domain.accounting import Fill, FillApplication
from kiwoom_stock.utils.market_cal import KST


class SwingEconomicsError(ValueError):
    """Episode economics input is incomplete or internally inconsistent."""


def _identity(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SwingEconomicsError(f"{name} is required")


def _non_negative(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise SwingEconomicsError(f"{name} must be a non-negative integer KRW")


@dataclass(frozen=True)
class EpisodeFillObservation:
    """One already-accounted fill leg belonging to one swing episode."""

    fill_id: str
    episode_id: str
    position_id: str
    symbol: str
    side: str
    quantity: int
    fill_session: date
    gross_cash_delta_krw: int
    base_cash_delta_krw: int
    stress_cash_delta_krw: int

    def __post_init__(self) -> None:
        for identity_value, name in (
            (self.fill_id, "fill_id"),
            (self.episode_id, "episode_id"),
            (self.position_id, "position_id"),
            (self.symbol, "symbol"),
        ):
            _identity(identity_value, name)
        if self.side not in {"BUY", "SELL"}:
            raise SwingEconomicsError("fill side must be BUY or SELL")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise SwingEconomicsError("fill quantity must be positive")
        if not isinstance(self.fill_session, date):
            raise SwingEconomicsError("fill session must be a date")
        for delta_value, name in (
            (self.gross_cash_delta_krw, "gross_cash_delta_krw"),
            (self.base_cash_delta_krw, "base_cash_delta_krw"),
            (self.stress_cash_delta_krw, "stress_cash_delta_krw"),
        ):
            if type(delta_value) is not int:
                raise SwingEconomicsError(f"{name} must be an integer KRW delta")
        base_cost = self.gross_cash_delta_krw - self.base_cash_delta_krw
        stress_cost = self.gross_cash_delta_krw - self.stress_cash_delta_krw
        if base_cost < 0 or stress_cost < base_cost:
            raise SwingEconomicsError("fill cost ordering is invalid")

    @property
    def base_cost_krw(self) -> int:
        return self.gross_cash_delta_krw - self.base_cash_delta_krw

    @property
    def stress_cost_krw(self) -> int:
        return self.gross_cash_delta_krw - self.stress_cash_delta_krw

    @classmethod
    def from_fill_application(
        cls,
        *,
        episode_id: str,
        fill: Fill,
        application: FillApplication,
    ) -> "EpisodeFillObservation":
        """Project typed accounting output without recomputing strategy state."""

        if not isinstance(fill, Fill) or not isinstance(application, FillApplication):
            raise SwingEconomicsError("typed Fill and FillApplication are required")
        notional = fill.quantity * fill.raw_price_krw
        gross = -notional if fill.side == "BUY" else notional
        if application.gross_cash_delta_krw != gross:
            raise SwingEconomicsError("accounting gross cash delta differs from fill")
        base = gross - application.cost_bundle.base.total_krw
        stress = gross - application.cost_bundle.stress.total_krw
        return cls(
            fill.fill_id,
            episode_id,
            fill.position_id,
            fill.symbol,
            fill.side,
            fill.quantity,
            fill.fill_at.astimezone(KST).date(),
            gross,
            base,
            stress,
        )


@dataclass(frozen=True)
class EpisodeMarkObservation:
    """A complete mark used to value one still-open episode."""

    episode_id: str
    position_id: str
    symbol: str
    mark_session: date
    gross_value_krw: int
    base_liquidation_cost_krw: int
    stress_liquidation_cost_krw: int
    complete: bool = True

    def __post_init__(self) -> None:
        for value, name in (
            (self.episode_id, "episode_id"),
            (self.position_id, "position_id"),
            (self.symbol, "symbol"),
        ):
            _identity(value, name)
        if not isinstance(self.mark_session, date):
            raise SwingEconomicsError("mark session must be a date")
        if type(self.gross_value_krw) is not int or self.gross_value_krw <= 0:
            raise SwingEconomicsError("mark gross value must be positive")
        _non_negative(self.base_liquidation_cost_krw, "base_liquidation_cost_krw")
        _non_negative(self.stress_liquidation_cost_krw, "stress_liquidation_cost_krw")
        if self.stress_liquidation_cost_krw < self.base_liquidation_cost_krw:
            raise SwingEconomicsError("mark liquidation cost ordering is invalid")
        if type(self.complete) is not bool:
            raise SwingEconomicsError("mark completeness must be boolean")


@dataclass(frozen=True)
class EpisodeOutcome:
    episode_id: str
    position_id: str
    symbol: str
    entry_session: date
    last_session: date
    holding_sessions: int
    fill_count: int
    closed: bool
    gross_pnl_krw: int
    base_pnl_krw: int
    stress_pnl_krw: int
    base_cost_krw: int
    stress_cost_krw: int
    realized_base_pnl_krw: int | None
    unrealized_base_pnl_krw: int | None

    def __post_init__(self) -> None:
        for value, name in (
            (self.episode_id, "episode_id"),
            (self.position_id, "position_id"),
            (self.symbol, "symbol"),
        ):
            _identity(value, name)
        if not isinstance(self.entry_session, date) or not isinstance(self.last_session, date):
            raise SwingEconomicsError("outcome sessions must be dates")
        if self.last_session < self.entry_session:
            raise SwingEconomicsError("outcome sessions are not ordered")
        if type(self.holding_sessions) is not int or self.holding_sessions <= 0:
            raise SwingEconomicsError("holding sessions must be positive")
        if type(self.fill_count) is not int or self.fill_count <= 0:
            raise SwingEconomicsError("fill count must be positive")
        if type(self.closed) is not bool:
            raise SwingEconomicsError("closed must be boolean")
        _non_negative(self.base_cost_krw, "base_cost_krw")
        _non_negative(self.stress_cost_krw, "stress_cost_krw")
        if self.stress_cost_krw < self.base_cost_krw:
            raise SwingEconomicsError("outcome cost ordering is invalid")
        if self.closed == (self.realized_base_pnl_krw is None):
            raise SwingEconomicsError("realized pnl presence does not match closure")
        if self.closed == (self.unrealized_base_pnl_krw is not None):
            raise SwingEconomicsError("unrealized pnl presence does not match closure")


def build_episode_outcome(
    fills: Sequence[EpisodeFillObservation],
    *,
    holding_sessions: int,
    mark: EpisodeMarkObservation | None = None,
) -> EpisodeOutcome:
    """Aggregate one episode, rejecting repeated lots or incomplete valuation."""

    legs = tuple(fills)
    if not legs or not all(isinstance(leg, EpisodeFillObservation) for leg in legs):
        raise SwingEconomicsError("episode requires typed fill observations")
    episode_ids = {leg.episode_id for leg in legs}
    position_ids = {leg.position_id for leg in legs}
    symbols = {leg.symbol for leg in legs}
    fill_ids = [leg.fill_id for leg in legs]
    if len(episode_ids) != 1 or len(position_ids) != 1 or len(symbols) != 1 or len(fill_ids) != len(set(fill_ids)):
        raise SwingEconomicsError("episode fill identities are inconsistent")
    buys = tuple(leg for leg in legs if leg.side == "BUY")
    sells = tuple(leg for leg in legs if leg.side == "SELL")
    if len(buys) != 1 or len(sells) > 1:
        raise SwingEconomicsError("one episode must contain one buy and at most one sell")
    buy = buys[0]
    sell = sells[0] if sells else None
    if sell is not None:
        if sell.fill_session < buy.fill_session or sell.quantity != buy.quantity:
            raise SwingEconomicsError("episode sell does not close the entry lot")
        if mark is not None:
            raise SwingEconomicsError("closed episode cannot carry an open mark")
    else:
        if mark is None or not mark.complete:
            raise SwingEconomicsError("open episode requires a complete mark")
        if (
            mark.episode_id != buy.episode_id
            or mark.position_id != buy.position_id
            or mark.symbol != buy.symbol
            or mark.mark_session < buy.fill_session
        ):
            raise SwingEconomicsError("open mark identity or chronology is invalid")

    closed = sell is not None
    entry_session = buy.fill_session
    last_session = sell.fill_session if sell is not None else mark.mark_session  # type: ignore[union-attr]
    gross = sum(leg.gross_cash_delta_krw for leg in legs)
    base = sum(leg.base_cash_delta_krw for leg in legs)
    stress = sum(leg.stress_cash_delta_krw for leg in legs)
    base_cost = sum(leg.base_cost_krw for leg in legs)
    stress_cost = sum(leg.stress_cost_krw for leg in legs)
    if not closed:
        assert mark is not None
        gross += mark.gross_value_krw
        base += mark.gross_value_krw - mark.base_liquidation_cost_krw
        stress += mark.gross_value_krw - mark.stress_liquidation_cost_krw
        base_cost += mark.base_liquidation_cost_krw
        stress_cost += mark.stress_liquidation_cost_krw
    return EpisodeOutcome(
        buy.episode_id,
        buy.position_id,
        buy.symbol,
        entry_session,
        last_session,
        holding_sessions,
        len(legs),
        closed,
        gross,
        base,
        stress,
        base_cost,
        stress_cost,
        base if closed else None,
        base if not closed else None,
    )


@dataclass(frozen=True)
class EpisodeAggregate:
    dataset_id: str
    episode_count: int
    closed_episode_count: int
    open_episode_count: int
    total_fill_count: int
    gross_pnl_krw: int
    base_pnl_krw: int
    stress_pnl_krw: int
    base_cost_krw: int
    stress_cost_krw: int
    realized_base_pnl_krw: int
    unrealized_base_pnl_krw: int
    average_holding_sessions: Decimal
    loss_episode_count: int

    def __post_init__(self) -> None:
        _identity(self.dataset_id, "dataset_id")
        if self.episode_count <= 0 or self.total_fill_count <= 0:
            raise SwingEconomicsError("aggregate requires at least one episode and fill")
        if self.closed_episode_count + self.open_episode_count != self.episode_count:
            raise SwingEconomicsError("aggregate episode counts do not reconcile")
        _non_negative(self.base_cost_krw, "base_cost_krw")
        _non_negative(self.stress_cost_krw, "stress_cost_krw")
        if self.stress_cost_krw < self.base_cost_krw:
            raise SwingEconomicsError("aggregate cost ordering is invalid")
        if not self.average_holding_sessions.is_finite() or self.average_holding_sessions <= 0:
            raise SwingEconomicsError("aggregate holding average is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "episode_count": self.episode_count,
            "closed_episode_count": self.closed_episode_count,
            "open_episode_count": self.open_episode_count,
            "total_fill_count": self.total_fill_count,
            "gross_pnl_krw": self.gross_pnl_krw,
            "base_pnl_krw": self.base_pnl_krw,
            "stress_pnl_krw": self.stress_pnl_krw,
            "base_cost_krw": self.base_cost_krw,
            "stress_cost_krw": self.stress_cost_krw,
            "realized_base_pnl_krw": self.realized_base_pnl_krw,
            "unrealized_base_pnl_krw": self.unrealized_base_pnl_krw,
            "average_holding_sessions": str(self.average_holding_sessions),
            "loss_episode_count": self.loss_episode_count,
        }


def aggregate_episode_outcomes(
    dataset_id: str,
    outcomes: Sequence[EpisodeOutcome],
) -> EpisodeAggregate:
    """Aggregate episodes without collapsing their individual identities."""

    items = tuple(outcomes)
    if not items or not all(isinstance(item, EpisodeOutcome) for item in items):
        raise SwingEconomicsError("aggregate requires typed episode outcomes")
    ids = [item.episode_id for item in items]
    if len(ids) != len(set(ids)):
        raise SwingEconomicsError("aggregate episode identities must be unique")
    closed = tuple(item for item in items if item.closed)
    open_items = tuple(item for item in items if not item.closed)
    return EpisodeAggregate(
        dataset_id,
        len(items),
        len(closed),
        len(open_items),
        sum(item.fill_count for item in items),
        sum(item.gross_pnl_krw for item in items),
        sum(item.base_pnl_krw for item in items),
        sum(item.stress_pnl_krw for item in items),
        sum(item.base_cost_krw for item in items),
        sum(item.stress_cost_krw for item in items),
        sum(item.realized_base_pnl_krw or 0 for item in closed),
        sum(item.unrealized_base_pnl_krw or 0 for item in open_items),
        Decimal(sum(item.holding_sessions for item in items)) / Decimal(len(items)),
        sum(1 for item in items if item.base_pnl_krw < 0),
    )


@dataclass(frozen=True)
class SwingEconomicComparison:
    baseline: EpisodeAggregate
    candidate: EpisodeAggregate

    def __post_init__(self) -> None:
        if self.baseline.dataset_id != self.candidate.dataset_id:
            raise SwingEconomicsError("baseline and candidate datasets differ")

    @property
    def base_pnl_delta_krw(self) -> int:
        return self.candidate.base_pnl_krw - self.baseline.base_pnl_krw

    @property
    def stress_pnl_delta_krw(self) -> int:
        return self.candidate.stress_pnl_krw - self.baseline.stress_pnl_krw

    @property
    def base_cost_delta_krw(self) -> int:
        return self.candidate.base_cost_krw - self.baseline.base_cost_krw

    @property
    def fill_count_delta(self) -> int:
        return self.candidate.total_fill_count - self.baseline.total_fill_count

    @property
    def holding_sessions_delta(self) -> Decimal:
        return self.candidate.average_holding_sessions - self.baseline.average_holding_sessions

    def to_dict(self) -> dict[str, object]:
        return {
            "baseline": self.baseline.to_dict(),
            "candidate": self.candidate.to_dict(),
            "base_pnl_delta_krw": self.base_pnl_delta_krw,
            "stress_pnl_delta_krw": self.stress_pnl_delta_krw,
            "base_cost_delta_krw": self.base_cost_delta_krw,
            "fill_count_delta": self.fill_count_delta,
            "holding_sessions_delta": str(self.holding_sessions_delta),
        }
