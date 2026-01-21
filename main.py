# src/kiwoom_stock/main.py

import sys
import time

from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.monitoring.engine import MultiTimeframeRSIMonitor
from kiwoom_stock.utils.config import load_config, get_base_url

def main():
    try:
        # 1. 설정 로드 (루트 폴더의 config.json 읽기)
        config = load_config()

        # 2. 네트워크 및 서버 연결 대기 로직 (Retry)
        max_retries = 10
        retry_delay = 10  # 10초 간격으로 시도
        client = None
        
        # 3. API 클라이언트 초기화 (인증 및 도메인 설정)
        # 문서에 명시된 운영 도메인(https://api.kiwoom.com)을 사용합니다.
        for i in range(max_retries):
            try:
                client = KiwoomClient(
                    appkey=config['appkey'],
                    secretkey=config['secretkey'],
                    base_url=get_base_url()
                )
                print("✅ 서버 연결 및 인증에 성공했습니다.")
                break
            except Exception as e:
                print(f"⚠️ 연결 시도 중 ({i+1}/{max_retries}): {e}")
                if i < max_retries - 1:
                    print(f"ℹ️ {retry_delay}초 후 다시 시도합니다...")
                    time.sleep(retry_delay)
                else:
                    print("❌ 네트워크 연결 실패로 프로그램을 종료합니다.")
                    sys.exit(1)

        # 4. 모니터링 엔진 초기화
        # MultiTimeframeRSIMonitor는 내부적으로 client.market 등을 사용합니다.
        monitor = MultiTimeframeRSIMonitor(client, config)
        
        print("🚀 키움 증권 올-웨더 모니터링 시스템을 시작합니다.")

        # 5. 프로세스 실행
        monitor.run()
        
    except KeyboardInterrupt:
        print("\n👋 사용자에 의해 시스템이 종료되었습니다.")
    except Exception as e:
        print(f"❌ 시스템 가동 중 치명적 오류 발생: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()