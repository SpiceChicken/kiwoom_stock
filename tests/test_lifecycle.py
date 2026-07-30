import logging
from inspect import Parameter, signature

import pytest

from kiwoom_stock.application.lifecycle import (
    notify_monitor_crashed,
    notify_monitor_finished,
    notify_monitor_started,
    run_post_market_tasks,
)
from kiwoom_stock.application.ports import (
    ArchiveReceipt,
    ArchiveStatus,
    ArchiveTargetReceipt,
    CleanupNotStartedError,
    CleanupReceipt,
    CleanupState,
    FilesystemIdentity,
    PostMarketResult,
)


TODAY = "2026-07-18"
SOURCE_DIR = "/runtime/output/20260718"
OUTPUT_ROOT = "/runtime/output"
SOURCE_IDENTITY = FilesystemIdentity(1, 100, 4096, 1000, 1000)
TARGET_IDENTITY = FilesystemIdentity(1, 200, 64, 1000, 1000)


class RecordingNotifier:
    def __init__(self, events=None):
        self.messages = []
        self.events = events

    def send_slack(self, text):
        self.messages.append(text)
        if self.events is not None:
            self.events.append("finish")


class RecordingReporter:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error

    def run_pipeline(self):
        self.events.append("report")
        if self.error is not None:
            raise self.error


class RecordingArchiveStore:
    def __init__(self, events, result):
        self.events = events
        self.result = result

    def sync_daily_outputs(self, target_date, source_dir):
        self.events.append(("s3", target_date, source_dir))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def archive_target(filename, succeeded=True, *, source_dir=SOURCE_DIR, object_key=None):
    return ArchiveTargetReceipt(
        local_path=f"{source_dir}/{filename}",
        object_key=object_key or f"daily/{TODAY}/{filename}",
        succeeded=succeeded,
        source_identity=TARGET_IDENTITY,
        failure=None if succeeded else "upload returned false",
    )


def successful_archive(*filenames):
    return ArchiveReceipt(
        status=ArchiveStatus.SUCCEEDED,
        target_date=TODAY,
        source_dir=SOURCE_DIR,
        targets=tuple(archive_target(filename) for filename in sorted(filenames)),
        source_identity=SOURCE_IDENTITY,
    )


def forbidden_legacy_cleanup(**_kwargs):
    raise AssertionError("prod must never call broad retention cleanup")


def forbidden_scoped_cleanup(**_kwargs):
    raise AssertionError("scoped cleanup must not run without full archive success")


def test_lifecycle_notifications_preserve_message_shapes():
    notifier = RecordingNotifier()

    notify_monitor_started(notifier, process_name="proc", now_text="2026-07-18 10:00:00")
    notify_monitor_finished(notifier, process_name="proc", now_text="2026-07-18 15:30:00")
    notify_monitor_crashed(
        notifier,
        process_name="proc",
        now_text="2026-07-18 10:30:00",
        error=RuntimeError("boom"),
    )

    assert "정상적으로 부팅" in notifier.messages[0]
    assert "모든 임무" in notifier.messages[1]
    assert "boom" in notifier.messages[2]


def test_local_post_market_keeps_legacy_three_day_cleanup_and_skips_archive():
    events = []
    notifier = RecordingNotifier(events)

    def reporter_factory(notifier_arg):
        assert notifier_arg is notifier
        return RecordingReporter(events)

    def forbidden_s3_factory(**_kwargs):
        raise AssertionError("local mode must not create S3 manager")

    def cleanup_files(retention_days, target_dir):
        events.append(("cleanup", retention_days, target_dir))

    result = run_post_market_tasks(
        notifier=notifier,
        process_name="paper-monitor",
        now_text="2026-07-18 15:30:00",
        today_text=TODAY,
        app_env="local",
        output_dir_str=SOURCE_DIR,
        s3_bucket=None,
        reporter_factory=reporter_factory,
        s3_factory=forbidden_s3_factory,
        cleanup_files=cleanup_files,
        scoped_cleanup=forbidden_scoped_cleanup,
    )

    assert events == ["report", ("cleanup", 3, OUTPUT_ROOT), "finish"]
    assert result == PostMarketResult("local", None, None, False)
    assert result.requires_attention is False
    assert "paper-monitor" in notifier.messages[0]


