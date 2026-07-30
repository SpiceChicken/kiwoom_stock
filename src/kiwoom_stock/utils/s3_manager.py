import logging
import os
import stat
from datetime import date
from typing import BinaryIO, Callable, Optional, Protocol

import boto3
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from kiwoom_stock.application.ports import (
    ArchiveReceipt,
    ArchiveStatus,
    ArchiveTargetReceipt,
    FilesystemIdentity,
)

logger = logging.getLogger(__name__)


class _S3Client(Protocol):
    def upload_file(self, local_path: str, bucket_name: str, object_key: str) -> None:
        """Upload one local pathname to object storage."""

    def upload_fileobj(
        self,
        file_obj: BinaryIO,
        bucket_name: str,
        object_key: str,
    ) -> None:
        """Upload one already-open file object to object storage."""


_TargetHook = Callable[[str], None]
_EXPECTED_UPLOAD_ERRORS = (ClientError, S3UploadFailedError)
_ARCHIVE_DIR_FD_SUPPORTED = all(
    function in os.supports_dir_fd for function in (os.open, os.stat)
)


def _filesystem_identity(metadata: os.stat_result) -> FilesystemIdentity:
    return FilesystemIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )


def _validate_sync_command(target_date: str, source_dir: str) -> str:
    if not isinstance(target_date, str) or not target_date:
        raise ValueError("target_date must use YYYY-MM-DD")
    try:
        parsed_date = date.fromisoformat(target_date)
    except ValueError as error:
        raise ValueError("target_date must use YYYY-MM-DD") from error
    if parsed_date.isoformat() != target_date:
        raise ValueError("target_date must use YYYY-MM-DD")
    if not isinstance(source_dir, str) or not source_dir.strip():
        raise ValueError("source_dir must not be blank")
    return os.path.abspath(os.path.normpath(source_dir))


