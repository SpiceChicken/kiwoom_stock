"""P4 episode transition boundary built on the frozen G0 episode reducer."""

from __future__ import annotations

from dataclasses import dataclass

from .swing_contracts import (
    AdmissionEvent,
    ContractError,
    EpisodeRearmEvidence,
    EpisodeSnapshot,
    reduce_episode,
)


EPISODE_SEMANTIC_VERSION = "swing-v1"


@dataclass(frozen=True)
class EpisodeTransition:
    """One typed, deterministic event plus optional re-arm evidence."""

    event: AdmissionEvent
    rearm_evidence: EpisodeRearmEvidence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, AdmissionEvent):
            raise ContractError("episode transition requires AdmissionEvent")
        if self.rearm_evidence is not None and not isinstance(self.rearm_evidence, EpisodeRearmEvidence):
            raise ContractError("episode re-arm evidence must be typed")


def reduce_transition(
    snapshot: EpisodeSnapshot,
    transition: EpisodeTransition,
) -> EpisodeSnapshot:
    """Reduce one transition and normalize the state result to a snapshot."""

    result = reduce_episode(
        snapshot,
        transition.event,
        current_version=EPISODE_SEMANTIC_VERSION,
        evidence=transition.rearm_evidence,
    )
    if not isinstance(result, EpisodeSnapshot):
        raise ContractError("episode reducer returned an unbound state")
    return result
