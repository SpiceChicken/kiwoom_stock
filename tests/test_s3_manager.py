from pathlib import Path

import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from kiwoom_stock.application.ports import ArchiveStatus, FilesystemIdentity
from kiwoom_stock.utils import s3_manager as s3_module
from kiwoom_stock.utils.s3_manager import S3Manager


TODAY = "2026-07-18"


class FakeS3Client:
    def __init__(
        self,
        *,
        failures=(),
        pathname_failure=None,
        unexpected_error=None,
    ):
        self.failures = set(failures)
        self.pathname_failure = pathname_failure
        self.unexpected_error = unexpected_error
        self.uploads = []

    def upload_file(self, local_path, bucket_name, object_key):
        self.uploads.append(("path", local_path, bucket_name, object_key))
        if self.pathname_failure is not None:
            raise self.pathname_failure

    def upload_fileobj(self, file_obj, bucket_name, object_key):
        payload = file_obj.read()
        self.uploads.append(("fileobj", payload, bucket_name, object_key))
        if self.unexpected_error is not None:
            raise self.unexpected_error
        if Path(object_key).name in self.failures:
            raise S3UploadFailedError(f"managed upload failed: {object_key}")


@pytest.fixture(autouse=True)
def forbid_real_boto_client(monkeypatch):
    def forbidden_client(*_args, **_kwargs):
        raise AssertionError("tests must inject an S3 client")

    monkeypatch.setattr(s3_module.boto3, "client", forbidden_client)


@pytest.mark.parametrize(
    ("target_date", "source_dir", "message"),
    [
        ("", "/tmp/source", "YYYY-MM-DD"),
        ("20260718", "/tmp/source", "YYYY-MM-DD"),
        ("2026-7-18", "/tmp/source", "YYYY-MM-DD"),
        (TODAY, "", "must not be blank"),
        (TODAY, "   ", "must not be blank"),
    ],
)
def test_sync_rejects_invalid_command_before_any_upload(
    target_date,
    source_dir,
    message,
):
    client = FakeS3Client()

    with pytest.raises(ValueError, match=message):
        S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
            target_date,
            source_dir,
        )

    assert client.uploads == []


@pytest.mark.parametrize("source_kind", ["missing", "file"])
def test_sync_daily_outputs_reports_missing_or_non_directory_source(tmp_path, source_kind):
    source = tmp_path / source_kind
    if source_kind == "file":
        source.write_text("not a directory", encoding="utf-8")
    client = FakeS3Client()

    receipt = S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
        TODAY,
        str(source),
    )

    assert receipt.status is ArchiveStatus.SOURCE_MISSING
    assert receipt.source_dir == str(source.absolute())
    assert receipt.targets == ()
    assert receipt.cleanup_allowed is False
    assert client.uploads == []


@pytest.mark.parametrize("link_kind", ["source", "parent"])
def test_sync_rejects_symlinked_source_path_before_upload(tmp_path, link_kind):
    real_parent = tmp_path / "real-output"
    real_source = real_parent / "20260718"
    real_source.mkdir(parents=True)
    (real_source / "daily_2026-07-18.csv").write_text("data", encoding="utf-8")
    if link_kind == "source":
        linked_source = tmp_path / "linked-source"
        linked_source.symlink_to(real_source, target_is_directory=True)
    else:
        linked_parent = tmp_path / "linked-output"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        linked_source = linked_parent / "20260718"
    client = FakeS3Client()

    receipt = S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
        TODAY,
        str(linked_source),
    )

    assert receipt.status is ArchiveStatus.SOURCE_MISSING
    assert "symlink" in receipt.detail
    assert client.uploads == []


def test_sync_daily_outputs_reports_zero_for_no_direct_matching_regular_csv(tmp_path):
    source = tmp_path / "20260718"
    source.mkdir()
    (source / "wrong_2026-07-17.csv").write_text("old", encoding="utf-8")
    (source / "uppercase_2026-07-18.CSV").write_text("upper", encoding="utf-8")
    (source / "directory_2026-07-18.csv").mkdir()
    nested = source / "nested"
    nested.mkdir()
    (nested / "nested_2026-07-18.csv").write_text("nested", encoding="utf-8")
    outside = tmp_path / "outside_2026-07-18.csv"
    outside.write_text("outside", encoding="utf-8")
    (source / "linked_2026-07-18.csv").symlink_to(outside)
    client = FakeS3Client()

    receipt = S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
        TODAY,
        str(source),
    )

    assert receipt.status is ArchiveStatus.NO_TARGETS
    assert isinstance(receipt.source_identity, FilesystemIdentity)
    assert receipt.targets == ()
    assert receipt.cleanup_allowed is False
    assert client.uploads == []


