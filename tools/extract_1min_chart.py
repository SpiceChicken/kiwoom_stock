import sys
import os
import pandas as pd
from datetime import datetime

# 프로젝트 루트 경로를 sys.path에 추가 (tools 폴더에서 실행 시 모듈 import 오류 방지)
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiwoom_stock.core import config 
from kiwoom_stock.api.client import KiwoomClient 
from kiwoom_stock.monitoring.collector import MarketDataCollector
from kiwoom_stock.core.database import TradeLogger

def main():
    # 1. API 클라이언트 초기화 및 인증
    print("🚀 키움 API 클라이언트 연결 중...")
    
    system_config = config.CONFIG
    client = KiwoomClient(
        appkey=system_config['appkey'],
        secretkey=system_config['secretkey'],
        base_url=system_config['base_url']
    )
    
    collector = MarketDataCollector(client)
    
    # 2. 검증할 타겟 종목 딕셔너리 (코드: 종목명)
    # trade_analysis_20260220.csv 에서 손절 처리된 대표 종목들

    db = TradeLogger()

    # 💡 하드코딩 대신 DB에서 동적으로 타겟 딕셔너리 생성
    targets = db.get_today_traded_targets()

    if not targets:
        print("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
        return

    targets = {
        (r['stock_code'], r['stock_name'])
        for r in targets
        if r['stock_code'] and r['stock_name']
    }

    print(f"오늘 거래된 종목 개수: {len(targets)}개")
    print(f"종목 타겟 목록: {targets}")

    today_str = datetime.now().strftime("%Y%m%d")
    directory_path = os.path.join("etc", today_str)

    # 디렉토리 없으면 생성
    if not os.path.exists(directory_path):
        os.makedirs(directory_path)
    
    for code, name in targets:
        print(f"\n📥 [{name}({code})] 1분봉 데이터 수집 시작...")
        
        # 3. Collector를 통한 1분봉 API 호출 (tic="1")
        raw_data = collector.fetch_minute_chart(code, tic="1")
        
        if not raw_data:
            print(f"❌ [{name}] 데이터를 불러오지 못했습니다. API 호출 한도나 장 마감 여부를 확인하세요.")
            continue
            
        # 4. DataFrame 변환
        df = pd.DataFrame(raw_data)
        
        # API 응답은 보통 최신순(과거로 갈수록 아래)이므로, 시계열 분석을 위해 오름차순(과거->최신)으로 뒤집기
        df = df.iloc[::-1].reset_index(drop=True)
        
        # 보기 편하게 주요 컬럼명 매핑 (API 응답 필드명에 따라 실제 CSV 출력 시 확인 필요)
        # 키움 API 응답 기준: cur_prc(현재가), high_pric(고가), low_pric(저가), trde_qty(거래량)
        
        # 5. CSV 저장
        today_str = datetime.now().strftime("%Y%m%d")
        filename = f"{name}_{code}_1min_{today_str}.csv"
                
        # 안전하게 디렉토리 경로와 파일명을 결합
        file_path = os.path.join(directory_path, filename)
        
        # 한글 깨짐 방지를 위해 utf-8-sig 인코딩 사용
        df.to_csv(file_path, index=False, encoding='utf-8-sig')
        print(f"✅ 저장 완료: {file_path} (총 {len(df)}개 분봉 데이터)")

if __name__ == "__main__":
    main()