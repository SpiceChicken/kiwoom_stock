# [PATCH] tools/trade_validator.py (전체 덮어쓰기)

import sqlite3
import pandas as pd
import os
from datetime import datetime
from rich.console import Console
from rich.table import Table

def analyze_trade_efficiency(db_path="trades.db", export_csv=True):
    if not os.path.exists(db_path):
        print(f"❌ 에러: {db_path} 파일을 찾을 수 없습니다.")
        return

    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM trades WHERE status = 'CLOSED'", conn)
    conn.close()

    console = Console()
    if df.empty:
        console.print("[yellow]조회된 종료 거래 데이터가 없습니다.[/]")
        return

    # 구 DB 구조(S.V.T)인 경우 충돌 방지
    if 'thrust' not in df.columns:
        console.print("[red]❌ 구버전 스키마(S.V.T)가 감지되었습니다. 기존 trades.db를 삭제 후 새로 구동하십시오.[/]")
        return

    analysis_results = []
    table = Table(title="[bold white]물리 엔진 동역학 타점 정밀 진단 (7대 벡터 모두 표시)[/]", show_lines=True)
    table.add_column("종목 (레짐)", style="cyan")
    table.add_column("수익률", justify="right")
    table.add_column("7대 물리 벡터 (Forces)", justify="center")
    table.add_column("타점 판정", justify="left")

    for _, row in df.iterrows():
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

        # 4. 표 출력용 데이터 포매팅 (모든 값 표시, 가독성을 위해 줄바꿈 적용)
        res_color = "red" if profit > 0 else "blue"
        
        # T:추세 / G:중력(저항) / D:마찰력 / M:호가흡입 / J:가속도 / I:순간충격
        score_summary = (
            f"[bold white]Net Force: {net_force:+.2f}[/]\n"
            f"T:{forces['Thrust']:+.2f}  G:{forces['Gravity']:+.2f}  D:{forces['Drag']:+.2f}\n"
            f"M:{forces['Magnetic']:+.2f}  J:{forces['Jerk']:+.2f}  I:{forces['Impulse']:+.2f}"
        )
        
        table.add_row(
            f"{row['stock_name']}\n({row['buy_regime']})",
            f"[{res_color}]{profit:+.2f}%[/{res_color}]",
            score_summary,
            f"{judgement}\n(Main: {primary_driver})"
        )

        # 5. CSV 저장용 데이터 구성 (pandas to_dict()가 이미 7개 컬럼을 모두 가져옵니다)
        row_dict = row.to_dict()
        row_dict['primary_driver'] = primary_driver
        row_dict['judgement'] = judgement
        analysis_results.append(row_dict)

    console.print(table)

    if export_csv:
        result_df = pd.DataFrame(analysis_results)
        filename = f"physics_trade_analysis_{datetime.now().strftime('%Y%m%d')}.csv"
        result_df.to_csv(filename, index=False, encoding='utf-8-sig')
        console.print(f"\n[bold green]✅ 물리 엔진 7대 벡터 분석 결과가 CSV로 전체 저장되었습니다: {filename}[/]")

if __name__ == "__main__":
    analyze_trade_efficiency()