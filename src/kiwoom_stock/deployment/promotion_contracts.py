"""Immutable value contracts for the promotion command plane."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Candidate:
    """Immutable protected release tuple."""

    source_sha: str
    image_digest: str
    build_run_id: int


@dataclass(frozen=True)
class ArtifactContract:
    artifact_id: int
    size_bytes: int
    digest: str
    build_job_id: int


@dataclass(frozen=True)
class ReleaseContract:
    image_size_mib: int
    compose_sha256: str
    compose_prod_sha256: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class BinaryCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
