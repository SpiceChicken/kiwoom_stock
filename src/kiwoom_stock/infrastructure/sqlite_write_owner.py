"""Process-local SQLite writer ownership with fail-closed file locking."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported runtime is Linux
    fcntl = None  # type: ignore[assignment]


class SqliteWriterOwnershipError(RuntimeError):
    """The exact SQLite file is already owned by another writable ledger."""


class SqliteWriterOwner:
    """Hold one non-blocking exclusive advisory lock for a writable DB file."""

    def __init__(self, database_path: str | os.PathLike[str]) -> None:
        self.path = Path(os.path.abspath(os.fspath(database_path)))
        self._descriptor: Optional[int] = None
        self._acquire()

    def _acquire(self) -> None:
        if fcntl is None:
            raise SqliteWriterOwnershipError(
                "SQLite writer ownership requires Linux file locking"
            )
        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
                0o660,
            )
        except OSError as error:
            raise SqliteWriterOwnershipError(
                "SQLite writer ownership file could not be opened"
            ) from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EACCES, errno.EAGAIN):
                raise SqliteWriterOwnershipError(
                    "SQLite database already has a writable owner"
                ) from None
            raise SqliteWriterOwnershipError(
                "SQLite writer ownership could not be acquired"
            ) from error
        self._descriptor = descriptor

    @property
    def closed(self) -> bool:
        return self._descriptor is None

    def close(self) -> None:
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
