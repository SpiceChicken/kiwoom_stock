"""Side-effect-free same-input shadow fan-out for the swing candidate."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, Mapping

from kiwoom_stock.domain.swing_strategy import SwingEvaluationContext
from kiwoom_stock.application.swing_replay import canonical_replay_hash


def _freeze(value: Any) -> Any:
    """Recursively freeze fan-out input so evaluators share one immutable view."""

    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


@dataclass(frozen=True)
class SwingShadowInput:
    snapshot_id: str
    decision_at: datetime
    payload: Mapping[str, Any]
    strategy_context: SwingEvaluationContext | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("shadow input identity/timing is invalid")
        if not isinstance(self.payload, Mapping):
            raise TypeError("shadow input payload must be a mapping")
        if self.strategy_context is not None and not isinstance(
            self.strategy_context, SwingEvaluationContext
        ):
            raise TypeError("shadow input strategy context is invalid")
        object.__setattr__(self, "payload", _freeze(self.payload))

    @property
    def input_hash(self) -> str:
        return canonical_replay_hash(
            (
                self.snapshot_id,
                self.decision_at,
                self.payload,
                self.strategy_context,
            )
        )


@dataclass(frozen=True)
class SwingShadowEvidence:
    snapshot_id: str
    input_hash: str
    legacy_output_hash: str | None
    candidate_output_hash: str | None
    candidate_enabled: bool
    candidate_database_path: str | None = None
    candidate_portfolio_id: str | None = None
    side_effects: bool = False
    candidate_decision: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.snapshot_id.strip() or not self.input_hash:
            raise ValueError("shadow evidence identity is required")
        if self.side_effects:
            raise ValueError("swing shadow evidence cannot contain side effects")
        if self.candidate_enabled and self.candidate_output_hash is None:
            raise ValueError("enabled candidate shadow requires an output hash")
        if (self.candidate_database_path is None) != (
            self.candidate_portfolio_id is None
        ):
            raise ValueError("candidate isolated identity must be complete")
        if self.candidate_decision is not None:
            if not isinstance(self.candidate_decision, Mapping):
                raise TypeError("candidate decision evidence must be a mapping")
            if self.candidate_decision.get("decision_schema") != "swing-decision-v1":
                raise ValueError("candidate decision evidence schema is invalid")
            object.__setattr__(
                self,
                "candidate_decision",
                _freeze(self.candidate_decision),
            )

    def to_safe_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "input_hash": self.input_hash,
            "legacy_output_hash": self.legacy_output_hash,
            "candidate_output_hash": self.candidate_output_hash,
            "candidate_enabled": self.candidate_enabled,
            "candidate_database_path": self.candidate_database_path,
            "candidate_portfolio_id": self.candidate_portfolio_id,
            "side_effects": self.side_effects,
        }
        if self.candidate_decision is not None:
            result["candidate_decision"] = dict(self.candidate_decision)
        return result


@dataclass(frozen=True)
class SwingShadowRun:
    evidence: SwingShadowEvidence
    legacy_output: Mapping[str, Any] | None = None
    candidate_output: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ShadowInputAssembly:
    """Immutable market input plus the already-bound candidate evaluator."""

    shadow_input: SwingShadowInput
    candidate_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None


def market_snapshot_payload(
    snapshot: Any,
    *,
    stock_code: str,
    proxy_code: str,
) -> Mapping[str, Any]:
    """Convert one validated market snapshot into the shadow input contract."""

    required = ("basic", "stock_chart", "proxy_chart", "strength", "order_book")
    if any(not hasattr(snapshot, name) for name in required):
        raise TypeError("market snapshot is missing a required component")
    if not stock_code.strip() or not proxy_code.strip():
        raise ValueError("market snapshot symbols are required")
    if any(not isinstance(value, Mapping) for value in (snapshot.basic, snapshot.order_book)):
        raise TypeError("market snapshot mappings are invalid")
    if any(
        not isinstance(row, Mapping)
        for rows in (snapshot.stock_chart, snapshot.proxy_chart, snapshot.strength)
        for row in rows
    ):
        raise TypeError("market snapshot rows are invalid")
    payload = {
        "snapshot_schema": "market-snapshot-v1",
        "stock_code": stock_code,
        "proxy_code": proxy_code,
        "basic": dict(snapshot.basic),
        "stock_chart": tuple(dict(row) for row in snapshot.stock_chart),
        "proxy_chart": tuple(dict(row) for row in snapshot.proxy_chart),
        "strength": tuple(dict(row) for row in snapshot.strength),
        "order_book": dict(snapshot.order_book),
    }
    return payload


def assemble_shadow_input(
    snapshot: Any,
    *,
    stock_code: str,
    proxy_code: str,
    activation_id: str,
    decision_at: datetime,
    candidate_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None = None,
    candidate_context_factory: Callable[[SwingShadowInput], SwingEvaluationContext] | None = None,
    candidate_evaluator_factory: Callable[
        [str | None], Callable[[SwingShadowInput], Mapping[str, Any]]
    ] | None = None,
    expected_strategy_semantics_version: str | None = None,
) -> ShadowInputAssembly:
    """Build one immutable input and bind optional candidate context/evaluator."""

    payload = market_snapshot_payload(
        snapshot,
        stock_code=stock_code,
        proxy_code=proxy_code,
    )
    base_input = SwingShadowInput(
        snapshot_id=f"{activation_id}:{canonical_replay_hash(payload)}",
        decision_at=decision_at,
        payload=payload,
    )
    effective_evaluator = candidate_evaluator
    shadow_input = base_input
    if candidate_context_factory is not None:
        context = candidate_context_factory(base_input)
        shadow_input = SwingShadowInput(
            snapshot_id=base_input.snapshot_id,
            decision_at=base_input.decision_at,
            payload=base_input.payload,
            strategy_context=context,
        )
        if candidate_evaluator_factory is None:
            raise ValueError("candidate evaluator factory is required with a context factory")
        effective_evaluator = candidate_evaluator_factory(
            expected_strategy_semantics_version
        )
    return ShadowInputAssembly(shadow_input, effective_evaluator)


def run_same_input_shadow(
    *,
    snapshot: SwingShadowInput,
    legacy_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None = None,
    candidate_evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None = None,
    candidate_enabled: bool = False,
    candidate_database_path: str | None = None,
    candidate_portfolio_id: str | None = None,
) -> SwingShadowRun:
    """Fan out one immutable input; evaluation callbacks must not perform writes."""

    legacy_output = legacy_evaluator(snapshot) if legacy_evaluator is not None else None
    candidate_output = candidate_evaluator(snapshot) if candidate_enabled and candidate_evaluator is not None else None
    for name, output in (("legacy", legacy_output), ("candidate", candidate_output)):
        if output is not None and not isinstance(output, Mapping):
            raise TypeError(f"{name} shadow evaluator must return a mapping")
    evidence = SwingShadowEvidence(
        snapshot.snapshot_id,
        snapshot.input_hash,
        canonical_replay_hash(legacy_output) if legacy_output is not None else None,
        canonical_replay_hash(candidate_output) if candidate_output is not None else None,
        candidate_enabled,
        candidate_database_path,
        candidate_portfolio_id,
        False,
        (
            dict(candidate_output)
            if candidate_output is not None
            and candidate_output.get("decision_schema") == "swing-decision-v1"
            else None
        ),
    )
    return SwingShadowRun(evidence, legacy_output, candidate_output)