def test_sync_uploads_pinned_direct_targets_once_in_filename_order(tmp_path):
    source = tmp_path / "20260718"
    source.mkdir()
    expected_names = ["a_2026-07-18.csv", "z_2026-07-18.csv"]
    for name in reversed(expected_names):
        (source / name).write_text(name, encoding="utf-8")
    (source / "ignore.txt").write_text("ignore", encoding="utf-8")
    client = FakeS3Client()

    receipt = S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
        target_date=TODAY,
        source_dir=str(source),
    )

    assert receipt.status is ArchiveStatus.SUCCEEDED
    assert [Path(item[3]).name for item in client.uploads] == expected_names
    assert [item[1].decode() for item in client.uploads] == expected_names
    assert [item[2] for item in client.uploads] == ["test-bucket", "test-bucket"]
    assert [item[3] for item in client.uploads] == [
        f"daily/{TODAY}/{name}" for name in expected_names
    ]
    assert tuple(target.local_path for target in receipt.targets) == tuple(
        str((source / name).absolute()) for name in expected_names
    )
    assert all(
        isinstance(target.source_identity, FilesystemIdentity)
        for target in receipt.targets
    )
    assert receipt.cleanup_paths == tuple(target.local_path for target in receipt.targets)
    assert receipt.failed_targets == ()
    assert receipt.cleanup_allowed is True


@pytest.mark.parametrize(
    ("failures", "expected_status", "success_count"),
    [
        ({"b_2026-07-18.csv"}, ArchiveStatus.PARTIAL_FAILURE, 1),
        (
            {"a_2026-07-18.csv", "b_2026-07-18.csv"},
            ArchiveStatus.FAILED,
            0,
        ),
    ],
    ids=["partial-real-managed-exception", "all-failed-real-managed-exception"],
)
def test_sync_maps_real_s3_upload_failed_error_to_per_target_receipts(
    tmp_path,
    failures,
    expected_status,
    success_count,
):
    source = tmp_path / "20260718"
    source.mkdir()
    for name in ("a_2026-07-18.csv", "b_2026-07-18.csv"):
        (source / name).write_text(name, encoding="utf-8")
    client = FakeS3Client(failures=failures)

    receipt = S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
        TODAY,
        str(source),
    )

    assert receipt.status is expected_status
    assert len(client.uploads) == 2
    assert len(receipt.succeeded_targets) == success_count
    assert len(receipt.failed_targets) == 2 - success_count
    assert all(target.failure == "upload returned false" for target in receipt.failed_targets)
    assert receipt.cleanup_allowed is False


@pytest.mark.parametrize(
    "managed_error",
    [
        S3UploadFailedError("managed transfer failure"),
        ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "UploadFile",
        ),
    ],
    ids=["s3-upload-failed", "client-error"],
)
def test_upload_file_keeps_boolean_legacy_contract_for_managed_errors(managed_error):
    successful_client = FakeS3Client()
    failed_client = FakeS3Client(pathname_failure=managed_error)

    assert S3Manager("bucket", s3_client=successful_client).upload_file(
        "/tmp/success.csv",
        "daily/success.csv",
    ) is True
    assert S3Manager("bucket", s3_client=failed_client).upload_file(
        "/tmp/failed.csv",
        "daily/failed.csv",
    ) is False


def test_target_replacement_before_secure_open_causes_zero_upload(tmp_path):
    source = tmp_path / "20260718"
    source.mkdir()
    target = source / "daily_2026-07-18.csv"
    target.write_text("archived-version", encoding="utf-8")
    client = FakeS3Client()
    hook_calls = []

    def replace_before_open(local_path):
        hook_calls.append(local_path)
        replacement = tmp_path / "replacement.csv"
        replacement.write_text("replacement", encoding="utf-8")
        replacement.replace(target)

    receipt = S3Manager(
        "test-bucket",
        s3_client=client,
        _before_target_open=replace_before_open,
    ).sync_daily_outputs(TODAY, str(source))

    assert hook_calls == [str(target.absolute())]
    assert client.uploads == []
    assert receipt.status is ArchiveStatus.FAILED
    assert receipt.targets[0].failure == "source directory changed before upload"
    assert target.read_text(encoding="utf-8") == "replacement"


def test_target_replacement_after_pinned_upload_invalidates_success_receipt(tmp_path):
    source = tmp_path / "20260718"
    source.mkdir()
    target = source / "daily_2026-07-18.csv"
    target.write_text("archived-version", encoding="utf-8")
    client = FakeS3Client()

    def replace_after_upload(_local_path):
        replacement = tmp_path / "replacement.csv"
        replacement.write_text("replacement", encoding="utf-8")
        replacement.replace(target)

    receipt = S3Manager(
        "test-bucket",
        s3_client=client,
        _after_target_upload=replace_after_upload,
    ).sync_daily_outputs(TODAY, str(source))

    assert client.uploads[0][1] == b"archived-version"
    assert receipt.status is ArchiveStatus.FAILED
    assert receipt.targets[0].failure == "source identity changed during upload"
    assert receipt.cleanup_allowed is False
    assert target.read_text(encoding="utf-8") == "replacement"


def test_unexpected_upload_exception_is_not_converted_to_success(tmp_path):
    source = tmp_path / "20260718"
    source.mkdir()
    (source / "daily_2026-07-18.csv").write_text("data", encoding="utf-8")
    client = FakeS3Client(unexpected_error=RuntimeError("unexpected"))

    with pytest.raises(RuntimeError, match="unexpected"):
        S3Manager("test-bucket", s3_client=client).sync_daily_outputs(
            TODAY,
            str(source),
        )

    assert len(client.uploads) == 1
