import os
import pandas as pd
import logging
from datetime import datetime
from typing import Optional

from kiwoom_stock.core import config
from kiwoom_stock.core.database import TradeLogger

# 💡 [V3.1] 로거 초기화
logger = logging.getLogger(__name__)

def analyze_trade_efficiency(target_date_str: Optional[str] = None):
    if target_date_str is None:
        target_date_str = datetime.now().strftime('%Y-%m-%d')
    
    db = TradeLogger()
    targets = db.get_today_traded_targets(target_date_str)

    if not targets:
        logger.info("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
        return None

    analysis_results = []
    for row in targets:
        forces = {
            "Thrust": row['thrust'],
            "Gravity": row['gravity'],
            "Drag": row['drag'],
            "Magnetic": row['magnetic'],
            "Jerk": row['jerk'],
            "Impulse": row['impulse']
        }
        net_force = row['net_force']
        
        positive_forces = {k: v for k, v in forces.items() if v > 0}
        primary_driver = max(positive_forces, key=positive_forces.get) if positive_forces else "None"
        profit = row['profit_rate']
        
        if profit > 2.0:
            judgement = "🎯 정밀타격" if net_force >= 1.0 else "🤔 요행(가속부족)"
        elif profit < -2.0:
            judgement = "❌ 엔진과열(오판)" if net_force >= 1.0 else "⚠️ 억지진입(동력부족)"
        else:
            judgement = "➖ 보합(마찰 상쇄)"

        row_dict = dict(row)
        row_dict['primary_driver'] = primary_driver
        row_dict['judgement'] = judgement
        analysis_results.append(row_dict)

    if analysis_results:
        result_df = pd.DataFrame(analysis_results)
        filename = f"physics_trade_analysis_{target_date_str}.csv"
        
        file_path = os.path.join(config.OUTPUT_DIR_STR, filename)
        result_df.to_csv(file_path, index=False, encoding='utf-8-sig')
        
        logger.info(f"✅ 매매 분석 리포트 저장 완료: {file_path}")
        
        return file_path 

    return None

if __name__ == "__main__":
    analyze_trade_efficiency()