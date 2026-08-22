#!/usr/bin/env python3
"""Durable, idempotent host fence for the C* shadow schedule protocol.

The fence is intentionally independent of the worker container.  It records
the protocol generation and one occurrence identity before a worker side
effect is allowed.  All file writes are atomic and all state transitions are
forward-only; an APPLYING occurrence is never blindly replayed.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import fcntl
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys
from typing import Any, Iterator, Mapping, cast

try:
    from deploy.shadow_cstar_contract import (
        KST,
        RETENTION_DAYS,
        occurrence_identity,
        parse_utc_timestamp,
        validate_scheduler_payload,
        validate_session_lease,
        validate_scheduled_slot,
    )
except ModuleNotFoundError:  # standalone /usr/local/libexec installation
    from shadow_cstar_contract import (  # type: ignore[no-redef]
        KST,
        RETENTION_DAYS,
        occurrence_identity,
        parse_utc_timestamp,
        validate_scheduler_payload,
        validate_session_lease,
        validate_scheduled_slot,
    )


FENCE_SCHEMA_VERSION = 1
DEFAULT_STATE: dict[str, Any] = {
    "schema_version": FENCE_SCHEMA_VERSION,
    "authority": None,
    "sessions": {},
    "occurrences": {},
}
START_CUTOFF = (8, 58, 59)
STOP_CUTOFF = (15, 50, 59)


class FenceError(ValueError):
    """A fail-closed fence rejection or durable-state error."""


def _invalid() -> FenceError:
    return FenceError("invalid")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _format_now(value: datetime) -> str:
    if value.tzinfo is None:
        raise _invalid()
    return value.astimezone(timezone.utc).replace(microsecond=0).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _regular_single_link(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError:
        raise _invalid() from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise _invalid()
    return info


def _validate_state(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid()
    if set(value) != {"schema_version", "authority", "sessions", "occurrences"}:
        raise _invalid()
    if type(value.get("schema_version")) is not int or value["schema_version"] != FENCE_SCHEMA_VERSION:
        raise _invalid()
    if value.get("authority") is not None and not isinstance(value["authority"], Mapping):
        raise _invalid()
    if not isinstance(value.get("sessions"), Mapping) or not isinstance(value.get("occurrences"), Mapping):
        raise _invalid()
    sessions = value["sessions"]
    occurrences = value["occurrences"]
    if any(not isinstance(item, Mapping) for item in sessions.values()):
        raise _invalid()
    if any(not isinstance(item, Mapping) for item in occurrences.values()):
        raise _invalid()
    return {
        "schema_version": FENCE_SCHEMA_VERSION,
        "authority": dict(value["authority"]) if value["authority"] is not None else None,
        "sessions": {str(key): dict(item) for key, item in sessions.items()},
        "occurrences": {str(key): dict(item) for key, item in occurrences.items()},
    }


@contextmanager
def protocol_locks(
    fence_lock_path: Path,
    incumbent_lock_path: Path | None = None,
) -> Iterator[tuple[int, int | None]]:
    """Acquire C* fence then incumbent lock, never in the reverse order."""

    fence_lock_path.parent.mkdir(parents=True, exist_ok=True)
    fence_handle = fence_lock_path.open("a+b")
    try:
        fcntl.flock(fence_handle.fileno(), fcntl.LOCK_EX)
        if incumbent_lock_path is None:
            yield fence_handle.fileno(), None
            return
        incumbent_lock_path.parent.mkdir(parents=True, exist_ok=True)
        incumbent_handle = incumbent_lock_path.open("a+b")
        try:
            fcntl.flock(incumbent_handle.fileno(), fcntl.LOCK_EX)
            yield fence_handle.fileno(), incumbent_handle.fileno()
        finally:
            fcntl.flock(incumbent_handle.fileno(), fcntl.LOCK_UN)
            incumbent_handle.close()
    finally:
        fcntl.flock(fence_handle.fileno(), fcntl.LOCK_UN)
        fence_handle.close()


class ShadowScheduleFence:
    """Persistent state machine for one EC2 host."""

    def __init__(
        self,
        state_path: Path,
        *,
        lock_path: Path | None = None,
        incumbent_lock_path: Path | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.lock_path = lock_path or self.state_path.parent / "fence.lock"
        self.incumbent_lock_path = incumbent_lock_path

    def _read_unlocked(self) -> dict[str, object]:
        if not self.state_path.exists():
            return cast(dict[str, object], json.loads(json.dumps(DEFAULT_STATE)))
        _regular_single_link(self.state_path)
        try:
            raw = self.state_path.read_bytes()
            if len(raw) > 1_048_576 or not raw.endswith(b"\n"):
                raise _invalid()
            parsed = json.loads(
                raw.decode("utf-8"),
                parse_constant=lambda _: (_ for _ in ()).throw(_invalid()),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, FenceError):
            raise _invalid() from None
        return _validate_state(parsed)

    def _atomic_write_unlocked(self, value: Mapping[str, object]) -> None:
        checked = _validate_state(value)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.parent / f".{self.state_path.name}.{secrets.token_hex(12)}.tmp"
        data = json.dumps(
            checked,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
            directory_descriptor = os.open(self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            _regular_single_link(self.state_path)
            if self.state_path.read_bytes() != data:
                raise _invalid()
        except (OSError, FenceError):
            raise FenceError("durability failure") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                raise FenceError("durability failure") from None

    @contextmanager
    def _locked(self) -> Iterator[dict[str, object]]:
        with protocol_locks(self.lock_path, self.incumbent_lock_path):
            state = self._read_unlocked()
            yield state

    def configure_authority(
        self,
        *,
        generation: str,
        protocol_sha256: str,
        armed_at: str,
        clock_owner: str = "eventbridge-scheduler",
    ) -> dict[str, object]:
        if (
            not generation.startswith("cstar-g")
            or len(protocol_sha256) != 64
            or clock_owner != "eventbridge-scheduler"
        ):
            raise _invalid()
        if any(character not in "0123456789abcdef" for character in protocol_sha256):
            raise _invalid()
        parse_utc_timestamp(armed_at)
        authority = {
            "clock_owner": clock_owner,
            "active_schedule_generation": generation,
            "protocol_sha256": protocol_sha256,
            "armed_at": armed_at,
        }
        with self._locked() as state:
            current = state["authority"]
            if current is not None and current != authority:
                raise FenceError("authority mismatch")
            state["authority"] = authority
            self._atomic_write_unlocked(state)
            return dict(authority)

    def pin_session(self, lease: Mapping[str, object]) -> dict[str, object]:
        validated = validate_session_lease(lease)
        key = str(validated["session_date_kst"])
        with self._locked() as state:
            sessions = cast(dict[str, dict[str, object]], state["sessions"])
            existing = sessions.get(key)
            if existing is not None and existing != validated:
                raise FenceError("session lease mismatch")
            sessions[key] = dict(validated)
            self._atomic_write_unlocked(state)
            return dict(validated)

    def claim(
        self,
        scheduler_payload: Mapping[str, object],
        *,
        release_id: str,
        observed_at: datetime | None = None,
    ) -> dict[str, object]:
        payload = validate_scheduler_payload(scheduler_payload)
        validate_scheduled_slot(payload)
        identity = occurrence_identity(payload)
        observed = (observed_at or _utc_now()).astimezone(KST)
        scheduled = parse_utc_timestamp(payload["scheduled_time"]).astimezone(KST)
        cutoff_hour, cutoff_minute, cutoff_second = START_CUTOFF if payload["phase"] == "start" else STOP_CUTOFF
        cutoff = scheduled.replace(hour=cutoff_hour, minute=cutoff_minute, second=cutoff_second)
        if observed > cutoff:
            raise FenceError("late trigger")
        with self._locked() as state:
            authority = state["authority"]
            if not isinstance(authority, Mapping) or authority.get("active_schedule_generation") != payload["schedule_generation"]:
                return self._record_rejection(state, identity.occurrence_id, payload, release_id, "STALE_GENERATION")
            session_key = identity.session_date_kst
            sessions = cast(dict[str, dict[str, object]], state["sessions"])
            session = sessions.get(session_key)
            if session is None:
                return self._record_rejection(state, identity.occurrence_id, payload, release_id, "NO_SESSION")
            if session.get("release_id") != release_id or session.get("activation_id") != identity.activation_id:
                return self._record_rejection(state, identity.occurrence_id, payload, release_id, "LEASE_MISMATCH")
            occurrences = cast(dict[str, dict[str, object]], state["occurrences"])
            existing = occurrences.get(identity.occurrence_id)
            if existing is not None:
                if existing.get("state") == "TERMINAL":
                    return self._receipt(existing, duplicate=True)
                return self._receipt(existing, duplicate=False)
            occurrence = {
                "occurrence_id": identity.occurrence_id,
                "session_date_kst": identity.session_date_kst,
                "activation_id": identity.activation_id,
                "phase": identity.phase,
                "schedule_generation": payload["schedule_generation"],
                "schedule_arn": payload["schedule_arn"],
                "scheduled_time": payload["scheduled_time"],
                "release_id": release_id,
                "state": "CLAIMED",
                "claim_count": 1,
                "claimed_at": _format_now(_utc_now()),
            }
            occurrences[identity.occurrence_id] = occurrence
            self._atomic_write_unlocked(state)
            return self._receipt(occurrence, duplicate=False)

    def apply(self, occurrence_id: str) -> dict[str, object]:
        with self._locked() as state:
            occurrence = self._get_occurrence(state, occurrence_id)
            if occurrence["state"] == "CLAIMED":
                occurrence["state"] = "APPLYING"
                occurrence["applying_at"] = _format_now(_utc_now())
                self._atomic_write_unlocked(state)
            elif occurrence["state"] != "APPLYING":
                raise FenceError("effect not claimable")
            return self._receipt(occurrence, duplicate=False)

    def effect_observed(self, occurrence_id: str, *, effect_id: str) -> dict[str, object]:
        if not effect_id or len(effect_id.encode("utf-8")) > 256:
            raise _invalid()
        with self._locked() as state:
            occurrence = self._get_occurrence(state, occurrence_id)
            if occurrence["state"] == "EFFECT_OBSERVED":
                if occurrence.get("effect_id") != effect_id:
                    raise FenceError("effect mismatch")
                return self._receipt(occurrence, duplicate=False)
            if occurrence["state"] != "APPLYING":
                raise FenceError("effect not applying")
            occurrence["state"] = "EFFECT_OBSERVED"
            occurrence["effect_id"] = effect_id
            occurrence["effect_observed_at"] = _format_now(_utc_now())
            self._atomic_write_unlocked(state)
            return self._receipt(occurrence, duplicate=False)

    def terminal(self, occurrence_id: str) -> dict[str, object]:
        with self._locked() as state:
            occurrence = self._get_occurrence(state, occurrence_id)
            if occurrence["state"] == "TERMINAL":
                return self._receipt(occurrence, duplicate=True)
            if occurrence["state"] != "EFFECT_OBSERVED":
                raise FenceError("effect not observed")
            occurrence["state"] = "TERMINAL"
            occurrence["terminal_at"] = _format_now(_utc_now())
            occurrence["retention_days"] = RETENTION_DAYS
            self._atomic_write_unlocked(state)
            return self._receipt(occurrence, duplicate=False)

    def ambiguous(self, occurrence_id: str, *, reason: str) -> dict[str, object]:
        if not reason or len(reason.encode("utf-8")) > 256:
            raise _invalid()
        with self._locked() as state:
            occurrence = self._get_occurrence(state, occurrence_id)
            if occurrence["state"] == "TERMINAL":
                return self._receipt(occurrence, duplicate=True)
            if occurrence["state"] not in {"CLAIMED", "APPLYING", "EFFECT_OBSERVED"}:
                raise FenceError("occurrence closed")
            occurrence["state"] = "AMBIGUOUS"
            occurrence["ambiguous_reason"] = reason
            occurrence["ambiguous_at"] = _format_now(_utc_now())
            self._atomic_write_unlocked(state)
            return self._receipt(occurrence, duplicate=False)

    @contextmanager
    def worker_effect(self, occurrence_id: str, *, phase: str) -> Iterator[int | None]:
        """Hold fence->incumbent locks across the worker side effect."""

        with protocol_locks(self.lock_path, self.incumbent_lock_path) as (
            _fence_fd,
            incumbent_fd,
        ):
            state = self._read_unlocked()
            occurrence = self._get_occurrence(state, occurrence_id)
            if occurrence["state"] != "CLAIMED":
                raise FenceError("effect not claimable")
            authority = state["authority"]
            if (
                not isinstance(authority, Mapping)
                or authority.get("active_schedule_generation")
                != occurrence.get("schedule_generation")
            ):
                raise FenceError("authority changed")
            occurrence["state"] = "APPLYING"
            occurrence["applying_at"] = _format_now(_utc_now())
            self._atomic_write_unlocked(state)
            try:
                yield incumbent_fd
            except BaseException as error:
                occurrence["state"] = "AMBIGUOUS"
                occurrence["ambiguous_reason"] = type(error).__name__
                occurrence["ambiguous_at"] = _format_now(_utc_now())
                self._atomic_write_unlocked(state)
                raise
            occurrence["state"] = "EFFECT_OBSERVED"
            occurrence["effect_id"] = (
                f"{occurrence['activation_id']}:{phase}"
            )
            occurrence["effect_observed_at"] = _format_now(_utc_now())
            if phase == "stop":
                occurrence["state"] = "TERMINAL"
                occurrence["terminal_at"] = _format_now(_utc_now())
                occurrence["retention_days"] = RETENTION_DAYS
            self._atomic_write_unlocked(state)

    def read(self) -> dict[str, object]:
        with self._locked() as state:
            return cast(dict[str, object], json.loads(json.dumps(state)))

    @staticmethod
    def _get_occurrence(state: Mapping[str, object], occurrence_id: str) -> dict[str, object]:
        occurrences = cast(dict[str, dict[str, object]], state["occurrences"])
        if not isinstance(occurrence_id, str) or occurrence_id not in occurrences:
            raise FenceError("unknown occurrence")
        return occurrences[occurrence_id]

    def _record_rejection(
        self,
        state: dict[str, object],
        occurrence_id: str,
        payload: Mapping[str, object],
        release_id: str,
        reason: str,
    ) -> dict[str, object]:
        occurrences = cast(dict[str, dict[str, object]], state["occurrences"])
        existing = occurrences.get(occurrence_id)
        if existing is not None:
            return self._receipt(existing, duplicate=existing.get("state") == "TERMINAL")
        identity = occurrence_identity(payload)
        occurrence = {
            "occurrence_id": occurrence_id,
            "session_date_kst": identity.session_date_kst,
            "activation_id": identity.activation_id,
            "phase": identity.phase,
            "schedule_generation": payload["schedule_generation"],
            "schedule_arn": payload["schedule_arn"],
            "scheduled_time": payload["scheduled_time"],
            "release_id": release_id,
            "state": "REJECTED",
            "rejection_reason": reason,
            "rejected_at": _format_now(_utc_now()),
        }
        occurrences[occurrence_id] = occurrence
        self._atomic_write_unlocked(state)
        return self._receipt(occurrence, duplicate=False)

    @staticmethod
    def _receipt(occurrence: Mapping[str, object], *, duplicate: bool) -> dict[str, object]:
        return {
            "occurrence_id": occurrence["occurrence_id"],
            "session_date_kst": occurrence["session_date_kst"],
            "activation_id": occurrence["activation_id"],
            "phase": occurrence["phase"],
            "release_id": occurrence["release_id"],
            "state": occurrence["state"],
            "effect_id": occurrence.get("effect_id"),
            "duplicate": duplicate,
        }


def _activation_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["activate"])
    parser.add_argument("--phase", choices=["start", "stop"], required=True)
    parser.add_argument("--schedule-generation", required=True)
    parser.add_argument("--schedule-arn", required=True)
    parser.add_argument("--scheduled-time", required=True)
    parser.add_argument("--occurrence-id", required=True)
    parser.add_argument("--session-date-kst", required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--desired-state", choices=["continuous", "stop"], required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--compose-shadow-sha256", required=True)
    parser.add_argument("--expected-worker-sha256", required=True)
    parser.add_argument("--expected-validator-sha256", required=True)
    parser.add_argument("--expected-shadow-document-sha256", required=True)
    parser.add_argument("--expected-instance-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--state-path", default="/var/lib/kiwoom-stock/shadow-schedule/fence.json")
    parser.add_argument("--lock-path", default="/run/lock/kiwoom-stock-shadow-fence.lock")
    parser.add_argument("--incumbent-lock-path", default="/run/lock/kiwoom-stock-shadow.lock")
    parser.add_argument("--worker-path", default="/usr/local/sbin/kiwoom-shadow-worker")
    args = parser.parse_args(argv)
    if args.phase == "start" and args.desired_state != "continuous":
        parser.error("start requires continuous")
    if args.phase == "stop" and args.desired_state != "stop":
        parser.error("stop requires stop")
    if args.expected_instance_id != "i-0e42e09d6c087ba29" or args.region != "ap-northeast-2":
        parser.error("instance or region is not approved")
    payload = {
        "schema_version": 1,
        "phase": args.phase,
        "schedule_generation": args.schedule_generation,
        "schedule_arn": args.schedule_arn,
        "scheduled_time": args.scheduled_time,
        "execution_id": f"host-{args.occurrence_id[:16]}",
        "attempt_number": "0",
    }
    identity = occurrence_identity(payload)
    if (
        args.occurrence_id != identity.occurrence_id
        or args.session_date_kst != identity.session_date_kst
        or args.activation_id != identity.activation_id
    ):
        parser.error("occurrence/session identity mismatch")
    fence = ShadowScheduleFence(
        Path(args.state_path),
        lock_path=Path(args.lock_path),
        incumbent_lock_path=Path(args.incumbent_lock_path),
    )
    lease = {
        "schema_version": 1,
        "session_date_kst": args.session_date_kst,
        "activation_id": args.activation_id,
        "release_id": args.release_id,
        "schedule_generation": args.schedule_generation,
    }
    fence.pin_session(lease)
    receipt = fence.claim(payload, release_id=args.release_id)
    if receipt["occurrence_id"] != args.occurrence_id:
        parser.error("occurrence identity mismatch")
    if receipt["state"] == "TERMINAL":
        print(json.dumps(receipt, sort_keys=True))
        return 0
    if receipt["state"] != "CLAIMED":
        print(json.dumps(receipt, sort_keys=True), file=sys.stderr)
        return 75
    worker_args = [
        args.worker_path,
        "--desired-state", args.desired_state,
        "--image", args.image,
        "--source-sha", args.source_sha,
        "--activation-id", args.activation_id,
        "--expected-worker-sha256", args.expected_worker_sha256,
        "--expected-validator-sha256", args.expected_validator_sha256,
        "--expected-shadow-document-sha256", args.expected_shadow_document_sha256,
        "--expected-instance-id", args.expected_instance_id,
        "--region", args.region,
    ]
    if args.phase == "start":
        worker_args.extend(["--compose-shadow-sha256", args.compose_shadow_sha256])
    try:
        with fence.worker_effect(args.occurrence_id, phase=args.phase) as incumbent_fd:
            pass_fds = () if incumbent_fd is None else (incumbent_fd,)
            if incumbent_fd is not None:
                worker_args.extend(["--inherited-lock-fd", str(incumbent_fd)])
            completed = subprocess.run(
                worker_args,
                check=False,
                pass_fds=pass_fds,
            )
            if completed.returncode != 0:
                raise FenceError(f"worker_exit_{completed.returncode}")
    except FenceError as error:
        print(f"shadow schedule fence: {error}", file=sys.stderr)
        return 1
    state = fence.read()
    occurrences = cast(dict[str, dict[str, object]], state["occurrences"])
    print(json.dumps(occurrences[args.occurrence_id], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_activation_cli(sys.argv[1:]))
