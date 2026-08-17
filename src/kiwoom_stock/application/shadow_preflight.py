"""Typed, side-effect-light preflight for the optional swing shadow branch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from kiwoom_stock.application.execution import ExecutionPolicy
from kiwoom_stock.application.swing_candidate import SwingCandidateContextFactory
from kiwoom_stock.application.swing_shadow import SwingShadowInput


class SwingCandidatePreflightError(RuntimeError):
    """Candidate composition is not safe to admit into the shadow graph."""


@dataclass(frozen=True)
class SwingCandidatePlan:
    """Resolved candidate inputs carried from validation into resource assembly."""

    enabled: bool
    evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None
    context_factory: SwingCandidateContextFactory | None
    database_path: Path | None
    portfolio_id: str | None
    context_owner: Any | None
    strategy_semantics_version: str | None


def build_swing_candidate_plan(
    *,
    policy: ExecutionPolicy,
    settings: Any,
    enabled: bool | None,
    evaluator: Callable[[SwingShadowInput], Mapping[str, Any]] | None,
    context_factory: SwingCandidateContextFactory | None,
    database_path: Path | None,
    portfolio_id: str | None,
    context_owner: Any | None,
) -> SwingCandidatePlan:
    """Resolve settings/policy identity without opening candidate resources."""

    configured = getattr(settings, "swing_candidate", None)
    semantics_version = getattr(configured, "strategy_semantics_version", None)
    if enabled is None:
        enabled = bool(getattr(configured, "enabled", False))
        if enabled:
            database_path = getattr(configured, "database_path", None)
            portfolio_id = getattr(configured, "portfolio_id", None)

    normalized_path = Path(database_path) if database_path is not None else None
    policy.assert_swing_candidate_identity(
        enabled=bool(enabled),
        database_path=normalized_path,
        portfolio_id=portfolio_id,
    )
    return SwingCandidatePlan(
        enabled=bool(enabled),
        evaluator=evaluator,
        context_factory=context_factory,
        database_path=normalized_path,
        portfolio_id=portfolio_id,
        context_owner=context_owner,
        strategy_semantics_version=semantics_version,
    )


def validate_swing_candidate_plan(
    plan: SwingCandidatePlan,
    *,
    legacy_database_path: Path,
) -> SwingCandidatePlan:
    """Validate candidate composition and bind its cleanup owner before I/O."""

    if plan.enabled and plan.evaluator is not None and plan.context_factory is not None:
        raise SwingCandidatePreflightError(
            "candidate evaluator and context factory cannot both be supplied"
        )
    if plan.enabled and plan.evaluator is None and plan.context_factory is None:
        raise SwingCandidatePreflightError(
            "enabled swing candidate shadow requires a strategy context factory"
        )
    if plan.enabled:
        if plan.database_path is None or plan.portfolio_id is None:
            raise SwingCandidatePreflightError(
                "enabled swing candidate shadow requires isolated database and portfolio identity"
            )
        if not plan.database_path.is_absolute() or plan.database_path == legacy_database_path:
            raise SwingCandidatePreflightError(
                "swing candidate database must be an isolated absolute path"
            )
        _assert_isolated_candidate_database(plan.database_path, legacy_database_path)
        if not plan.portfolio_id.strip():
            raise SwingCandidatePreflightError(
                "swing candidate portfolio identity is required"
            )
        owner = plan.context_owner
        if plan.context_factory is not None and owner is None:
            close = getattr(plan.context_factory, "close", None)
            if callable(close):
                owner = plan.context_factory
        return SwingCandidatePlan(
            enabled=plan.enabled,
            evaluator=plan.evaluator,
            context_factory=plan.context_factory,
            database_path=plan.database_path,
            portfolio_id=plan.portfolio_id,
            context_owner=owner,
            strategy_semantics_version=plan.strategy_semantics_version,
        )
    if any(
        value is not None
        for value in (
            plan.evaluator,
            plan.context_factory,
            plan.context_owner,
            plan.database_path,
            plan.portfolio_id,
        )
    ):
        raise SwingCandidatePreflightError(
            "disabled swing candidate shadow cannot receive candidate dependencies"
        )
    return plan


def _assert_isolated_candidate_database(
    candidate_path: Path,
    legacy_shadow_path: Path,
) -> None:
    """Reject aliases before a candidate evaluator can acquire a writer."""

    if candidate_path == legacy_shadow_path:
        raise SwingCandidatePreflightError(
            "candidate and legacy shadow databases must differ"
        )
    current = candidate_path
    while current != current.parent:
        if current.is_symlink():
            raise SwingCandidatePreflightError("candidate database aliases are forbidden")
        current = current.parent
    if candidate_path.exists():
        try:
            if candidate_path.stat().st_nlink != 1:
                raise SwingCandidatePreflightError(
                    "candidate database must have exactly one filesystem link"
                )
        except OSError:
            raise SwingCandidatePreflightError(
                "candidate database identity could not be verified"
            ) from None