@pytest.mark.parametrize("app_env", ["prod", "production-like"])
def test_production_class_full_archive_cleans_receipt_targets_before_finish(
    app_env,
):
    events = []
    notifier = RecordingNotifier(events)
    archive = successful_archive(
        "minute_2026-07-18.csv",
        "physics_2026-07-18.csv",
    )

    def s3_factory(bucket_name):
        assert bucket_name == "kiwoom-bucket"
        return RecordingArchiveStore(events, archive)

    def scoped_cleanup(**kwargs):
        events.append(("scoped_cleanup", kwargs))
        assert kwargs == {
            "target_date": TODAY,
            "source_dir": SOURCE_DIR,
            "allowed_root": OUTPUT_ROOT,
            "source_identity": SOURCE_IDENTITY,
            "archived_targets": archive.succeeded_targets,
        }
        return CleanupReceipt(
            requested_paths=archive.cleanup_paths,
            deleted_paths=archive.cleanup_paths,
            failed_paths=(),
        )

    result = run_post_market_tasks(
        notifier=notifier,
        process_name="prod-monitor",
        now_text="2026-07-18 15:30:00",
        today_text=TODAY,
        app_env=app_env,
        output_dir_str=SOURCE_DIR,
        s3_bucket="kiwoom-bucket",
        reporter_factory=lambda _notifier: RecordingReporter(events),
        s3_factory=s3_factory,
        cleanup_files=forbidden_legacy_cleanup,
        scoped_cleanup=scoped_cleanup,
    )

    assert events[0] == "report"
    assert events[1] == ("s3", TODAY, SOURCE_DIR)
    assert events[2][0] == "scoped_cleanup"
    assert events[3] == "finish"
    assert result.archive_receipt is archive
    assert result.cleanup_receipt is not None
    assert result.outputs_preserved is False
    assert result.cleanup_state is CleanupState.COMPLETED
    assert result.requires_attention is False


def test_prod_missing_bucket_does_not_create_archive_store_or_cleanup(caplog):
    events = []
    notifier = RecordingNotifier(events)

    def forbidden_s3_factory(**_kwargs):
        raise AssertionError("missing bucket must not create archive store")

    with caplog.at_level(logging.ERROR):
        result = run_post_market_tasks(
            notifier=notifier,
            process_name="prod-monitor",
            now_text="2026-07-18 15:30:00",
            today_text=TODAY,
            app_env="prod",
            output_dir_str=SOURCE_DIR,
            s3_bucket=None,
            reporter_factory=lambda _notifier: RecordingReporter(events),
            s3_factory=forbidden_s3_factory,
            cleanup_files=forbidden_legacy_cleanup,
            scoped_cleanup=forbidden_scoped_cleanup,
        )

    assert events == ["report", "finish"]
    assert result.archive_receipt is not None
    assert result.archive_receipt.status is ArchiveStatus.NOT_CONFIGURED
    assert result.cleanup_receipt is None
    assert result.outputs_preserved is True
    assert result.requires_attention is True
    assert "보존" in caplog.text


@pytest.mark.parametrize(
    "archive",
    [
        ArchiveReceipt(
            ArchiveStatus.SOURCE_MISSING,
            TODAY,
            SOURCE_DIR,
            detail="source missing",
        ),
        ArchiveReceipt(
            ArchiveStatus.NO_TARGETS,
            TODAY,
            SOURCE_DIR,
            detail="no targets",
        ),
        ArchiveReceipt(
            ArchiveStatus.PARTIAL_FAILURE,
            TODAY,
            SOURCE_DIR,
            targets=(
                archive_target("a_2026-07-18.csv"),
                archive_target("b_2026-07-18.csv", False),
            ),
            source_identity=SOURCE_IDENTITY,
        ),
        ArchiveReceipt(
            ArchiveStatus.FAILED,
            TODAY,
            SOURCE_DIR,
            targets=(archive_target("failed_2026-07-18.csv", False),),
            source_identity=SOURCE_IDENTITY,
        ),
    ],
    ids=["source-missing", "zero-target", "partial", "all-failed"],
)
def test_prod_non_success_archive_statuses_preserve_every_local_output(archive):
    events = []
    result = run_post_market_tasks(
        notifier=RecordingNotifier(events),
        process_name="prod-monitor",
        now_text="2026-07-18 15:30:00",
        today_text=TODAY,
        app_env="prod",
        output_dir_str=SOURCE_DIR,
        s3_bucket="kiwoom-bucket",
        reporter_factory=lambda _notifier: RecordingReporter(events),
        s3_factory=lambda **_kwargs: RecordingArchiveStore(events, archive),
        cleanup_files=forbidden_legacy_cleanup,
        scoped_cleanup=forbidden_scoped_cleanup,
    )

    assert events == ["report", ("s3", TODAY, SOURCE_DIR), "finish"]
    assert result.archive_receipt is archive
    assert result.cleanup_receipt is None
    assert result.outputs_preserved is True
    assert result.requires_attention is True


