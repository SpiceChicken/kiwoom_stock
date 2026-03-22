import os
import logging
import pandas as pd
from datetime import datetime
from typing import Dict, Any

from kiwoom_stock.core import config
from kiwoom_stock.monitoring.notifier import Notifier, SlackUploader
from tools.trade_validator import analyze_trade_efficiency
from tools.extract_1min_chart import extract_and_save_1min_chart

logger = logging.getLogger(__name__)

class DailyReporter:
    def __init__(self, notifier: Notifier):
        self.notifier = notifier

    # 💡 [변경] 디렉토리 경로 대신 명시적인 'minute_chart_list'를 파라미터로 받습니다.
    def execute_slack_telemetry(self, trade_csv_path: str, minute_chart_list: list):
        """ EC2 Slack Telemetry 파이프라인 가동기"""
        system_config = getattr(config, 'CONFIG', {})
        token = system_config.get("slack_token")
        channel = system_config.get("slack_channel")
        
        if not token or not channel:
            logger.warning("⚠️ 슬랙 토큰(slack_token) 또는 채널 ID(slack_channel)가 설정되지 않아 파일 업로드를 건너뜁니다.")
            return

        today_str = datetime.now().strftime("%Y%m%d")
        uploader = SlackUploader(token=token, channel_id=channel)
        
        logger.info("🚀 [Telemetry] 일일 슬랙 텔레메트리 파이프라인 가동...")
        
        # 1. 트레이드 분석 CSV 단독 업로드
        if trade_csv_path and os.path.exists(trade_csv_path):
            uploader.upload_csv(trade_csv_path, f"📊 *[{today_str}] V3.0 엔진 매매 분석 리포트*")
        else:
            logger.warning(f"트레이드 분석 파일을 찾을 수 없어 업로드를 생략합니다: {trade_csv_path}")

        # 2. 1분봉 CSV 일괄 업로드 (💡 하나의 메시지(스레드)로 통합)
        if minute_chart_list:
            uploader.upload_multiple_files(
                file_paths=minute_chart_list, 
                comment=f"📈 *[{today_str}] 1분봉 백업 데이터 일괄 업로드 ({len(minute_chart_list)}개 종목)*"
            )
        else:
            logger.warning("업로드할 1분봉 차트 데이터(리스트)가 없습니다.")

    def run_pipeline(self):
        logger.info("🔄 [Daily Post-Mortem] 1단계: 1분봉 데이터 추출 시작")
        try:
            # 💡 [변경] 1단계에서 반환된 파일 리스트를 변수에 안전하게 담습니다.
            minute_chart_list = extract_and_save_1min_chart()
            
            logger.info("📊 [Daily Post-Mortem] 2단계: 거래 검증 및 통계 산출")
            csv_path = analyze_trade_efficiency()
            
            logger.info("🤖 [Daily Post-Mortem] 3단계: LLM 총평 생성 및 Slack 알림")
            stats = self._load_and_parse_stats(csv_path)
            self.notifier.send_daily_post_mortem(stats=stats, csv_path=csv_path)
            
            logger.info("📦 [Daily Post-Mortem] 4단계: EC2 텔레메트리 백업 (Slack Upload)")
            # 💡 [변경] 4단계 호출 시 해당 리스트를 그대로 주입합니다.
            self.execute_slack_telemetry(trade_csv_path=csv_path, minute_chart_list=minute_chart_list)
            
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