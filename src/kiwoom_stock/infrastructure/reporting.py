"""Concrete data and CSV adapters for post-market reporting."""

import logging
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import pandas as pd

from kiwoom_stock.application.reporting import ReportArtifact


class _ReadOnlySQLiteReportDatabase:
    """Minimal, query-only database used by production reporting reads."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise FileNotFoundError(f"report database does not exist: {path}")
        self._connection = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro", uri=True
        )
        self._connection.row_factory = sqlite3.Row

    def get_today_traded_targets(self, target_date: str):
        query = """SELECT DISTINCT * FROM trades
                   WHERE buy_time LIKE ? OR sell_time LIKE ?"""
        return self._connection.execute(
            query, (f"{target_date}%", f"{target_date}%")
        ).fetchall()

    def close(self) -> None:
        self._connection.close()


class TradeLoggerReportDataSource:
    """Read copied trade rows from one explicitly configured database path."""

    def __init__(
        self,
        database_path: Path,
        *,
        database_factory: Any = None,
        target_logger: Optional[logging.Logger] = None,
    ) -> None:
        self._database_path = database_path
        self._database_factory = database_factory or _ReadOnlySQLiteReportDatabase
        self._logger = (
            target_logger
            if target_logger is not None
            else logging.getLogger(__name__)
        )

    def load_trades(self, target_date: str) -> Tuple[Mapping[str, Any], ...]:
        """Materialize rows, then close the database before returning them."""

        database = self._database_factory(self._database_path)
        query_error: Optional[BaseException] = None
        try:
            return tuple(
                dict(row)
                for row in database.get_today_traded_targets(target_date)
            )
        except BaseException as error:
            query_error = error
            raise
        finally:
            try:
                database.close()
            except BaseException as close_error:
                if query_error is None:
                    raise
                self._logger.critical(
                    "report DB close failed while preserving query error: %s",
                    close_error,
                    exc_info=(
                        type(close_error),
                        close_error,
                        close_error.__traceback__,
                    ),
                )
                query_error.add_note(
                    f"report DB close also failed: {close_error}"
                )


class CollectorMinuteChartSource:
    """Adapt the existing market collector to the minute-chart source port."""

    def __init__(self, collector: Any) -> None:
        self._collector = collector

    def load_minutes(
        self,
        stock_code: str,
        target_date: str,
    ) -> Tuple[Mapping[str, Any], ...]:
        """Return copied rows; date filtering belongs to the artifact store."""

        del target_date
        rows = self._collector.fetch_minute_chart(stock_code, tic="1")
        if not rows:
            return ()
        return tuple(
            dict(row)
            for row in rows
        )


class CsvReportArtifactStore:
    """Persist the two legacy CSV artifact shapes in one output directory."""

    _TIME_COLUMNS = ("체결시간", "cntr_tm", "dt", "date")

    def __init__(
        self,
        output_dir: Path,
        *,
        target_logger: Optional[logging.Logger] = None,
    ) -> None:
        self._output_dir = output_dir
        self._logger = (
            target_logger
            if target_logger is not None
            else logging.getLogger(__name__)
        )

    def save_minute_chart(
        self,
        *,
        stock_code: str,
        stock_name: str,
        target_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> Optional[ReportArtifact]:
        """Filter, reverse, and persist one exact legacy minute-chart CSV."""

        frame = pd.DataFrame(tuple(dict(row) for row in rows))
        time_column = next(
            (
                column
                for column in self._TIME_COLUMNS
                if column in frame.columns
            ),
            None,
        )
        filtered = self._filter_minute_rows(
            frame,
            target_date=target_date,
            time_column=time_column,
        )
        if filtered.empty:
            self._logger.warning(
                "❌ [%s] 당일 1분봉 거래 데이터가 없습니다.",
                stock_name,
            )
            return None

        final_frame = filtered.iloc[::-1].reset_index(drop=True)
        filename = f"{stock_name}_{stock_code}_1min_{target_date}.csv"
        path = self._output_dir / filename
        final_frame.to_csv(path, index=False, encoding="utf-8-sig")
        self._logger.info("✅ 저장 완료: %s", path)
        return ReportArtifact(
            kind="minute_chart",
            logical_name=filename,
            reference=str(path),
        )

    def save_trade_analysis(
        self,
        *,
        target_date: str,
        rows: Sequence[Mapping[str, Any]],
    ) -> Optional[ReportArtifact]:
        """Persist already analyzed copied rows with the legacy schema order."""

        copied_rows = tuple(dict(row) for row in rows)
        if not copied_rows:
            return None

        filename = f"physics_trade_analysis_{target_date}.csv"
        path = self._output_dir / filename
        pd.DataFrame(copied_rows).to_csv(
            path,
            index=False,
            encoding="utf-8-sig",
        )
        self._logger.info(
            "✅ 매매 분석 리포트 저장 완료: %s",
            path,
        )
        return ReportArtifact(
            kind="trade_analysis",
            logical_name=filename,
            reference=str(path),
        )

    def _filter_minute_rows(
        self,
        frame: pd.DataFrame,
        *,
        target_date: str,
        time_column: Optional[str],
    ) -> pd.DataFrame:
        if time_column is None:
            return frame

        time_series = frame[time_column].astype(str)
        mask = time_series.str.startswith(target_date.replace("-", ""))
        filtered = frame.loc[mask].copy()
        self._logger.info(
            "   -> %s 데이터 필터링 적용: %s개 분봉 추출됨",
            target_date,
            len(filtered),
        )
        return filtered


def read_traded_targets(
    target_date_str: str,
    *,
    database_path: Path,
    database_factory: Any,
    target_logger: logging.Logger,
) -> Tuple[Mapping[str, Any], ...]:
    """Compatibility callable backed by the single report data adapter."""

    return TradeLoggerReportDataSource(
        database_path,
        database_factory=database_factory,
        target_logger=target_logger,
    ).load_trades(target_date_str)
