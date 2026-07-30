import os
import time

import pytest

from kiwoom_stock.application.ports import (
    ArchiveTargetReceipt,
    CleanupNotStartedError,
    FilesystemIdentity,
)
from kiwoom_stock.utils.file_manager import (
    clean_archived_csv_files,
    clean_old_csv_files,
)


TODAY = "2026-07-18"


def make_daily_layout(tmp_path, source_name="20260718"):
    root = tmp_path / "output"
    source = root / source_name
    source.mkdir(parents=True)
    return root, source


def write_csv(path, content="data"):
    path.write_text(content, encoding="utf-8")
    return str(path.absolute())


def identity(path, *, follow_symlinks=False):
    metadata = os.stat(path, follow_symlinks=follow_symlinks)
    return FilesystemIdentity(
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def archived_target(path, *, source_identity=None):
    path_text = str(path.absolute())
    return ArchiveTargetReceipt(
        local_path=path_text,
        object_key=f"daily/{TODAY}/{path.name}",
        succeeded=True,
        source_identity=source_identity or identity(path),
    )


def run_scoped(
    root,
    source,
    targets,
    *,
    target_date=TODAY,
    source_identity=None,
    before_delete=None,
):
    assert all(str(test_path) != "/" for test_path in (root, source))
    return clean_archived_csv_files(
        target_date=target_date,
        source_dir=str(source.absolute()),
        allowed_root=str(root.absolute()),
        source_identity=source_identity or identity(source),
        archived_targets=targets,
        _before_delete=before_delete,
    )


def test_scoped_cleanup_deletes_only_exact_identity_bound_csv_targets(tmp_path):
    root, source = make_daily_layout(tmp_path)
    archived_a = source / "a_2026-07-18.csv"
    archived_b = source / "b_2026-07-18.csv"
    sibling = root / "20260717"
    sibling.mkdir()
    sibling_csv = sibling / "sibling_2026-07-18.csv"
    nested = source / "nested"
    nested.mkdir()
    nested_csv = nested / "nested_2026-07-18.csv"
    non_csv = source / "notes_2026-07-18.txt"
    for path in (archived_a, archived_b, sibling_csv, nested_csv):
        write_csv(path)
    non_csv.write_text("keep", encoding="utf-8")
    targets = (archived_target(archived_a), archived_target(archived_b))
    source_archive_identity = identity(source)

    receipt = run_scoped(
        root,
        source,
        targets,
        source_identity=source_archive_identity,
    )

    expected = tuple(target.local_path for target in targets)
    assert receipt.requested_paths == expected
    assert receipt.deleted_paths == expected
    assert receipt.failed_paths == ()
    assert not archived_a.exists()
    assert not archived_b.exists()
    assert sibling_csv.exists()
    assert nested_csv.exists()
    assert non_csv.exists()
    assert source.is_dir(), "scoped cleanup must never remove the date directory"


@pytest.mark.parametrize(
    "unsafe_kind",
    [
        "outside-root",
        "parent-traversal",
        "sibling-date",
        "nested",
        "wrong-date-name",
        "non-csv",
        "symlink",
    ],
)
def test_scoped_cleanup_rejects_unsafe_target_set_before_first_delete(
    tmp_path,
    unsafe_kind,
):
    root, source = make_daily_layout(tmp_path)
    valid = source / "valid_2026-07-18.csv"
    write_csv(valid)

    if unsafe_kind == "outside-root":
        unsafe = tmp_path / "outside_2026-07-18.csv"
        write_csv(unsafe)
        unsafe_path = unsafe
    elif unsafe_kind == "parent-traversal":
        unsafe = source / "traversed_2026-07-18.csv"
        write_csv(unsafe)
        nested = source / "nested"
        nested.mkdir()
        unsafe_path = nested / ".." / unsafe.name
    elif unsafe_kind == "sibling-date":
        sibling = root / "20260717"
        sibling.mkdir()
        unsafe = sibling / "sibling_2026-07-18.csv"
        write_csv(unsafe)
        unsafe_path = unsafe
    elif unsafe_kind == "nested":
        nested = source / "nested"
        nested.mkdir()
        unsafe = nested / "nested_2026-07-18.csv"
        write_csv(unsafe)
        unsafe_path = unsafe
    elif unsafe_kind == "wrong-date-name":
        unsafe = source / "wrong_2026-07-17.csv"
        write_csv(unsafe)
        unsafe_path = unsafe
    elif unsafe_kind == "non-csv":
        unsafe = source / "notes_2026-07-18.txt"
        unsafe.write_text("keep", encoding="utf-8")
        unsafe_path = unsafe
    else:
        outside = tmp_path / "symlink-target_2026-07-18.csv"
        write_csv(outside)
        unsafe = source / "linked_2026-07-18.csv"
        unsafe.symlink_to(outside)
        unsafe_path = unsafe

    targets = (
        archived_target(valid),
        archived_target(unsafe_path),
    )
    source_archive_identity = identity(source)

    with pytest.raises(CleanupNotStartedError):
        run_scoped(
            root,
            source,
            targets,
            source_identity=source_archive_identity,
        )

    assert valid.exists(), "validation must complete before the first unlink"
    assert unsafe.exists()


def test_scoped_cleanup_rejects_duplicate_paths_atomically(tmp_path):
    root, source = make_daily_layout(tmp_path)
    target = source / "target_2026-07-18.csv"
    write_csv(target)
    target_receipt = archived_target(target)

    with pytest.raises(CleanupNotStartedError, match="unique"):
        run_scoped(root, source, (target_receipt, target_receipt))

    assert target.exists()


@pytest.mark.parametrize("source_mode", ["same-as-root", "wrong-date-directory"])
def test_scoped_cleanup_rejects_invalid_source_scope_before_delete(tmp_path, source_mode):
    root = tmp_path / "output"
    root.mkdir()
    if source_mode == "same-as-root":
        source = root
    else:
        source = root / "20260717"
        source.mkdir()
    target = source / "target_2026-07-18.csv"
    write_csv(target)

    with pytest.raises(CleanupNotStartedError):
        run_scoped(root, source, (archived_target(target),))

    assert target.exists()


@pytest.mark.parametrize("target_date", ["20260718", "2026-7-18", "not-a-date", ""])
def test_scoped_cleanup_rejects_invalid_iso_target_date_atomically(tmp_path, target_date):
    root, source = make_daily_layout(tmp_path)
    target = source / "target_2026-07-18.csv"
    write_csv(target)

    with pytest.raises(CleanupNotStartedError, match="YYYY-MM-DD"):
        run_scoped(
            root,
            source,
            (archived_target(target),),
            target_date=target_date,
        )

    assert target.exists()


def test_uploaded_file_replacement_is_not_deleted(tmp_path):
    root, source = make_daily_layout(tmp_path)
    target = source / "target_2026-07-18.csv"
    write_csv(target, "archived-version")
    target_receipt = archived_target(target)
    source_archive_identity = identity(source)
    replacement = tmp_path / "replacement.csv"
    write_csv(replacement, "replacement")
    replacement.replace(target)

    with pytest.raises(CleanupNotStartedError, match="identity"):
        run_scoped(
            root,
            source,
            (target_receipt,),
            source_identity=source_archive_identity,
        )

    assert target.read_text(encoding="utf-8") == "replacement"


@pytest.mark.parametrize("replacement_kind", ["date-directory", "output-parent"])
def test_parent_replacement_before_delete_fails_closed_with_zero_deletes(
    tmp_path,
    replacement_kind,
):
    root, source = make_daily_layout(tmp_path)
    target = source / "target_2026-07-18.csv"
    write_csv(target, "archived-version")
    target_receipt = archived_target(target)
    source_archive_identity = identity(source)
    replacement_target = None

    def replace_parent():
        nonlocal replacement_target
        if replacement_kind == "date-directory":
            displaced_source = root / "displaced-20260718"
            source.rename(displaced_source)
            source.mkdir()
            replacement_target = source / target.name
            write_csv(replacement_target, "replacement")
        else:
            displaced_root = tmp_path / "displaced-output"
            root.rename(displaced_root)
            source.mkdir(parents=True)
            replacement_target = source / target.name
            write_csv(replacement_target, "replacement")

    with pytest.raises(CleanupNotStartedError, match="changed"):
        run_scoped(
            root,
            source,
            (target_receipt,),
            source_identity=source_archive_identity,
            before_delete=replace_parent,
        )

    assert replacement_target is not None
    assert replacement_target.read_text(encoding="utf-8") == "replacement"
    displaced_matches = list(tmp_path.rglob("target_2026-07-18.csv"))
    assert len(displaced_matches) == 2
    assert sorted(path.read_text(encoding="utf-8") for path in displaced_matches) == [
        "archived-version",
        "replacement",
    ]


def test_scoped_cleanup_records_per_path_oserror_and_continues(tmp_path, monkeypatch):
    root, source = make_daily_layout(tmp_path)
    first = source / "a_2026-07-18.csv"
    second = source / "b_2026-07-18.csv"
    write_csv(first)
    write_csv(second)
    targets = (archived_target(first), archived_target(second))
    source_archive_identity = identity(source)
    original_unlink = os.unlink

    def selective_unlink(path, *args, **kwargs):
        if path == second.name:
            raise OSError("simulated unlink failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", selective_unlink)

    receipt = run_scoped(
        root,
        source,
        targets,
        source_identity=source_archive_identity,
    )

    expected = tuple(target.local_path for target in targets)
    assert receipt.requested_paths == expected
    assert receipt.deleted_paths == (expected[0],)
    assert receipt.failed_paths == (expected[1],)
    assert not first.exists()
    assert second.exists()


def test_legacy_retention_cleanup_keeps_exact_three_day_behavior_in_tmp_path(tmp_path):
    root = tmp_path / "output"
    old_dir = root / "old"
    recent_dir = root / "recent"
    old_dir.mkdir(parents=True)
    recent_dir.mkdir()
    old_file = old_dir / "old.csv"
    recent_file = recent_dir / "recent.csv"
    old_file.write_text("old", encoding="utf-8")
    recent_file.write_text("recent", encoding="utf-8")
    four_days_ago = time.time() - (4 * 86400)
    os.utime(old_file, (four_days_ago, four_days_ago))
    os.utime(old_dir, (four_days_ago, four_days_ago))

    clean_old_csv_files(retention_days=3, target_dir=str(root))

    assert not old_file.exists()
    assert old_dir.is_dir()
    assert list(old_dir.iterdir()) == []
    assert recent_file.exists()
