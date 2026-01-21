import sqlite3
import pandas as pd
import os
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

#
def generate_rich_report(db_path="trades.db"):
    if not os.path.exists(db_path):
        print(f"❌ 에러: {db_path} 파일을 찾을 수 없습니다.")
        return

    # 1. 데이터 로드 및 전처리
    conn = sqlite3.connect(db_path)
    df = pd.read_sql_query("SELECT * FROM trades", conn)
    conn.close()

    if df.empty:
        print("📭 기록된 거래 데이터가 없습니다.")
        return

    df['buy_date'] = pd.to_datetime(df['buy_time']).dt.date
    df['sell_date'] = pd.to_datetime(df['sell_time']).dt.date
    
    console = Console()
    
    # 상단 요약 패널
    total_trades = len(df[df['status'] == 'CLOSED'])
    avg_total_profit = df[df['status'] == 'CLOSED']['profit_rate'].mean()
    summary_text = Text.assemble(
        ("전체 종료 거래: ", "white"), (f"{total_trades}건", "cyan"), (" | "),
        ("누적 평균 수익률: ", "white"), (f"{avg_total_profit:+.2f}%", "red" if avg_total_profit > 0 else "blue")
    )
    console.print(Panel(summary_text, title="[bold white]주식 모니터링 시스템 사후 검증 리포트[/]", border_style="green"))

    # 2. 일별 메인 테이블 생성
    table = Table(show_header=True, header_style="bold magenta", show_lines=True, expand=True)
    table.add_column("날짜", justify="center", style="dim", width=12)
    table.add_column("매수", justify="center", width=6)
    table.add_column("매도", justify="center", width=6)
    table.add_column("평균 수익률", justify="right", width=12)
    table.add_column("승률", justify="right", width=8)
    table.add_column("상세 매도 내역 (수익률)", justify="left")

    all_dates = pd.concat([df['buy_date'], df['sell_date']]).dropna().unique()
    all_dates.sort()

    for date in reversed(all_dates):
        # 데이터 필터링
        bought_count = len(df[df['buy_date'] == date])
        sold_today = df[(df['sell_date'] == date) & (df['status'] == 'CLOSED')]
        
        # 수익률 색상 정의
        avg_profit = 0.0
        win_rate = 0.0
        profit_str = "-"
        win_rate_str = "-"
        details = Text()

        if not sold_today.empty:
            avg_profit = sold_today['profit_rate'].mean()
            win_rate = (sold_today['profit_rate'] > 0).sum() / len(sold_today) * 100
            
            p_color = "bold red" if avg_profit > 0 else "bold blue"
            profit_str = f"[{p_color}]{avg_profit:+.2f}%[/]"
            win_rate_str = f"{win_rate:.1f}%"

            # 종목별 상세 내역 (가독성을 위해 3개마다 줄바꿈)
            for i, (_, row) in enumerate(sold_today.iterrows()):
                name = row['stock_name']
                profit = row['profit_rate']
                color = "red" if profit > 0 else "blue"
                details.append(f"{name}", style="white")
                details.append(f"({profit:+.1f}%)", style=color)
                if (i + 1) % 3 == 0: details.append("\n")
                else: details.append("  |  ")
        
        table.add_row(
            str(date),
            str(bought_count),
            str(len(sold_today)),
            profit_str,
            win_rate_str,
            details
        )

    console.print(table)

    # 3. 레짐별 성과 분석 테이블
    regime_table = Table(title="\n[bold yellow]시장 레짐별 성과 분석[/]", show_header=True, header_style="bold yellow")
    regime_table.add_column("레짐(Regime)", style="cyan")
    regime_table.add_column("거래 수", justify="center")
    regime_table.add_column("평균 수익률", justify="right")

    regime_stats = df[df['status'] == 'CLOSED'].groupby('buy_regime')['profit_rate'].agg(['count', 'mean']).reset_index()
    for _, row in regime_stats.iterrows():
        p_color = "red" if row['mean'] > 0 else "blue"
        regime_table.add_row(
            row['buy_regime'],
            str(row['count']),
            f"[{p_color}]{row['mean']:+.2f}%[/{p_color}]"
        )
    console.print(regime_table)

if __name__ == "__main__":
    generate_rich_report()