@pytest.mark.parametrize("failure_stage", ["factory", "sync"])
def test_prod_archive_exceptions_become_explicit_error_and_preserve_outputs(
    failure_stage,
    caplog,
):
    events = []

    def s3_factory(**_kwargs):
        if failure_stage == "factory":
            raise RuntimeError("factory failed")
        return RecordingArchiveStore(events, RuntimeError("sync failed"))

    with caplog.at_level(logging.ERROR):
        result = run_post_market_tasks(
            notifier=RecordingNotifier(events),
            process_name="prod-monitor",
            now_text="2026-07-18 15:30:00",
            today_text=TODAY,
            app_env="prod",
            output_dir_str=SOURCE_DIR,
            s3_bucket="kiwoom-bucket",
            reporter_factory=lambda _notifier: RecordingReporter(events),
            s3_factory=s3_factory,
            cleanup_files=forbidden_legacy_cleanup,
            scoped_cleanup=forbidden_scoped_cleanup,
        )

    expected = ["report", "finish"]
    if failure_stage == "sync":
        expected.insert(1, ("s3", TODAY, SOURCE_DIR))
    assert events == expected
    assert result.archive_receipt is not None
    assert result.archive_receipt.status is ArchiveStatus.ERROR
    assert failure_stage in result.archive_receipt.detail
    assert result.outputs_preserved is True
    assert result.requires_attention is True
    assert "로컬 산출물을 보존" in caplog.text


@pytest.mark.parametrize(
    "archive",
    [
        ArchiveReceipt(
            ArchiveStatus.NO_TARGETS,
            "2026-07-17",
            SOURCE_DIR,
            detail="wrong date",
        ),
        ArchiveReceipt(
            ArchiveStatus.NO_TARGETS,
            TODAY,
            "/runtime/output/20260717",
            detail="wrong source",
        ),
        ArchiveReceipt(
            ArchiveStatus.SUCCEEDED,
            TODAY,
            SOURCE_DIR,
            targets=(
                archive_target(
                    "bad-key_2026-07-18.csv",
                    object_key="unexpected/key.csv",
                ),
            ),
            source_identity=SOURCE_IDENTITY,
        ),
        ArchiveReceipt(
            ArchiveStatus.NOT_CONFIGURED,
            TODAY,
            SOURCE_DIR,
            detail="adapter should not return this",
        ),
    ],
    ids=["date", "source", "object-key", "status"],
)
def test_prod_archive_contract_mismatch_is_error_and_cleanup_stays_closed(archive):
    events = []
    result = run_post_market_tasks(
        notifier=RecordingNotifier(events),
        process_name="prod-monitor",
        now_text="2026-07-18 15:30:00",
        today_text=TODAY,
        app_env="prod",
        output_dir_str=SOURCE_DIR,
        s3_bucket="kiwoom-bucket",
        reporter_factory=lambda _notifier: RecordingReporter(events),
        s3_factory=lambda **_kwargs: RecordingArchiveStore(events, archive),
        cleanup_files=forbidden_legacy_cleanup,
        scoped_cleanup=forbidden_scoped_cleanup,
    )

    assert events == ["report", ("s3", TODAY, SOURCE_DIR), "finish"]
    assert result.archive_receipt is not None
    assert result.archive_receipt.status is ArchiveStatus.ERROR
    assert result.outputs_preserved is True


def test_prod_unsafe_scoped_cleanup_preserves_outputs_and_finishes(caplog):
    events = []
    archive = successful_archive("safe_2026-07-18.csv")

    def unsafe_cleanup(**_kwargs):
        events.append("scoped_cleanup")
        raise CleanupNotStartedError("scope rejected")

    with caplog.at_level(logging.ERROR):
        result = run_post_market_tasks(
            notifier=RecordingNotifier(events),
            process_name="prod-monitor",
            now_text="2026-07-18 15:30:00",
            today_text=TODAY,
            app_env="prod",
            output_dir_str=SOURCE_DIR,
            s3_bucket="kiwoom-bucket",
            reporter_factory=lambda _notifier: RecordingReporter(events),
            s3_factory=lambda **_kwargs: RecordingArchiveStore(events, archive),
            cleanup_files=forbidden_legacy_cleanup,
            scoped_cleanup=unsafe_cleanup,
        )

    assert events == ["report", ("s3", TODAY, SOURCE_DIR), "scoped_cleanup", "finish"]
    assert result.archive_receipt is archive
    assert result.cleanup_receipt is None
    assert result.outputs_preserved is True
    assert result.cleanup_state is CleanupState.NOT_STARTED
    assert result.requires_attention is True
    assert "산출물을 보존" in caplog.text


