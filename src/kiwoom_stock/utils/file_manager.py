import os
import time
import logging
import shutil
import stat
from datetime import date
from typing import Callable, Optional, Sequence

from kiwoom_stock.application.ports import (
    ArchiveTargetReceipt,
    CleanupNotStartedError,
    CleanupReceipt,
    FilesystemIdentity,
)

logger = logging.getLogger(__name__)
_CLEANUP_DIR_FD_SUPPORTED = all(
    function in os.supports_dir_fd for function in (os.open, os.stat, os.unlink)
)

def clean_old_csv_files(retention_days: int, target_dir: str):
    """
    지정된 디렉토리(예: output/) 하위를 순회하며 
    보존 기간(retention_days)이 지난 CSV 파일과 텅 빈 날짜 폴더를 안전하게 삭제합니다.
    (retention_days가 0이면 S3 백업이 끝난 당일 데이터를 즉시 파기합니다)
    """
    if not os.path.exists(target_dir):
        logger.warning(f"[Cleanup] 대상 디렉토리를 찾을 수 없습니다: {target_dir}")
        return

    # retention_days가 0이면 당일(미래 시간 포함) 파일도 모두 지우도록 설정
    if retention_days == 0:
        cutoff_time = time.time() + 86400  
    else:
        cutoff_time = time.time() - (retention_days * 86400) # 86400초 = 1일
        
    deleted_files = 0
    deleted_dirs = 0

    # 💡 [V3.1] os.walk를 사용하여 날짜별 하위 폴더(YYYYMMDD)까지 모두 탐색 (Bottom-up 방식)
    for root, dirs, files in os.walk(target_dir, topdown=False):
        # 1. 오래된 CSV 파일 삭제
        for name in files:
            if name.endswith('.csv'):
                file_path = os.path.join(root, name)
                
                # 파일의 마지막 수정 시간이 보존 기간을 넘겼는지 확인
                if os.path.getmtime(file_path) < cutoff_time:
                    try:
                        os.remove(file_path)
                        deleted_files += 1
                    except Exception as e:
                        logger.error(f"[Cleanup] 파일 삭제 실패 {file_path}: {e}")
        
        # 2. 내부 파일이 모두 지워져서 텅 빈 날짜 폴더(YYYYMMDD)가 있다면 폴더 자체도 삭제
        for name in dirs:
            dir_path = os.path.join(root, name)
            
            # 폴더가 비어있고, 폴더 생성일 역시 오래되었다면
            if not os.listdir(dir_path) and os.path.getmtime(dir_path) < cutoff_time:
                try:
                    os.rmdir(dir_path)
                    deleted_dirs += 1
                except Exception as e:
                    pass
                    
    if deleted_files > 0 or deleted_dirs > 0:
        logger.info(f"🧹 [Cleanup] {retention_days}일 보존 기준 초과 산출물 파기 완료 (파일 {deleted_files}개, 빈 폴더 {deleted_dirs}개 삭제)")


