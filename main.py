# src/kiwoom_stock/main.py

import sys
import logging

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.monitoring.engine import TradingEngine
from kiwoom_stock.monitoring.reporter import DailyReporter
from kiwoom_stock.utils import setup_structured_logging

from kiwoom_stock.core import config 

from kiwoom_stock.monitoring.reporter import DailyReporter

# 로거 설정
logger = logging.getLogger(__name__)

def main():
    # 1. 로깅 시스템 초기화 (콘솔 출력 + 파일 적재)
    setup_structured_logging()
    
    try:
        # 2. 설정 파일 로드
        # 2-1. 시스템 설정 (API 키, URL 등)
        if not hasattr(config, 'CONFIG') or not hasattr(config, 'STRATEGY_CONFIG'):
            logger.critical("Config 로드 실패.")
            sys.exit(1)

        # 2. 변수 할당 (파일명이 대문자 변수가 됨)
        system_config = config.CONFIG           # from config/config.json
        strategy_params = config.STRATEGY_CONFIG # from config/strategy_config.json

        # 2-3. 설정 통합
        app_config = {**system_config, **strategy_params}
        
        # 클라이언트 생성 시점에 이미 _wait_for_ready()를 통해 
        # 인터넷이 연결되고 토큰 발급까지 완료된 상태임이 보장됩니다.
        client = KiwoomClient(
                    appkey=app_config['appkey'],
                    secretkey=app_config['secretkey'],
                    base_url=app_config['base_url']
                )

        # 엔진 초기화 (이후 발생하는 에러는 네트워크가 아닌 로직 에러임)
        monitor = TradingEngine(client, app_config)
        
        logger.info("🚀 키움 증권 올-웨더 모니터링 시스템 가동 시작")

        # 프로세스 실행
        monitor.run()

        logger.info("🏁 엔진 구동 종료. 일일 자동 부검 파이프라인(Daily Post-Mortem)을 가동합니다.")
        reporter = DailyReporter(monitor.notifier)
        reporter.run_pipeline()
        
    except KeyboardInterrupt:
        print("\n👋 사용자에 의해 시스템이 종료되었습니다.")
    except Exception as e:
        print(f"❌ 시스템 가동 중 치명적 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()