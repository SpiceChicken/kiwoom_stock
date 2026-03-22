import os
import pandas as pd
import logging
from datetime import datetime

from kiwoom_stock.core import config 
from kiwoom_stock.api.client import KiwoomClient 
from kiwoom_stock.monitoring.collector import MarketDataCollector
from kiwoom_stock.core.database import TradeLogger

# 💡 [V3.1] 로거 초기화
logger = logging.getLogger(__name__)

def extract_and_save_1min_chart(target_date_str: str = None):
    # 인자가 없으면 오늘 날짜('%Y-%m-%d') 사용
    if target_date_str is None:
        target_date_str = datetime.now().strftime('%Y-%m-%d')

    logger.info("🚀 키움 API 클라이언트 연결 중...")
    
    system_config = config.CONFIG
    client = KiwoomClient(
        appkey=system_config['appkey'],
        secretkey=system_config['secretkey'],
        base_url=system_config['base_url']
    )
    
    collector = MarketDataCollector(client)
    db = TradeLogger()
    targets = db.get_today_traded_targets(target_date_str)

    if not targets:
        # 정상적인 빈 결과이므로 info 처리
        logger.info("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
        return []

    targets = {
        (r['stock_code'], r['stock_name'])
        for r in targets
        if r['stock_code'] and r['stock_name']
    }

    logger.info(f"오늘 거래된 종목 개수: {len(targets)}개")
    
    directory_path = config.OUTPUT_DIR_STR
    saved_files = [] 
    
    for code, name in targets:
        logger.info(f"📥 [{name}({code})] 1분봉 데이터 수집 시작...")
        raw_data = collector.fetch_minute_chart(code, tic="1")
        
        if not raw_data:
            # API 호출 실패이므로 error 처리
            logger.error(f"❌ [{name}] 데이터를 불러오지 못했습니다. API 호출 한도나 장 마감 여부를 확인하세요.")
            continue
            
        df = pd.DataFrame(raw_data)
        
        time_col = next((col for col in ['체결시간', 'cntr_tm', 'dt', 'date'] if col in df.columns), None)
        if time_col:
            df = df[df[time_col].astype(str).str.startswith(target_date_str.replace('-', ''))]
            logger.info(f"   -> {target_date_str} 데이터 필터링 적용: {len(df)}개 분봉 추출됨")
        
        if df.empty:
            # 데이터가 비어있으므로 warning 처리
            logger.warning(f"❌ [{name}] 당일 1분봉 거래 데이터가 없습니다.")
            continue
        
        df = df.iloc[::-1].reset_index(drop=True)
        
        filename = f"{name}_{code}_1min_{target_date_str}.csv"
        file_path = os.path.join(directory_path, filename) 
        
        df.to_csv(file_path, index=False, encoding='utf-8-sig')
        saved_files.append(file_path) 
        logger.info(f"✅ 저장 완료: {file_path}")
    
    return saved_files

if __name__ == "__main__":
    extract_and_save_1min_chart()