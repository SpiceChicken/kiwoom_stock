import sqlite3
import pandas as pd
from rich.console import Console
from rich.table import Table

def analyze_trade_efficiency(db_path="trades.db"):
    conn = sqlite3.connect(db_path)
    # 로직에 의해 기록된 DB 로드
    df = pd.read_sql_query("SELECT * FROM trades WHERE status = 'CLOSED'", conn)
    conn.close()

    console = Console()
    table = Table(title="[bold white]개별 종목 로직 효용성 진단[/]", show_lines=True)

    table.add_column("종목 (레짐)", style="cyan")
    table.add_column("진입 점수", justify="center")
    table.add_column("수익률", justify="right")
    table.add_column("로직 효용성 판정", justify="left")

    for _, row in df.iterrows():
        # 1. 진입 품질 분석
        # 진입 점수가 높았음에도 손실이 났다면 지표간 '데드웨이트' 발생 가능성
        entry_quality = "✅ 적정 타점" if row['buy_score'] >= 80 else "⚠️ 낮은 신뢰도"
        
        # 2. 결과 분석
        profit = row['profit_rate']
        result_color = "red" if profit > 0 else "blue"
        
        # 3. 효용성 코멘트 (로직 복기)
        if profit > 2.0 and row['buy_score'] >= 80:
            comment = "🎯 로직 적중: 고득점 종목의 시세 분출"
        elif profit < -3.0 and row['buy_score'] >= 80:
            comment = "❌ 로직 실패: 고득점 후 급락 (손절 로직 점검 필요)"
        elif profit > 0 and row['buy_score'] < 70:
            comment = "🤔 요행: 낮은 점수에서 우연한 상승"
        else:
            comment = "➖ 통계적 범위 내 움직임"

        table.add_row(
            f"{row['stock_name']}\n({row['buy_regime']})",
            f"{row['buy_score']:.1f}",
            f"[{result_color}]{profit:+.2f}%[/{result_color}]",
            f"{entry_quality}\n{comment}"
        )

    console.print(table)

if __name__ == "__main__":
    analyze_trade_efficiency()