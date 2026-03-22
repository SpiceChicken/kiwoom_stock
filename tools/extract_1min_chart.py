import os
import pandas as pd
from datetime import datetime

from kiwoom_stock.core import config 
from kiwoom_stock.api.client import KiwoomClient 
from kiwoom_stock.monitoring.collector import MarketDataCollector
from kiwoom_stock.core.database import TradeLogger

def extract_and_save_1min_chart():
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

    saved_files = []  # <- 저장된 파일 경로를 모을 리스트
    
    for code, name in targets:
        print(f"\n📥 [{name}({code})] 1분봉 데이터 수집 시작...")
        
        # 3. Collector를 통한 1분봉 API 호출 (tic="1")
        raw_data = collector.fetch_minute_chart(code, tic="1")
        
        if not raw_data:
            print(f"❌ [{name}] 데이터를 불러오지 못했습니다. API 호출 한도나 장 마감 여부를 확인하세요.")
            continue
            
        # 4. DataFrame 변환
        df = pd.DataFrame(raw_data)
        
        # 💡 [V3.1] 900틱 중 당일(Today) 데이터만 추출 (체결시간 기준 필터링)
        # 키움증권 분봉 TR(opt10080)의 시간 컬럼('체결시간' 또는 'cntr_tm', 'dt' 등) 동적 탐색
        time_col = next((col for col in ['체결시간', 'cntr_tm', 'dt', 'date'] if col in df.columns), None)
        
        if time_col:
            # 문자열로 변환 후 오늘 날짜(YYYYMMDD)로 시작하는 row만 필터링
            df = df[df[time_col].astype(str).str.startswith(today_str)]
            print(f"   -> 당일({today_str}) 데이터 필터링 적용: {len(df)}개 분봉 추출됨")
        else:
            print(f"   -> ⚠️ 시간 컬럼을 찾을 수 없어 전체(900틱) 데이터를 유지합니다. (컬럼: {list(df.columns)})")
            
        # 필터링 후 오늘 거래 데이터가 없는 경우 스킵
        if df.empty:
            print(f"❌ [{name}] 당일 1분봉 거래 데이터가 없습니다.")
            continue
        
        # API 응답은 보통 최신순(과거로 갈수록 아래)이므로, 시계열 분석을 위해 오름차순(과거->최신)으로 뒤집기
        df = df.iloc[::-1].reset_index(drop=True)
        
        # 5. CSV 저장
        filename = f"{name}_{code}_1min_{today_str}.csv"
                
        # 안전하게 디렉토리 경로와 파일명을 결합
        file_path = os.path.join(directory_path, filename)
        
        # 한글 깨짐 방지를 위해 utf-8-sig 인코딩 사용
        df.to_csv(file_path, index=False, encoding='utf-8-sig')
        saved_files.append(file_path)  # <- 추가
        print(f"✅ 저장 완료: {file_path} (총 {len(df)}개 분봉 데이터)")
    
    return saved_files

if __name__ == "__main__":
    extract_and_save_1min_chart()