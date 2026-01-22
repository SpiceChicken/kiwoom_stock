import sqlite3
import pandas as pd
import os
from datetime import datetime
from rich.console import Console
from rich.table import Table

def analyze_trade_efficiency(db_path="trades.db", export_csv=True):
    # 1. 데이터 로드
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

    # 2. 분석 결과 리스트 생성 (CSV 및 표 출력용)
    analysis_results = []
    
    table = Table(title="[bold white]개별 종목 로직 효용성 정밀 진단[/]", show_lines=True)
    table.add_column("종목 (레짐)", style="cyan")
    table.add_column("수익률", justify="right")
    table.add_column("주요 지표 점수", justify="center")
    table.add_column("로직 판정", justify="left")

    for _, row in df.iterrows():
        # 지표별 점수 추출
        scores = {
            "Alpha": row['alpha_score'],
            "Supply": row['supply_score'],
            "VWAP": row['vwap_score'],
            "Trend": row['trend_score']
        }
        primary_driver = max(scores, key=scores.get)
        profit = row['profit_rate']
        
        # 판정 로직
        if profit > 2.0:
            judgement = "🎯 적중" if scores[primary_driver] >= 80 else "🤔 요행"
        elif profit < -2.0:
            judgement = "❌ 오판" if scores[primary_driver] >= 80 else "⚠️ 경고"
        else:
            judgement = "➖ 보합"

        # 표 출력용 데이터 추가
        res_color = "red" if profit > 0 else "blue"
        score_summary = f"A:{scores['Alpha']:.0f} S:{scores['Supply']:.0f} V:{scores['VWAP']:.0f} T:{scores['Trend']:.0f}"
        table.add_row(
            f"{row['stock_name']}\n({row['buy_regime']})",
            f"[{res_color}]{profit:+.2f}%[/{res_color}]",
            score_summary,
            judgement
        )

        # CSV 저장용 데이터 구성
        row_dict = row.to_dict()
        row_dict['primary_driver'] = primary_driver
        row_dict['judgement'] = judgement
        analysis_results.append(row_dict)

    console.print(table)

    # 3. CSV 파일 저장
    if export_csv:
        result_df = pd.DataFrame(analysis_results)
        # 파일명에 날짜 포함
        filename = f"trade_analysis_{datetime.now().strftime('%Y%m%d')}.csv"
        result_df.to_csv(filename, index=False, encoding='utf-8-sig')
        console.print(f"\n[bold green]✅ 분석 결과가 CSV로 저장되었습니다: {filename}[/]")

if __name__ == "__main__":
    analyze_trade_efficiency()