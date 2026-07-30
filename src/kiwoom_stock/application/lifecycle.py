"""Process lifecycle and post-market orchestration."""

import logging
import os
from typing import Any, Callable, Optional, Protocol

from kiwoom_stock.application.ports import (
    ArchiveReceipt,
    ArchiveStatus,
    ArchiveStore,
    CleanupNotStartedError,
    CleanupReceipt,
    CleanupState,
    PostMarketResult,
    ScopedCleanup,
    is_production_environment,
)

logger = logging.getLogger(__name__)


class SlackNotifier(Protocol):
    def send_slack(self, text: str) -> None:
        """Send a plain Slack message."""


class DailyReporterLike(Protocol):
    def run_pipeline(self) -> None:
        """Run daily post-mortem reporting."""


class CleanupFiles(Protocol):
    def __call__(self, *, retention_days: int, target_dir: str) -> None:
        """Clean output files according to a retention policy."""


ReporterFactory = Callable[[Any], DailyReporterLike]
S3Factory = Callable[..., ArchiveStore]


def notify_monitor_started(notifier: SlackNotifier, *, process_name: str, now_text: str) -> None:
    notifier.send_slack(
        f"🚀 *[{process_name}]* ({now_text})\n올-웨더 모니터링 시스템이 정상적으로 부팅되었습니다."
    )


def notify_monitor_finished(notifier: SlackNotifier, *, process_name: str, now_text: str) -> None:
    notifier.send_slack(
        f"🏁 *[{process_name}]* ({now_text})\n오늘의 모든 임무(매매/부검/백업)를 완벽하게 마치고 엔진을 안전하게 종료합니다."
    )


def notify_monitor_crashed(
    notifier: SlackNotifier,
    *,
    process_name: str,
    now_text: str,
    error: Exception,
) -> None:
    notifier.send_slack(
        f"🚨 *[{process_name}]* ({now_text})\n"
        f"엔진 가동 중 치명적 오류가 발생하여 시스템이 다운되었습니다.\n```{error}```"
    )


def _canonical_path(path: str) -> str:
    return os.path.abspath(os.path.normpath(path))


def _validate_archive_receipt(
    receipt: ArchiveReceipt,
    *,
    target_date: str,
    source_dir: str,
) -> None:
    if not isinstance(receipt, ArchiveReceipt):
        raise TypeError("archive store must return ArchiveReceipt")

    expected_source = _canonical_path(source_dir)
    if receipt.target_date != target_date:
        raise ValueError("archive receipt target date does not match request")
    if receipt.source_dir != expected_source:
        raise ValueError("archive receipt source directory does not match request")
    if receipt.status is ArchiveStatus.NOT_CONFIGURED:
        raise ValueError("configured archive store returned NOT_CONFIGURED")

    for target in receipt.targets:
        local_path = target.local_path
        filename = os.path.basename(local_path)
        if local_path != _canonical_path(local_path):
            raise ValueError("archive target path must be canonical")
        if os.path.dirname(local_path) != expected_source:
            raise ValueError("archive target must be a direct source child")
        if not filename.endswith(".csv") or target_date not in filename:
            raise ValueError("archive target must be a target-date CSV")
        expected_key = f"daily/{target_date}/{filename}"
        if target.object_key != expected_key:
            raise ValueError("archive target object key does not match request")


def _archive_error_receipt(
    *,
    target_date: str,
    source_dir: str,
    stage: str,
    error: Exception,
) -> ArchiveReceipt:
    return ArchiveReceipt(
        status=ArchiveStatus.ERROR,
        target_date=target_date,
        source_dir=_canonical_path(source_dir),
        detail=f"archive {stage} failed with {type(error).__name__}",
    )


