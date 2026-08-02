"""Non-blocking process ownership for one shadow worker and volume."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path


class ShadowProcessLockError(RuntimeError):
    """The shadow process lock could not be acquired or released."""


class ShadowProcessAlreadyRunning(ShadowProcessLockError):
    """Another process owns the bounded shadow capability."""


@dataclass
class ShadowProcessLock:
    """An advisory, non-blocking exclusive lock with idempotent release."""

    path: str | Path
    _fd: int | None = None
    release_error: str | None = None

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if not self.path.is_absolute() or self.path == self.path.root:
            raise ValueError("shadow process lock path must be an absolute file path")

    def acquire(self) -> None:
        if self._fd is not None:
            return
        try:
            fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as error:
            raise ShadowProcessLockError("shadow process lock is unavailable") from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise ShadowProcessAlreadyRunning(
                    "another shadow process owns the lock"
                ) from None
            raise ShadowProcessLockError("shadow process lock acquisition failed") from error
        self._fd = fd

    def release(self) -> None:
        fd = self._fd
        if fd is None:
            return
        failure: ShadowProcessLockError | None = None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as error:
                failure = ShadowProcessLockError(
                    "shadow process lock release failed"
                )
                self.release_error = type(error).__name__
        finally:
            try:
                os.close(fd)
            except OSError as error:
                if failure is None:
                    failure = ShadowProcessLockError(
                        "shadow process lock descriptor close failed"
                    )
                self.release_error = type(error).__name__
            self._fd = None
        if failure is not None:
            raise failure

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> "ShadowProcessLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        try:
            self.release()
        except ShadowProcessLockError:
            # Never replace the operation/cleanup exception while unwinding.
            if _exc_type is None:
                raise
