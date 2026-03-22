import sys
import os
import logging
from datetime import datetime

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.reporter import DailyReporter
from kiwoom_stock.utils import setup_structured_logging

from kiwoom_stock.core import config 
from kiwoom_stock.utils.market_cal import is_krx_open_today
from kiwoom_stock.utils.file_manager import clean_old_csv_files
from kiwoom_stock.utils.s3_manager import S3Manager

logger = logging.getLogger(__name__)

# 💡 길고 지저분한 datetime 코드를 람다 함수로 묶어버립니다.
get_now_str = lambda: datetime.now().strftime('%Y-%m-%d %H:%M:%S')
get_today_str = lambda: datetime.now().strftime('%Y-%m-%d')

def main():
    setup_structured_logging()
    logger.info("==== 시스템 부팅 ====")
    
    # 🛡️ [사전 차단기] 휴장일 검증
    if not is_krx_open_today():
        logger.info("🛑 휴장일로 판별되어 시스템(API 및 엔진)을 가동하지 않고 안전 종료합니다.")
        sys.exit(0)
        
    try:
        if not hasattr(config, 'CONFIG') or not hasattr(config, 'STRATEGY_CONFIG'):
            logger.critical("Config 로드 실패.")
            sys.exit(1)

        system_config = config.CONFIG
        strategy_params = config.STRATEGY_CONFIG
        app_config = {**system_config, **strategy_params}
        
        client = KiwoomClient(
                    appkey=app_config['appkey'],
                    secretkey=app_config['secretkey'],
                    base_url=app_config['base_url']
                )

        monitor = TradingEngine(client, app_config)

        # 🟢 [추가 1] 프로세스 시작
        monitor.notifier.send_slack(
            f"🚀 *[{app_config['process_name']}]* ({get_now_str()})\n올-웨더 모니터링 시스템이 정상적으로 부팅되었습니다."
        )

        logger.info("🚀 키움 증권 올-웨더 모니터링 시스템 가동 시작")

        # 메인 루프 실행
        monitor.run()

        # ========================================================
        # 🏁 사후 처리 1단계: 일일 부검 & 텔레메트리 (Slack)
        # (이 과정에서 추출된 CSV 파일들은 config.OUTPUT_DIR_STR에 쌓임)
        # ========================================================
        logger.info("🏁 엔진 구동 종료. 일일 자동 부검 파이프라인(Daily Post-Mortem)을 가동합니다.")
        reporter = DailyReporter(monitor.notifier)
        reporter.run_pipeline()
        
        # ========================================================
        # 🧹 사후 처리 2단계: S3 데이터 레이크 백업 및 가비지 컬렉션
        # ========================================================
        app_env = system_config.get("app_env", "local").lower()
        
        logger.info(f"🧹 장 마감 사후 처리 가동 (현재 환경: {app_env.upper()})")

        if app_env == "prod":
            logger.info("[Prod] 운영 환경입니다. S3 백업 후 로컬 파일을 파기합니다.")
            s3_bucket = system_config.get("aws_s3_bucket_name")
            if s3_bucket:
                s3 = S3Manager(bucket_name=s3_bucket)
                # config.OUTPUT_DIR_STR 에 모인 파일들을 S3로 동기화
                s3.sync_daily_outputs(target_date=get_today_str(), source_dir=config.OUTPUT_DIR_STR)
            
            # 운영 환경은 로컬 디스크 낭비를 막기 위해 당일 즉시 삭제 (0일)
            clean_old_csv_files(retention_days=0, target_dir=os.path.dirname(config.OUTPUT_DIR_STR))
            
        else:
            logger.info("[Local] 테스트 환경입니다. S3 업로드를 스킵하고 로컬에 3일간 보존합니다.")
            # 로컬은 직접 열어봐야 하므로 3일간 보존
            clean_old_csv_files(retention_days=3, target_dir=os.path.dirname(config.OUTPUT_DIR_STR))

        # 프로세스 정상 종료
        monitor.notifier.send_slack(
            f"🏁 *[{app_config['process_name']}]* ({get_now_str()})\n오늘의 모든 임무(매매/부검/백업)를 완벽하게 마치고 엔진을 안전하게 종료합니다."
        )

        logger.info("==== 오늘의 모든 임무 완료. 시스템 정상 종료 ====")

    except KeyboardInterrupt:
        logger.info("\n👋 사용자에 의해 시스템이 종료되었습니다.")
    except Exception as e:
        logger.error(f"❌ 시스템 가동 중 치명적 오류 발생: {e}", exc_info=True)
        if 'monitor' in locals() and hasattr(monitor, 'notifier'):
            try:
                monitor.notifier.send_slack(
                    f"🚨 *[{app_config['process_name']}]* ({get_now_str()})\n엔진 가동 중 치명적 오류가 발생하여 시스템이 다운되었습니다.\n```{e}```"
                )
            except:
                pass
        sys.exit(1)

if __name__ == "__main__":
    main()