# src/kiwoom_stock/main.py

import sys
from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.monitoring.engine import MultiTimeframeRSIMonitor
from kiwoom_stock.utils.config import load_config, get_base_url

def main():
    try:
        # 1. 설정 로드 (루트 폴더의 config.json 읽기)
        config = load_config()
        
        # 2. API 클라이언트 초기화 (인증 및 도메인 설정)
        # 문서에 명시된 운영 도메인(https://api.kiwoom.com)을 사용합니다.
        client = KiwoomClient(
            appkey=config['appkey'],
            secretkey=config['secretkey'],
            base_url=get_base_url()
        )
        
        # 3. 모니터링 엔진 초기화
        # MultiTimeframeRSIMonitor는 내부적으로 client.market 등을 사용합니다.
        monitor = MultiTimeframeRSIMonitor(client, config)
        
        print("🚀 키움 증권 올-웨더 모니터링 시스템을 시작합니다.")
        # print(f"계획: {config.get('check_interval')}초 간격으로 코스피 상위 종목 감시")
        
        # 4. 프로세스 실행
        monitor.run()
        
    except KeyboardInterrupt:
        print("\n👋 사용자에 의해 시스템이 종료되었습니다.")
    except Exception as e:
        print(f"❌ 시스템 가동 중 치명적 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()