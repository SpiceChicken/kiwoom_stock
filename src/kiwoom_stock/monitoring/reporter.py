import logging
import pandas as pd
from datetime import datetime
from typing import Dict, Any

from kiwoom_stock.monitoring.notifier import Notifier

from tools.trade_validator import analyze_trade_efficiency
from tools.extract_1min_chart import extract_and_save_1min_chart

logger = logging.getLogger(__name__)

class DailyReporter:
    def __init__(self, notifier: Notifier):
        self.notifier = notifier

    def run_pipeline(self):
        logger.info("🔄 [Daily Post-Mortem] 1단계: 1분봉 데이터 추출 시작")
        try:
            # 1. extract_1min_chart 호출
            minute_chart_list = extract_and_save_1min_chart()
            
            logger.info("📊 [Daily Post-Mortem] 2단계: 거래 검증 및 통계 산출")
            
            # 💡 2. trade_validator 호출
            csv_path = analyze_trade_efficiency()
            
            logger.info("🤖 [Daily Post-Mortem] 3단계: LLM 총평 생성 및 Slack 알림")
            stats = self._load_and_parse_stats(csv_path)
            
            # 3. 산출된 결과를 그대로 Notifier에 전달
            self.notifier.send_daily_post_mortem(stats=stats, csv_path=csv_path)
            
            logger.info("✅ 일일 자동 부검 파이프라인 완료.")

        except Exception as e:
            logger.error(f"부검 파이프라인 처리 중 오류: {e}", exc_info=True)

    def _load_and_parse_stats(self, csv_path: str) -> Dict[str, Any]:
        """trade_validator가 리턴한 CSV 경로를 받아 기초 통계 산출"""
        today_str = datetime.now().strftime("%Y-%m-%d")
        stats = {
            "date": today_str,
            "win_rate": "N/A",
            "total_pnl": 0.0,
            "defense_count": 0
        }

        if not csv_path:
            return stats

        try:
            df = pd.read_csv(csv_path)
            wins = len(df[df['profit_rate'] > 0])
            total = len(df)
            
            win_rate_val = (wins / total * 100) if total > 0 else 0
            total_pnl_val = df['profit_rate'].sum() if total > 0 else 0
            
            if 'status' in df.columns:
                defense_count = len(df[df['status'].str.contains('차단', na=False)])
            else:
                defense_count = 0
            
            stats["win_rate"] = f"{win_rate_val:.1f}% ({wins}승 {total-wins}패)"
            stats["total_pnl"] = f"{total_pnl_val:+.2f}%"
            stats["defense_count"] = defense_count
            
        except FileNotFoundError:
            logger.error(f"CSV 파일을 찾을 수 없습니다: {csv_path}")
        except Exception as e:
            logger.error(f"CSV 기초 통계 파싱 실패: {e}")
            
        return stats