def _secure_directory_flags() -> int:
    required_names = ("O_DIRECTORY", "O_NOFOLLOW")
    if (
        any(not hasattr(os, name) for name in required_names)
        or not _ARCHIVE_DIR_FD_SUPPORTED
    ):
        raise RuntimeError("secure descriptor-relative archive is not supported")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _secure_file_flags() -> int:
    if not hasattr(os, "O_NOFOLLOW"):
        raise RuntimeError("secure no-follow archive is not supported")
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class S3Manager:
    def __init__(
        self,
        bucket_name: str,
        *,
        s3_client: Optional[_S3Client] = None,
        _before_target_open: Optional[_TargetHook] = None,
        _after_target_upload: Optional[_TargetHook] = None,
    ):
        self.bucket_name = bucket_name
        # EC2에 연결된 IAM Role/provider chain은 boto3가 처리합니다.
        self.s3_client = s3_client if s3_client is not None else boto3.client("s3")
        # Deterministic race seams used only by local safety tests.
        self._before_target_open = _before_target_open
        self._after_target_upload = _after_target_upload

    def upload_file(self, local_path: str, s3_key: str) -> bool:
        """Keep the legacy pathname upload surface and managed-failure bool result."""
        try:
            self.s3_client.upload_file(local_path, self.bucket_name, s3_key)
            return True
        except _EXPECTED_UPLOAD_ERRORS as error:
            logger.error(f"[S3 Upload] 실패 {local_path}: {error}")
            return False

    def _upload_fileobj(
        self,
        file_obj: BinaryIO,
        local_path: str,
        object_key: str,
    ) -> bool:
        try:
            self.s3_client.upload_fileobj(file_obj, self.bucket_name, object_key)
            return True
        except _EXPECTED_UPLOAD_ERRORS as error:
            logger.error(f"[S3 Upload] 실패 {local_path}: {error}")
            return False

    def sync_daily_outputs(self, target_date: str, source_dir: str) -> ArchiveReceipt:
        canonical_source = _validate_sync_command(target_date, source_dir)
        if os.path.realpath(canonical_source) != canonical_source:
            logger.warning(f"[S3 Sync] symlink 경로는 허용하지 않습니다: {source_dir}")
            return ArchiveReceipt(
                status=ArchiveStatus.SOURCE_MISSING,
                target_date=target_date,
                source_dir=canonical_source,
                detail="source directory path must not traverse a symlink",
            )
        source_parent = os.path.dirname(canonical_source)
        source_name = os.path.basename(canonical_source)

        parent_fd: Optional[int] = None
        source_fd: Optional[int] = None
        try:
            directory_flags = _secure_directory_flags()
            parent_fd = os.open(source_parent, directory_flags)
            source_fd = os.open(source_name, directory_flags, dir_fd=parent_fd)
        except OSError:
            if source_fd is not None:
                os.close(source_fd)
            if parent_fd is not None:
                os.close(parent_fd)
            logger.warning(f"[S3 Sync] 소스 디렉토리를 찾을 수 없습니다: {source_dir}")
            return ArchiveReceipt(
                status=ArchiveStatus.SOURCE_MISSING,
                target_date=target_date,
                source_dir=canonical_source,
                detail="source directory is missing, unsafe, or is not a directory",
            )

        assert parent_fd is not None
        assert source_fd is not None
        try:
            source_identity = _filesystem_identity(os.fstat(source_fd))
            matching_files = []
            with os.scandir(source_fd) as entries:
                for entry in entries:
                    if not entry.name.endswith(".csv") or target_date not in entry.name:
                        continue
                    try:
                        metadata = os.stat(
                            entry.name,
                            dir_fd=source_fd,
                            follow_symlinks=False,
                        )
                    except OSError:
                        continue
                    if stat.S_ISREG(metadata.st_mode):
                        matching_files.append(
                            (entry.name, _filesystem_identity(metadata))
                        )
            matching_files.sort(key=lambda item: item[0])

            if not matching_files:
                logger.warning(
                    f"[S3 Sync] 백업 대상 CSV를 찾을 수 없습니다: {canonical_source}"
                )
                return ArchiveReceipt(
                    status=ArchiveStatus.NO_TARGETS,
                    target_date=target_date,
                    source_dir=canonical_source,
                    detail="no matching direct CSV targets",
                    source_identity=source_identity,
                )

            logger.info(f"[{target_date}] ☁️ S3 데이터 백업 파이프라인 가동...")
            targets = []
            for filename, discovered_identity in matching_files:
                local_path = os.path.join(canonical_source, filename)
                object_key = f"daily/{target_date}/{filename}"
                if self._before_target_open is not None:
                    self._before_target_open(local_path)

                file_fd: Optional[int] = None
                failure: Optional[str] = None
                succeeded = False
                try:
                    if _filesystem_identity(os.fstat(source_fd)) != source_identity:
                        failure = "source directory changed before upload"
                    else:
                        file_fd = os.open(
                            filename,
                            _secure_file_flags(),
                            dir_fd=source_fd,
                        )
                        opened_identity = _filesystem_identity(os.fstat(file_fd))
                        if opened_identity != discovered_identity:
                            failure = "source file changed before upload"
                        else:
                            with os.fdopen(file_fd, "rb", closefd=True) as file_obj:
                                file_fd = None
                                succeeded = self._upload_fileobj(
                                    file_obj,
                                    local_path,
                                    object_key,
                                )
                                if succeeded and self._after_target_upload is not None:
                                    self._after_target_upload(local_path)
                                if succeeded:
                                    descriptor_identity = _filesystem_identity(
                                        os.fstat(file_obj.fileno())
                                    )
                                    try:
                                        path_metadata = os.stat(
                                            filename,
                                            dir_fd=source_fd,
                                            follow_symlinks=False,
                                        )
                                    except OSError:
                                        path_metadata = None
                                    if (
                                        descriptor_identity != discovered_identity
                                        or path_metadata is None
                                        or not stat.S_ISREG(path_metadata.st_mode)
                                        or _filesystem_identity(path_metadata)
                                        != discovered_identity
                                        or _filesystem_identity(os.fstat(source_fd))
                                        != source_identity
                                    ):
                                        succeeded = False
                                        failure = "source identity changed during upload"
                                elif failure is None:
                                    failure = "upload returned false"
                except OSError:
                    failure = "source identity changed before upload"
                finally:
                    if file_fd is not None:
                        os.close(file_fd)

                targets.append(
                    ArchiveTargetReceipt(
                        local_path=local_path,
                        object_key=object_key,
                        succeeded=succeeded,
                        source_identity=discovered_identity,
                        failure=None if succeeded else failure,
                    )
                )

            succeeded_count = sum(target.succeeded for target in targets)
            if succeeded_count == len(targets):
                status = ArchiveStatus.SUCCEEDED
            elif succeeded_count == 0:
                status = ArchiveStatus.FAILED
            else:
                status = ArchiveStatus.PARTIAL_FAILURE

            logger.info(
                f"[S3 Sync] 완료. 총 {succeeded_count}/{len(targets)}개의 파일을 "
                f"S3({self.bucket_name})에 백업했습니다."
            )
            return ArchiveReceipt(
                status=status,
                target_date=target_date,
                source_dir=canonical_source,
                targets=tuple(targets),
                source_identity=source_identity,
            )
        finally:
            os.close(source_fd)
            os.close(parent_fd)
