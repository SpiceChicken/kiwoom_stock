import pandas as pd
from datetime import datetime
from kiwoom_stock.core.database import TradeLogger

def analyze_trade_efficiency():
    db = TradeLogger()

    # 💡 하드코딩 대신 DB에서 동적으로 타겟 딕셔너리 생성
    targets = db.get_today_traded_targets()

    if not targets:
        print("오늘 거래된 종목이 없습니다. 스크립트를 종료합니다.")
        return

    analysis_results = []

    for row in targets:
        # 1. 7대 물리 벡터 모두 추출
        forces = {
            "Thrust": row['thrust'],
            "Gravity": row['gravity'],
            "Drag": row['drag'],
            "Magnetic": row['magnetic'],
            "Jerk": row['jerk'],
            "Impulse": row['impulse']
        }
        net_force = row['net_force']
        
        # 2. 가장 강하게 작용한 양의 벡터(상승 주동력) 추출
        positive_forces = {k: v for k, v in forces.items() if v > 0}
        primary_driver = max(positive_forces, key=positive_forces.get) if positive_forces else "None"
        
        profit = row['profit_rate']
        
        # 3. 판정 로직: Net Force(합력)가 1.0 이상인 강력한 물리적 돌파 자리였는가?
        if profit > 2.0:
            judgement = "🎯 정밀타격" if net_force >= 1.0 else "🤔 요행(가속부족)"
        elif profit < -2.0:
            judgement = "❌ 엔진과열(오판)" if net_force >= 1.0 else "⚠️ 억지진입(동력부족)"
        else:
            judgement = "➖ 보합(마찰 상쇄)"

        # 5. CSV 저장용 데이터 구성 (pandas to_dict()가 이미 7개 컬럼을 모두 가져옵니다)
        row_dict = dict(row)
        row_dict['primary_driver'] = primary_driver
        row_dict['judgement'] = judgement
        analysis_results.append(row_dict)

    if analysis_results:
        result_df = pd.DataFrame(analysis_results)
        filename = f"physics_trade_analysis_{datetime.now().strftime('%Y%m%d')}.csv"
        result_df.to_csv(filename, index=False, encoding='utf-8-sig')

    return filename

if __name__ == "__main__":
    analyze_trade_efficiency()