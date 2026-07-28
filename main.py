import sys
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from kiwoom_stock.application.lifecycle import (
    notify_monitor_crashed,
    notify_monitor_started,
    run_post_market_tasks,
)
from kiwoom_stock.application.ports import (
    PaperTradeLedger,
    PhysicalStateRepository,
)
from kiwoom_stock.application.session import TradingSessionResult
from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.application.runtime import create_trading_runtime
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.reporter import DailyReporter
from kiwoom_stock.application.reporting_composition import build_daily_reporter
from kiwoom_stock.utils import setup_preflight_logging, setup_structured_logging
from kiwoom_stock.utils.file_manager import (
    clean_archived_csv_files,
    clean_old_csv_files,
)
from kiwoom_stock.utils.s3_manager import S3Manager

from kiwoom_stock.core import config
from kiwoom_stock.settings import Settings, SettingsValidationError
from kiwoom_stock.utils.market_cal import is_krx_open_on

logger = logging.getLogger(__name__)
_DEFAULT_DAILY_REPORTER = DailyReporter


def get_now_str() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def get_today_str() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _create_engine(
    client: Any,
    app_config: Dict[str, Any],
    *,
    ledger: PaperTradeLedger,
    physical_state_repository: PhysicalStateRepository,
) -> TradingEngine:
    """Preserve the public engine patch point behind the typed runtime seam."""

    return TradingEngine(
        client,
        app_config,
        ledger=ledger,
        physical_state_repository=physical_state_repository,
    )


def _prepare_startup(
    today_provider: Optional[Callable[[], date]],
    market_calendar: Optional[Callable[[date], bool]],
) -> tuple[Settings, date]:
    try:
        prevalidated_settings = config.validate_environment_settings()
    except SettingsValidationError as error:
        logger.error("%s", error)
        raise SystemExit(1)
    except Exception as error:
        logger.error(f"❌ 시스템 가동 중 치명적 오류 발생: {error}", exc_info=True)
        raise SystemExit(1)

    date_provider = (
        today_provider if today_provider is not None else lambda: datetime.now().date()
    )
    calendar_check = market_calendar if market_calendar is not None else is_krx_open_on

    try:
        startup_date = date_provider()
        is_market_open = calendar_check(startup_date)
    except Exception as error:
        logger.error(f"❌ 시스템 가동 중 치명적 오류 발생: {error}", exc_info=True)
        raise SystemExit(1)

    if not is_market_open:
        logger.info("🛑 휴장일로 판별되어 시스템(API 및 엔진)을 가동하지 않고 안전 종료합니다.")
        raise SystemExit(0)

    return prevalidated_settings, startup_date


def _log_config_warnings(settings: Settings) -> None:
    for warning in settings.diagnostics.warnings:
        logger.warning(f"[Config Migration] {warning}")


def _log_close_failure_preserving_primary(
    primary_error: BaseException,
    close_error: BaseException,
) -> None:
    logger.critical(
        "monitor close also failed while preserving the primary error: %s",
        close_error,
        exc_info=(type(close_error), close_error, close_error.__traceback__),
    )
    primary_error.add_note(f"monitor close also failed: {close_error}")


def _close_runtime_client(client: Any) -> None:
    """Close the local API owner after all post-market consumers finish."""

    if client is None:
        return
    primary_error = sys.exception()
    try:
        client.close()
    except BaseException as close_error:
        if primary_error is None:
            raise
        logger.critical(
            "Kiwoom client close also failed while preserving %s: %s",
            type(primary_error).__name__,
            type(close_error).__name__,
            exc_info=(type(close_error), close_error, close_error.__traceback__),
        )
        primary_error.add_note(
            "Kiwoom client close also failed with "
            f"{type(close_error).__name__}"
        )


def _reporter_factory_for_runtime(runtime: Any, monitor: Any) -> Callable[[Any], Any]:
    """Create the post-market reporter without doing I/O during import.

    The legacy class remains a patch seam for process-level tests and downstream
    callers; production uses the explicit typed composition graph.
    """
    if DailyReporter is not _DEFAULT_DAILY_REPORTER:
        return DailyReporter

    settings = runtime.settings
    database_path = settings.database.path
    output_dir = Path(runtime.output_dir_str)
    def factory(notifier: Any) -> Any:
        # Resolve the collector only when the reporter is actually built.  A
        # monitor may intentionally be a lightweight test double (or a
        # session that never reaches post-market work), so composing the
        # factory must not require the full analyzer graph up front.
        collector = monitor.analyzer.collector
        return build_daily_reporter(
            database_path=database_path,
            output_dir=output_dir,
            market_collector=collector,
            narrator=notifier.ai_client,
            publisher=notifier,
            clock=lambda: datetime.now(),
        )

    return factory


