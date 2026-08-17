"""Export the legacy one-minute charts for the current trade targets."""

from datetime import datetime
import logging
from pathlib import Path
from typing import Any, List, Optional

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.application.credential_preflight import preflight_environment
from kiwoom_stock.infrastructure.kiwoom_credentials import (
    StrictFileCredentialProvider,
    credential_repository_boundary,
)
from kiwoom_stock.infrastructure.kiwoom_market_only import (
    KiwoomMarketDataGatewayAdapter,
)
from kiwoom_stock.application.runtime import RuntimeDisabledError
from kiwoom_stock.core import config
from kiwoom_stock.core.config import report_output_dir_for
from kiwoom_stock.infrastructure.reporting import (
    CollectorMinuteChartSource,
    CsvReportArtifactStore,
    read_traded_targets as _read_traded_targets,
)
from kiwoom_stock.monitoring.collector import MarketDataCollector


logger = logging.getLogger(__name__)


def _strict_provider(credentials_dir: Path) -> StrictFileCredentialProvider:
    return StrictFileCredentialProvider(
        credentials_dir,
        repository_root=credential_repository_boundary(),
    )


def _extract_and_save_1min_chart(
    target_date_str: Optional[str],
    *,
    config_module: Any,
    datetime_type: Any,
    client_factory: Any,
    collector_factory: Any,
    database_factory: Any,
    target_logger: logging.Logger,
    credential_provider_factory: Any = _strict_provider,
) -> List[str]:
    """Run the implementation with explicit dependencies for legacy wrappers."""

    preflight = preflight_environment(
        config_module,
        credential_provider_factory,
    )
    settings = preflight.settings

    if settings.kiwoom.api_mode == "disabled":
        raise RuntimeDisabledError(
            "KIWOOM_API_MODE=disabled permits configuration checks only"
        )
    now = datetime_type.now()
    settings = config_module.activate_runtime_settings(settings, today=now.date())
    if target_date_str is None:
        target_date_str = now.strftime("%Y-%m-%d")

    target_logger.info("🚀 키움 API 클라이언트 연결 중...")
    endpoint = settings.kiwoom.endpoint
    credentials = preflight.credentials
    if endpoint is None or credentials is None:
        raise ValueError("enabled credential preflight is incomplete")
    client = client_factory(credentials=credentials, endpoint=endpoint)
    primary_error: Optional[BaseException] = None
    try:
        market_gateway = KiwoomMarketDataGatewayAdapter.from_client(client)
        market_gateway.preflight()
        collector = collector_factory(market_gateway)
        target_rows = _read_traded_targets(
            target_date_str,
            database_path=settings.database.path,
            database_factory=database_factory,
            target_logger=target_logger,
        )

        if not target_rows:
            target_logger.info("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
            return []

        targets = {
            (row["stock_code"], row["stock_name"])
            for row in target_rows
            if row["stock_code"] and row["stock_name"]
        }

        target_logger.info(f"오늘 거래된 종목 개수: {len(targets)}개")

        minute_source = CollectorMinuteChartSource(collector)
        artifact_store = CsvReportArtifactStore(
            report_output_dir_for(settings, now.date(), config_module),
            target_logger=target_logger,
        )
        saved_files = []

        for code, name in targets:
            target_logger.info(f"📥 [{name}({code})] 1분봉 데이터 수집 시작...")
            raw_data = minute_source.load_minutes(code, target_date_str)

            if not raw_data:
                target_logger.error(
                    f"❌ [{name}] 데이터를 불러오지 못했습니다. API 호출 한도나 장 마감 여부를 확인하세요."
                )
                continue

            artifact = artifact_store.save_minute_chart(
                stock_code=code,
                stock_name=name,
                target_date=target_date_str,
                rows=raw_data,
            )
            if artifact is None:
                continue
            saved_files.append(artifact.reference)

        return saved_files
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            client.close()
        except BaseException as close_error:
            if primary_error is None:
                raise
            target_logger.error(
                "Kiwoom client local close failed while preserving %s: %s",
                type(primary_error).__name__,
                type(close_error).__name__,
            )
            primary_error.add_note(
                "Kiwoom client local close also failed with "
                f"{type(close_error).__name__}"
            )


def extract_and_save_1min_chart(target_date_str: Optional[str] = None) -> List[str]:
    """Export charts using the production compatibility dependencies."""

    return _extract_and_save_1min_chart(
        target_date_str,
        config_module=config,
        datetime_type=datetime,
        client_factory=KiwoomClient,
        collector_factory=MarketDataCollector,
        credential_provider_factory=_strict_provider,
        database_factory=None,
        target_logger=logger,
    )
