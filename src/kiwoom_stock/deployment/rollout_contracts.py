"""Immutable value contracts shared by the shadow rollout command plane."""

from __future__ import annotations

from dataclasses import dataclass
import re


SOURCE_RE = re.compile(r"[0-9a-f]{40}")
HASH_RE = re.compile(r"[0-9a-f]{64}")
ID_RE = re.compile(r"[1-9][0-9]{0,19}")


class RolloutError(RuntimeError):
    """An operator-safe rollout failure category."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class RolloutTuple:
    """Immutable rollout identity (Git SHA, content hashes, positive attempt)."""

    source_sha: str
    worker_sha256: str
    validator_sha256: str
    shadow_document_sha256: str
    shadow_document_raw_sha256: str
    rollout_document_sha256: str
    rollout_attempt_id: str
    rollout_document_version: str = ""
    rollout_document_canonical_sha256: str = ""

    def validate(self) -> None:
        if SOURCE_RE.fullmatch(self.source_sha) is None:
            raise RolloutError("source_sha_invalid")
        for value in (
            self.worker_sha256,
            self.validator_sha256,
            self.shadow_document_sha256,
            self.shadow_document_raw_sha256,
            self.rollout_document_sha256,
        ):
            if HASH_RE.fullmatch(value) is None:
                raise RolloutError("hash_invalid")
        if ID_RE.fullmatch(self.rollout_attempt_id) is None:
            raise RolloutError("rollout_attempt_id_invalid")
        if re.fullmatch(r"[1-9][0-9]*", self.rollout_document_version) is None:
            raise RolloutError("rollout_document_version_invalid")
        if (
            HASH_RE.fullmatch(self.rollout_document_canonical_sha256) is None
            or self.rollout_document_canonical_sha256 == "0" * 64
        ):
            raise RolloutError("hash_invalid")


@dataclass(frozen=True)
class AttestedRolloutDocument:
    """Exact active SSM document identity bound to one rollout execution."""

    version: str
    canonical_sha256: str
