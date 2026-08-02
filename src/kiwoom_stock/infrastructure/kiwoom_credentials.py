"""POSIX mounted-file adapter for Kiwoom credentials."""

from __future__ import annotations

import os
from pathlib import Path
import stat
from typing import Final

from kiwoom_stock.application.credentials import (
    CredentialProviderError,
    KiwoomClientCredentials,
    SensitiveText,
)


APP_KEY_FILE: Final = "KIWOOM_APP_KEY"
SECRET_KEY_FILE: Final = "KIWOOM_SECRET_KEY"
MATERIALIZED_APP_KEY_FILE: Final = "app-key"
MATERIALIZED_SECRET_KEY_FILE: Final = "secret-key"
MAX_CREDENTIAL_BYTES: Final = 8 * 1024


def credential_repository_boundary() -> Path:
    """Find a source checkout boundary, falling back conservatively to cwd."""

    for start in (Path(__file__).resolve(), Path.cwd()):
        candidate = start if start.is_dir() else start.parent
        for parent in (candidate, *candidate.parents):
            if (parent / ".git").exists():
                return parent
    return Path.cwd()


class StrictFileCredentialProvider:
    """Read exactly two hardened files from an external absolute directory.

    This adapter is POSIX-only because its guarantees depend on directory file
    descriptors, ``O_NOFOLLOW``, ownership, link counts, and Unix mode bits.
    """

    def __init__(
        self,
        credentials_dir: Path,
        *,
        repository_root: Path | None = None,
        file_names: tuple[str, str] = (APP_KEY_FILE, SECRET_KEY_FILE),
    ) -> None:
        if os.name != "posix":
            raise CredentialProviderError(
                "strict file credentials require a POSIX runtime"
            )
        if file_names not in (
            (APP_KEY_FILE, SECRET_KEY_FILE),
            (MATERIALIZED_APP_KEY_FILE, MATERIALIZED_SECRET_KEY_FILE),
        ):
            raise CredentialProviderError(
                "credential file names are not approved"
            )
        self._file_names = file_names
        if not credentials_dir.is_absolute():
            raise CredentialProviderError("credential directory must be absolute")
        required_flags = ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        if any(not hasattr(os, name) for name in required_flags):
            raise CredentialProviderError(
                "secure descriptor-relative credential access is unsupported"
            )
        self._reject_symlink_components(credentials_dir)
        try:
            supplied_metadata = credentials_dir.lstat()
        except OSError as exc:
            raise CredentialProviderError(
                "credential directory must be an existing directory"
            ) from exc
        if stat.S_ISLNK(supplied_metadata.st_mode):
            raise CredentialProviderError(
                "credential directory must not be a symbolic link"
            )
        constructor_fd = self._open_directory_path(credentials_dir)
        try:
            opened_metadata = os.fstat(constructor_fd)
            self._validate_directory(opened_metadata)
            if self._stable_identity(
                supplied_metadata
            ) != self._stable_identity(opened_metadata):
                raise CredentialProviderError(
                    "credential directory changed during construction"
                )
            try:
                resolved_dir = credentials_dir.resolve(strict=True)
                resolved_metadata = resolved_dir.stat()
            except OSError as exc:
                raise CredentialProviderError(
                    "credential directory must be an existing directory"
                ) from exc
            if self._stable_identity(
                resolved_metadata
            ) != self._stable_identity(opened_metadata):
                raise CredentialProviderError(
                    "credential directory path changed during construction"
                )
        finally:
            os.close(constructor_fd)

        root = (
            repository_root
            if repository_root is not None
            else credential_repository_boundary()
        )
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise CredentialProviderError("repository boundary is unavailable") from exc
        if resolved_dir == resolved_root or resolved_root in resolved_dir.parents:
            raise CredentialProviderError(
                "credential directory must be outside the repository"
            )

        self._credentials_dir = resolved_dir
        self._directory_identity = self._stable_identity(supplied_metadata)

    def load(self) -> KiwoomClientCredentials:
        """Open the directory and both files descriptor-relatively, once."""

        directory_fd = self._open_directory_path(self._credentials_dir)
        try:
            directory_before = os.fstat(directory_fd)
            self._validate_directory(directory_before)
            if self._stable_identity(directory_before) != self._directory_identity:
                raise CredentialProviderError(
                    "credential directory identity changed before load"
                )
            app_name, secret_name = self._file_names
            app_fd = self._open_file(directory_fd, app_name)
            try:
                secret_fd = self._open_file(directory_fd, secret_name)
                try:
                    app_generation = self._initial_file_generation(
                        app_fd,
                        app_name,
                    )
                    secret_generation = self._initial_file_generation(
                        secret_fd,
                        secret_name,
                    )
                    app_key = self._read_open_file(
                        directory_fd,
                        app_fd,
                        app_name,
                        app_generation,
                    )
                    secret_key = self._read_open_file(
                        directory_fd,
                        secret_fd,
                        secret_name,
                        secret_generation,
                    )
                    self._validate_file_pair_generation(
                        directory_fd,
                        (
                            (app_fd, app_name, app_generation),
                            (secret_fd, secret_name, secret_generation),
                        ),
                    )
                    try:
                        directory_after = os.fstat(directory_fd)
                    except OSError as exc:
                        raise CredentialProviderError(
                            "credential directory changed during pair read"
                        ) from exc
                    if self._generation_identity(
                        directory_before
                    ) != self._generation_identity(directory_after):
                        raise CredentialProviderError(
                            "credential directory changed during pair read"
                        )
                    try:
                        current_path = os.stat(
                            self._credentials_dir,
                            follow_symlinks=False,
                        )
                    except OSError as exc:
                        raise CredentialProviderError(
                            "credential directory was replaced during load"
                        ) from exc
                    if self._stable_identity(
                        current_path
                    ) != self._stable_identity(directory_before):
                        raise CredentialProviderError(
                            "credential directory was replaced during load"
                        )
                finally:
                    os.close(secret_fd)
            finally:
                os.close(app_fd)
        finally:
            os.close(directory_fd)
        return KiwoomClientCredentials(
            app_key=SensitiveText(app_key),
            secret_key=SensitiveText(secret_key),
        )

    @staticmethod
    def _validate_directory(metadata: os.stat_result) -> None:
        if not stat.S_ISDIR(metadata.st_mode):
            raise CredentialProviderError("credential source must be a directory")
        if metadata.st_uid not in (0, os.geteuid()):
            raise CredentialProviderError(
                "credential directory has an untrusted owner"
            )
        if stat.S_IMODE(metadata.st_mode) & 0o022:
            raise CredentialProviderError(
                "credential directory must not be group/world writable"
            )

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            try:
                metadata = current.lstat()
            except OSError as exc:
                raise CredentialProviderError(
                    "credential directory path is unavailable"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise CredentialProviderError(
                    "credential directory path must not contain symbolic links"
                )

    @staticmethod
    def _open_directory_path(path: Path) -> int:
        """Traverse every directory component with no-follow descriptor opens."""

        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            current_fd = os.open(path.anchor, flags)
        except OSError as exc:
            raise CredentialProviderError(
                "credential directory failed secure open"
            ) from exc
        try:
            for part in path.parts[1:]:
                try:
                    next_fd = os.open(part, flags, dir_fd=current_fd)
                except OSError as exc:
                    raise CredentialProviderError(
                        "credential directory path failed secure traversal"
                    ) from exc
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except BaseException:
            os.close(current_fd)
            raise

    @staticmethod
    def _stable_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IFMT(metadata.st_mode),
            metadata.st_uid,
            metadata.st_gid,
        )

    @staticmethod
    def _generation_identity(metadata: os.stat_result) -> tuple[int, ...]:
        return (
            *StrictFileCredentialProvider._stable_identity(metadata),
            metadata.st_nlink,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
        )

    @staticmethod
    def _open_file(directory_fd: int, name: str) -> int:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            return os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise CredentialProviderError(f"{name} failed secure open") from exc

    @staticmethod
    def _initial_file_generation(file_fd: int, name: str) -> tuple[int, ...]:
        try:
            metadata = os.fstat(file_fd)
        except OSError as exc:
            raise CredentialProviderError(
                f"{name} failed initial generation capture"
            ) from exc
        StrictFileCredentialProvider._validate_file(name, metadata)
        return StrictFileCredentialProvider._generation_identity(metadata)

    @staticmethod
    def _read_open_file(
        directory_fd: int,
        file_fd: int,
        name: str,
        expected_generation: tuple[int, ...],
    ) -> str:
        try:
            metadata_before = os.fstat(file_fd)
        except OSError as exc:
            raise CredentialProviderError(
                f"{name} changed before credential read"
            ) from exc
        if (
            StrictFileCredentialProvider._generation_identity(metadata_before)
            != expected_generation
        ):
            raise CredentialProviderError(
                f"{name} changed before credential read"
            )
        payload = StrictFileCredentialProvider._read_bounded(file_fd, name)
        try:
            metadata_after = os.fstat(file_fd)
            current_path = os.stat(
                name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise CredentialProviderError(
                f"{name} changed during credential read"
            ) from exc
        if expected_generation != StrictFileCredentialProvider._generation_identity(
            metadata_after
        ) or expected_generation != StrictFileCredentialProvider._generation_identity(
            current_path
        ):
            raise CredentialProviderError(f"{name} changed during credential read")
        try:
            value = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CredentialProviderError(f"{name} must be valid UTF-8") from exc
        if value.endswith("\n"):
            value = value[:-1]
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise CredentialProviderError(
                f"{name} must contain one non-empty single-line value"
            )
        return value

    @staticmethod
    def _validate_file_pair_generation(
        directory_fd: int,
        files: tuple[
            tuple[int, str, tuple[int, ...]],
            tuple[int, str, tuple[int, ...]],
        ],
    ) -> None:
        """Revalidate both opened files and both directory entries as one pair."""

        observed: list[
            tuple[str, tuple[int, ...], tuple[int, ...], tuple[int, ...]]
        ] = []
        try:
            for file_fd, name, expected in files:
                descriptor_generation = (
                    StrictFileCredentialProvider._generation_identity(
                        os.fstat(file_fd)
                    )
                )
                entry_generation = (
                    StrictFileCredentialProvider._generation_identity(
                        os.stat(
                            name,
                            dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                    )
                )
                observed.append(
                    (
                        name,
                        expected,
                        descriptor_generation,
                        entry_generation,
                    )
                )
        except OSError as exc:
            raise CredentialProviderError(
                "credential file pair changed after read"
            ) from exc
        for name, expected, descriptor_generation, entry_generation in observed:
            if expected != descriptor_generation or expected != entry_generation:
                raise CredentialProviderError(
                    f"{name} changed after credential pair read"
                )

    @staticmethod
    def _validate_file(name: str, metadata: os.stat_result) -> None:
        if not stat.S_ISREG(metadata.st_mode):
            raise CredentialProviderError(f"{name} must be a regular file")
        if metadata.st_nlink != 1:
            raise CredentialProviderError(f"{name} must have exactly one hard link")
        permissions = stat.S_IMODE(metadata.st_mode)
        owner_only = metadata.st_uid == os.geteuid() and permissions == 0o400
        root_group = (
            metadata.st_uid == 0
            and metadata.st_gid == os.getegid()
            and permissions == 0o440
        )
        if not (owner_only or root_group):
            raise CredentialProviderError(
                f"{name} must be owned and mode 0400 or root:effective-group 0440"
            )
        if metadata.st_size > MAX_CREDENTIAL_BYTES:
            raise CredentialProviderError(f"{name} exceeds the size limit")

    @staticmethod
    def _read_bounded(file_fd: int, name: str) -> bytes:
        chunks: list[bytes] = []
        remaining = MAX_CREDENTIAL_BYTES + 1
        try:
            while remaining:
                chunk = os.read(file_fd, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        except OSError as exc:
            raise CredentialProviderError(
                f"{name} failed bounded credential read"
            ) from exc
        payload = b"".join(chunks)
        if len(payload) > MAX_CREDENTIAL_BYTES:
            raise CredentialProviderError(f"{name} exceeds the size limit")
        return payload