def clean_archived_csv_files(
    *,
    target_date: str,
    source_dir: str,
    allowed_root: str,
    source_identity: FilesystemIdentity,
    archived_targets: Sequence[ArchiveTargetReceipt],
    _before_delete: Optional[Callable[[], None]] = None,
) -> CleanupReceipt:
    """Delete only identity-bound direct CSV targets using pinned directory fds."""
    try:
        parsed_date = date.fromisoformat(target_date)
    except (TypeError, ValueError) as error:
        raise CleanupNotStartedError("target_date must use YYYY-MM-DD") from error
    if parsed_date.isoformat() != target_date:
        raise CleanupNotStartedError("target_date must use YYYY-MM-DD")
    if not isinstance(source_identity, FilesystemIdentity):
        raise CleanupNotStartedError("source identity is required")
    if not isinstance(source_dir, str) or not source_dir.strip():
        raise CleanupNotStartedError("source_dir must not be blank")
    if not isinstance(allowed_root, str) or not allowed_root.strip():
        raise CleanupNotStartedError("allowed_root must not be blank")

    root_path = os.path.abspath(os.path.normpath(allowed_root))
    source_path = os.path.abspath(os.path.normpath(source_dir))
    source_name = os.path.basename(source_path)
    if source_path == root_path or os.path.dirname(source_path) != root_path:
        raise CleanupNotStartedError(
            "source_dir must be a direct child of allowed_root"
        )
    if source_name != target_date.replace("-", ""):
        raise CleanupNotStartedError("source_dir name must match target_date")
    if os.path.realpath(root_path) != root_path:
        raise CleanupNotStartedError("allowed_root must not traverse a symlink")

    if isinstance(archived_targets, (str, bytes)) or not archived_targets:
        raise CleanupNotStartedError(
            "archived_targets must be a non-empty target sequence"
        )
    targets = tuple(archived_targets)
    if not all(isinstance(target, ArchiveTargetReceipt) for target in targets):
        raise CleanupNotStartedError(
            "archived_targets must contain ArchiveTargetReceipt values"
        )
    if any(not target.succeeded for target in targets):
        raise CleanupNotStartedError("archived_targets must all be successful")
    requested_paths = tuple(target.local_path for target in targets)
    if len(requested_paths) != len(set(requested_paths)):
        raise CleanupNotStartedError("archived target paths must be unique")

    if (
        not all(
            hasattr(os, name)
            for name in ("O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC")
        )
        or not _CLEANUP_DIR_FD_SUPPORTED
    ):
        raise CleanupNotStartedError(
            "descriptor-relative no-follow cleanup is not supported"
        )

    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root_fd: Optional[int] = None
    source_fd: Optional[int] = None
    try:
        try:
            root_fd = os.open(root_path, directory_flags)
            source_fd = os.open(source_name, directory_flags, dir_fd=root_fd)
        except OSError as error:
            raise CleanupNotStartedError(
                "allowed root or source directory is missing or unsafe"
            ) from error

        assert root_fd is not None
        assert source_fd is not None
        root_identity = _filesystem_identity(os.fstat(root_fd))
        if _filesystem_identity(os.fstat(source_fd)) != source_identity:
            raise CleanupNotStartedError(
                "source directory identity does not match archive receipt"
            )

        target_names = []
        for target in targets:
            filename = os.path.basename(target.local_path)
            expected_path = os.path.join(source_path, filename)
            if target.local_path != expected_path or os.sep in filename:
                raise CleanupNotStartedError(
                    "archived path must be a canonical direct source child"
                )
            if not filename.endswith(".csv") or target_date not in filename:
                raise CleanupNotStartedError(
                    "archived path must be a target-date CSV"
                )
            try:
                metadata = os.stat(
                    filename,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise CleanupNotStartedError(
                    "archived target is missing before cleanup"
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _filesystem_identity(metadata) != target.source_identity
            ):
                raise CleanupNotStartedError(
                    "archived target identity does not match archive receipt"
                )
            target_names.append(filename)

        if _before_delete is not None:
            _before_delete()

        try:
            current_root = os.stat(root_path, follow_symlinks=False)
            current_source = os.stat(
                source_name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            raise CleanupNotStartedError(
                "cleanup root changed before the first deletion"
            ) from error
        if (
            _filesystem_identity(current_root) != root_identity
            or _filesystem_identity(current_source) != source_identity
            or _filesystem_identity(os.fstat(source_fd)) != source_identity
        ):
            raise CleanupNotStartedError(
                "cleanup root or source changed before the first deletion"
            )
        for filename, target in zip(target_names, targets):
            try:
                metadata = os.stat(
                    filename,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
            except OSError as error:
                raise CleanupNotStartedError(
                    "archived target changed before the first deletion"
                ) from error
            if (
                not stat.S_ISREG(metadata.st_mode)
                or _filesystem_identity(metadata) != target.source_identity
            ):
                raise CleanupNotStartedError(
                    "archived target changed before the first deletion"
                )

        deleted_paths = []
        failed_paths = []
        for filename, target in zip(target_names, targets):
            try:
                current_source = os.stat(
                    source_name,
                    dir_fd=root_fd,
                    follow_symlinks=False,
                )
                current_target = os.stat(
                    filename,
                    dir_fd=source_fd,
                    follow_symlinks=False,
                )
                if (
                    (current_source.st_dev, current_source.st_ino)
                    != (source_identity.device, source_identity.inode)
                    or not stat.S_ISREG(current_target.st_mode)
                    or _filesystem_identity(current_target)
                    != target.source_identity
                ):
                    raise OSError("filesystem identity changed before unlink")
                os.unlink(filename, dir_fd=source_fd)
                deleted_paths.append(target.local_path)
            except OSError as error:
                failed_paths.append(target.local_path)
                logger.error(
                    f"[Cleanup] 아카이브 완료 파일 삭제 실패 "
                    f"{target.local_path}: {error}"
                )

        return CleanupReceipt(
            requested_paths=requested_paths,
            deleted_paths=tuple(deleted_paths),
            failed_paths=tuple(failed_paths),
        )
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if root_fd is not None:
            os.close(root_fd)


def _filesystem_identity(metadata: os.stat_result) -> FilesystemIdentity:
    return FilesystemIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
    )
