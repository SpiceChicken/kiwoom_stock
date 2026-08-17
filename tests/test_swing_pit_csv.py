import csv
from datetime import date
import io
import json
from pathlib import Path

import pytest

from kiwoom_stock.application.swing_replay import ReplayDataError
from kiwoom_stock.infrastructure.point_in_time_replay import (
    CsvPITReplaySource,
    SWING_PIT_REPLAY_COLUMNS,
    SWING_PIT_REPLAY_SCHEMA,
)


HEADER = ",".join(SWING_PIT_REPLAY_COLUMNS)


def _row(
    event_id: str = "event-1",
    *,
    decision_at: str = "2026-08-18T09:00:00+09:00",
    available_at: str = "2026-08-18T08:59:00+09:00",
    payload: object = None,
) -> str:
    payload_value = {"value": 1} if payload is None else payload
    stream = io.StringIO()
    csv.writer(stream, lineterminator="").writerow(
        (
            SWING_PIT_REPLAY_SCHEMA,
            event_id,
            "2026-08-18",
            decision_at,
            available_at,
            f"snapshot-{event_id}",
            json.dumps(payload_value, separators=(",", ":")),
        )
    )
    return stream.getvalue()


def _write(path: Path, body: str) -> Path:
    path.write_text(f"{HEADER}\n{body}\n", encoding="utf-8-sig")
    return path


def test_csv_pit_loader_reads_standard_artifact_without_writing(tmp_path):
    path = tmp_path / "output" / "20260818" / "pit.csv"
    path.parent.mkdir(parents=True)
    _write(path, _row())

    source = CsvPITReplaySource.from_artifact(
        output_root=tmp_path,
        session_date=date(2026, 8, 18),
        filename="pit.csv",
        dataset_id="approved-pit-v1",
    )

    assert source.path == path
    assert source.dataset_id == "approved-pit-v1"
    assert source.events[0].event_id == "event-1"
    assert source.events[0].payload == {"value": 1}
    assert source.as_point_in_time_source().events == source.events


def test_pit_csv_header_schema_and_bytes_are_literal_golden(tmp_path):
    path = tmp_path / "pit.csv"
    body = _row()
    _write(path, body)

    assert HEADER == (
        "schema_version,event_id,session_date,decision_at,available_at,"
        "source_snapshot_id,payload_json"
    )
    assert path.read_bytes() == (
        b"\xef\xbb\xbfschema_version,event_id,session_date,decision_at,available_at,"
        b"source_snapshot_id,payload_json\n"
        b"swing-pit-replay-v1,event-1,2026-08-18,"
        b"2026-08-18T09:00:00+09:00,2026-08-18T08:59:00+09:00,"
        b"snapshot-event-1,\"{\"\"value\"\":1}\"\n"
    )


def test_csv_pit_loader_rejects_missing_or_relative_path_without_creating_file(tmp_path):
    missing = tmp_path / "missing.csv"
    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load(missing, dataset_id="dataset")
    assert not missing.exists()

    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load("relative.csv", dataset_id="dataset")


@pytest.mark.parametrize(
    "body",
    (
        _row(payload=[]),
        _row(payload="text"),
        _row(payload="{"),
        _row(decision_at="2026-08-18T09:00:00"),
        _row(available_at="2026-08-18T09:01:00+09:00"),
        _row(event_id=""),
    ),
)
def test_csv_pit_loader_rejects_invalid_rows(tmp_path, body):
    path = _write(tmp_path / "pit.csv", body)

    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load(path, dataset_id="dataset")


def test_csv_pit_loader_requires_exact_header_and_ordered_unique_events(tmp_path):
    bad_header = tmp_path / "bad-header.csv"
    bad_header.write_text(
        "event_id,schema_version,session_date,decision_at,available_at,source_snapshot_id,payload_json\n"
        f"{_row()}\n",
        encoding="utf-8",
    )
    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load(bad_header, dataset_id="dataset")

    unordered = _write(
        tmp_path / "unordered.csv",
        "\n".join(
            (
                _row("event-2", decision_at="2026-08-18T09:01:00+09:00", available_at="2026-08-18T09:00:00+09:00"),
                _row("event-1"),
            )
        ),
    )
    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load(unordered, dataset_id="dataset")

    duplicate = _write(
        tmp_path / "duplicate.csv",
        "\n".join((_row(), _row())),
    )
    with pytest.raises(ReplayDataError):
        CsvPITReplaySource.load(duplicate, dataset_id="dataset")