def main(
    *,
    today_provider: Optional[Callable[[], date]] = None,
    market_calendar: Optional[Callable[[date], bool]] = None,
) -> None:
    setup_preflight_logging()
    logger.info("==== 시스템 부팅 ====")
    prevalidated_settings, startup_date = _prepare_startup(today_provider, market_calendar)

    runtime: Optional[Any] = None
    try:
        runtime = create_trading_runtime(
            today=startup_date,
            config_module=config,
            client_factory=KiwoomClient,
            engine_factory=_create_engine,
            prevalidated_settings=prevalidated_settings,
        )
        monitor = runtime.monitor
        close_attempted = False

        try:
            settings = runtime.settings
            app_config = runtime.app_config

            setup_structured_logging()
            _log_config_warnings(settings)

            # 🟢 [추가 1] 프로세스 시작
            notify_monitor_started(
                monitor.notifier,
                process_name=app_config['process_name'],
                now_text=get_now_str(),
            )

            logger.info("🚀 키움 증권 올-웨더 모니터링 시스템 가동 시작")

            # 메인 루프 실행
            try:
                session_result = monitor.run()
            except KeyboardInterrupt as run_interrupt:
                close_attempted = True
                try:
                    monitor.close()
                except BaseException as close_error:
                    _log_close_failure_preserving_primary(run_interrupt, close_error)
                    raise run_interrupt

                logger.info("\n👋 사용자에 의해 시스템이 종료되었습니다.")
                return

            if not isinstance(session_result, TradingSessionResult):
                raise TypeError("TradingEngine.run() must return TradingSessionResult")

            close_attempted = True
            try:
                monitor.close()
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as close_error:
                if (
                    isinstance(session_result, TradingSessionResult)
                    and not session_result.post_market_allowed
                ):
                    logger.critical(
                        "kill-switch shutdown cleanup failed: %s",
                        close_error,
                        exc_info=(
                            type(close_error),
                            close_error,
                            close_error.__traceback__,
                        ),
                    )
                    raise SystemExit(1) from close_error
                raise

            if not session_result.post_market_allowed:
                logger.critical(
                    "Trading session stopped by %s; unresolved positions=%d; exit=%d",
                    session_result.reason.value,
                    len(session_result.unresolved_position_codes),
                    session_result.exit_code,
                )
                raise SystemExit(session_result.exit_code)

            run_post_market_tasks(
                notifier=monitor.notifier,
                process_name=app_config['process_name'],
                now_text=get_now_str(),
                today_text=get_today_str(),
                app_env=app_config.get("app_env", "local"),
                output_dir_str=runtime.output_dir_str,
                s3_bucket=app_config.get("aws_s3_bucket_name"),
                reporter_factory=_reporter_factory_for_runtime(runtime, monitor),
                s3_factory=S3Manager,
                cleanup_files=clean_old_csv_files,
                scoped_cleanup=clean_archived_csv_files,
            )

            logger.info("==== 오늘의 모든 임무 완료. 시스템 정상 종료 ====")
        except BaseException as primary_error:
            if not close_attempted:
                close_attempted = True
                try:
                    monitor.close()
                except BaseException as close_error:
                    _log_close_failure_preserving_primary(primary_error, close_error)
            raise
    except KeyboardInterrupt:
        raise
    except Exception as e:
        logger.error(f"❌ 시스템 가동 중 치명적 오류 발생: {e}", exc_info=True)
        if 'monitor' in locals() and hasattr(monitor, 'notifier'):
            try:
                notify_monitor_crashed(
                    monitor.notifier,
                    process_name=app_config['process_name'],
                    now_text=get_now_str(),
                    error=e,
                )
            except Exception:
                pass
        sys.exit(1)
    finally:
        if runtime is not None:
            _close_runtime_client(getattr(runtime, "client", None))


if __name__ == "__main__":
    main()