def run_post_market_tasks(
    *,
    notifier: SlackNotifier,
    process_name: str,
    now_text: str,
    today_text: str,
    app_env: str,
    output_dir_str: str,
    s3_bucket: Optional[str],
    reporter_factory: ReporterFactory,
    s3_factory: S3Factory,
    cleanup_files: CleanupFiles,
    scoped_cleanup: ScopedCleanup,
) -> PostMarketResult:
    """Run post-market report, optional backup, cleanup, and completion notice."""
    logger.info("🏁 엔진 구동 종료. 일일 자동 부검 파이프라인(Daily Post-Mortem)을 가동합니다.")
    reporter = reporter_factory(notifier)
    reporter.run_pipeline()

    normalized_env = app_env.lower()
    output_parent = os.path.dirname(output_dir_str)

    logger.info(f"🧹 장 마감 사후 처리 가동 (현재 환경: {normalized_env.upper()})")

    if is_production_environment(normalized_env):
        logger.info(
            "[Prod] 운영 등급 환경입니다. S3 전체 백업 확인 후 해당 로컬 파일만 정리합니다."
        )
        if not s3_bucket:
            archive_receipt = ArchiveReceipt(
                status=ArchiveStatus.NOT_CONFIGURED,
                target_date=today_text,
                source_dir=_canonical_path(output_dir_str),
                detail="S3 bucket is not configured",
            )
        else:
            archive_stage = "factory"
            try:
                s3 = s3_factory(bucket_name=s3_bucket)
                archive_stage = "sync"
                archive_receipt = s3.sync_daily_outputs(
                    target_date=today_text,
                    source_dir=output_dir_str,
                )
                archive_stage = "contract"
                _validate_archive_receipt(
                    archive_receipt,
                    target_date=today_text,
                    source_dir=output_dir_str,
                )
            except Exception as error:
                logger.exception(
                    "[Prod] S3 아카이브 단계가 실패했습니다. 로컬 산출물을 보존합니다."
                )
                archive_receipt = _archive_error_receipt(
                    target_date=today_text,
                    source_dir=output_dir_str,
                    stage=archive_stage,
                    error=error,
                )

        cleanup_receipt: Optional[CleanupReceipt] = None
        cleanup_state = CleanupState.NOT_STARTED
        outputs_preserved = True
        if archive_receipt.cleanup_allowed:
            cleanup_state = CleanupState.UNKNOWN_AFTER_ATTEMPT
            outputs_preserved = False
            try:
                assert archive_receipt.source_identity is not None
                cleanup_receipt = scoped_cleanup(
                    target_date=today_text,
                    source_dir=archive_receipt.source_dir,
                    allowed_root=output_parent,
                    source_identity=archive_receipt.source_identity,
                    archived_targets=archive_receipt.succeeded_targets,
                )
                if not isinstance(cleanup_receipt, CleanupReceipt):
                    raise TypeError("scoped cleanup must return CleanupReceipt")
                if cleanup_receipt.requested_paths != archive_receipt.cleanup_paths:
                    raise ValueError("cleanup receipt paths do not match archive receipt")
                cleanup_state = cleanup_receipt.state
                if cleanup_receipt.failed_paths:
                    logger.error(
                        "[Prod] 아카이브 후 일부 로컬 파일 정리에 실패했습니다: %d개",
                        len(cleanup_receipt.failed_paths),
                    )
            except CleanupNotStartedError:
                cleanup_receipt = None
                cleanup_state = CleanupState.NOT_STARTED
                outputs_preserved = True
                logger.exception(
                    "[Prod] 안전한 로컬 정리 범위 검증에 실패했습니다. 산출물을 보존합니다."
                )
            except Exception:
                cleanup_receipt = None
                cleanup_state = CleanupState.UNKNOWN_AFTER_ATTEMPT
                outputs_preserved = False
                logger.exception(
                    "[Prod] 로컬 정리 시도 후 상태를 확인할 수 없습니다. 운영자 확인이 필요합니다."
                )
        else:
            logger.error(
                "[Prod] S3 아카이브가 완전 성공하지 않아 로컬 산출물을 보존합니다: %s",
                archive_receipt.status.value,
            )

        result = PostMarketResult(
            environment=normalized_env,
            archive_receipt=archive_receipt,
            cleanup_receipt=cleanup_receipt,
            outputs_preserved=outputs_preserved,
            cleanup_state=cleanup_state,
        )
    else:
        logger.info("[Local] 테스트 환경입니다. S3 업로드를 스킵하고 로컬에 3일간 보존합니다.")
        cleanup_files(retention_days=3, target_dir=output_parent)
        result = PostMarketResult(
            environment=normalized_env,
            archive_receipt=None,
            cleanup_receipt=None,
            outputs_preserved=False,
        )

    notify_monitor_finished(notifier, process_name=process_name, now_text=now_text)
    return result