def test_prod_cleanup_partial_failure_is_explicit_and_requires_attention():
    events = []
    archive = successful_archive("a_2026-07-18.csv", "b_2026-07-18.csv")

    def partially_failed_cleanup(**_kwargs):
        events.append("scoped_cleanup")
        return CleanupReceipt(
            requested_paths=archive.cleanup_paths,
            deleted_paths=(archive.cleanup_paths[0],),
            failed_paths=(archive.cleanup_paths[1],),
        )

    result = run_post_market_tasks(
        notifier=RecordingNotifier(events),
        process_name="prod-monitor",
        now_text="2026-07-18 15:30:00",
        today_text=TODAY,
        app_env="prod",
        output_dir_str=SOURCE_DIR,
        s3_bucket="kiwoom-bucket",
        reporter_factory=lambda _notifier: RecordingReporter(events),
        s3_factory=lambda **_kwargs: RecordingArchiveStore(events, archive),
        cleanup_files=forbidden_legacy_cleanup,
        scoped_cleanup=partially_failed_cleanup,
    )

    assert events == ["report", ("s3", TODAY, SOURCE_DIR), "scoped_cleanup", "finish"]
    assert result.cleanup_receipt is not None
    assert result.cleanup_receipt.failed_paths == (archive.cleanup_paths[1],)
    assert result.outputs_preserved is False
    assert result.cleanup_state is CleanupState.PARTIAL
    assert result.requires_attention is True


def test_cleanup_side_effect_then_exception_is_unknown_not_preserved(caplog):
    events = []
    archive = successful_archive("a_2026-07-18.csv", "b_2026-07-18.csv")

    def delete_one_then_raise(**_kwargs):
        events.append("scoped_cleanup")
        events.append(("deleted", archive.cleanup_paths[0]))
        raise RuntimeError("adapter failed after one deletion")

    with caplog.at_level(logging.ERROR):
        result = run_post_market_tasks(
            notifier=RecordingNotifier(events),
            process_name="prod-monitor",
            now_text="2026-07-18 15:30:00",
            today_text=TODAY,
            app_env="prod",
            output_dir_str=SOURCE_DIR,
            s3_bucket="kiwoom-bucket",
            reporter_factory=lambda _notifier: RecordingReporter(events),
            s3_factory=lambda **_kwargs: RecordingArchiveStore(events, archive),
            cleanup_files=forbidden_legacy_cleanup,
            scoped_cleanup=delete_one_then_raise,
        )

    assert result.cleanup_receipt is None
    assert result.cleanup_state is CleanupState.UNKNOWN_AFTER_ATTEMPT
    assert result.outputs_preserved is False
    assert result.requires_attention is True
    assert "상태를 확인할 수 없습니다" in caplog.text
    assert events[-1] == "finish"


def test_reporter_exception_stops_archive_cleanup_and_finish_notice():
    events = []
    notifier = RecordingNotifier(events)

    with pytest.raises(RuntimeError, match="report failed"):
        run_post_market_tasks(
            notifier=notifier,
            process_name="prod-monitor",
            now_text="2026-07-18 15:30:00",
            today_text=TODAY,
            app_env="prod",
            output_dir_str=SOURCE_DIR,
            s3_bucket="kiwoom-bucket",
            reporter_factory=lambda _notifier: RecordingReporter(
                events,
                RuntimeError("report failed"),
            ),
            s3_factory=lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("archive must not start")
            ),
            cleanup_files=forbidden_legacy_cleanup,
            scoped_cleanup=forbidden_scoped_cleanup,
        )

    assert events == ["report"]
    assert notifier.messages == []


def test_application_lifecycle_requires_outer_adapter_injection():
    parameters = signature(run_post_market_tasks).parameters
    for name in ("reporter_factory", "s3_factory", "cleanup_files", "scoped_cleanup"):
        assert parameters[name].default is Parameter.empty


def test_receipt_invariants_fail_fast():
    with pytest.raises(ValueError, match="failure detail"):
        ArchiveTargetReceipt(
            "/tmp/a.csv",
            "daily/a.csv",
            False,
            TARGET_IDENTITY,
        )
    with pytest.raises(ValueError, match="target outcomes"):
        ArchiveReceipt(ArchiveStatus.SUCCEEDED, TODAY, SOURCE_DIR)
    with pytest.raises(ValueError, match="cover every requested"):
        CleanupReceipt(("/tmp/a.csv",), (), ())
    with pytest.raises(ValueError, match="requires an archive receipt"):
        PostMarketResult("prod", None, None, True)
