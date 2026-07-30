import os
import logging
import requests
import pandas as pd
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, cast

# Slack SDK Import 추가
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from kiwoom_stock.application.reporting import (
    DailyReportRequest,
    DailyReportStats,
    ReportArtifact,
)
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.utils.gemini_client import GeminiClient

# [1] 일반 운영/에러 로그용
logger = logging.getLogger(__name__)

# [2] 상태 테이블 전용
status_logger = logging.getLogger("status")


def _build_daily_summary_blocks(
    *,
    report_date: str,
    win_rate: str,
    total_pnl: float | str,
    defense_count: int,
    narrative: str,
) -> List[Dict[str, Any]]:
    """Build the byte-compatible summary blocks for both public paths."""
    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📈 일일 마감 부검 리포트",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*📅 날짜:*\n{report_date}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*✅ 승률:*\n{win_rate}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*💰 총 수익률:*\n*{total_pnl}*",
                },
                {
                    "type": "mrkdwn",
                    "text": (
                        "*🛡️ 쉴드 방어 (수급 락 등):*\n"
                        f"{defense_count}건 차단"
                    ),
                },
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📝 아키텍트 AI 총평*\n{narrative}",
            },
        },
    ]


class SlackUploader:
    def __init__(self, token: str, channel_id: str):
        self.client = WebClient(token=token)
        self.channel_id = channel_id

    def upload_csv(self, file_path: str, comment: str = "") -> bool:
        if not os.path.exists(file_path):
            logger.error(f"[Slack Upload] FileNotFound: {file_path}")
            return False

        file_name = os.path.basename(file_path)
        try:
            logger.info(f"[Slack Upload] {file_name} 업로드 시작...")
            # 대용량 CSV를 위한 v2 업로드 메서드 사용
            response = self.client.files_upload_v2(
                channel=self.channel_id,
                initial_comment=comment,
                file=file_path,
                title=file_name,
            )
            return response.get("ok", False)
        except SlackApiError as e:
            logger.error(f"[Slack Upload] API Error: {e.response['error']}")
            return False
    
    def upload_multiple_files(self, file_paths: List[str], comment: str = "") -> bool:
        """여러 개의 CSV 파일을 하나의 슬랙 메시지로 묶어서(최대 10개씩) 일괄 업로드합니다."""
        if not file_paths:
            return False

        # 1. 존재하는 파일만 필터링하고 슬랙 API가 요구하는 딕셔너리 형태로 변환
        valid_uploads = []
        for path in file_paths:
            if os.path.exists(path):
                valid_uploads.append({
                    "file": path,
                    "title": os.path.basename(path)
                })
        
        if not valid_uploads:
            logger.error("[Slack Upload] 유효한 업로드 대상 파일이 없습니다.")
            return False

        # 2. Slack API 한계(메시지 당 최대 10개 파일) 방어 로직 (Chunking)
        chunk_size = 10
        success = True
        
        for i in range(0, len(valid_uploads), chunk_size):
            chunk = valid_uploads[i:i + chunk_size]
            try:
                logger.info(f"[Slack Upload] {len(chunk)}개의 파일 묶음 업로드 중... ({i+1}~{min(i+chunk_size, len(valid_uploads))})")
                
                # 첫 묶음에만 코멘트를 달고, 나머지는 (계속...) 표시
                msg = comment if i == 0 else f"{comment} (이어서 계속...)"
                
                response = self.client.files_upload_v2(
                    channel=self.channel_id,
                    initial_comment=msg,
                    file_uploads=chunk  # 💡 다중 파일 업로드 핵심 파라미터
                )
                if not response.get("ok", False):
                    success = False
                    
            except SlackApiError as e:
                logger.error(f"[Slack Upload] 다중 업로드 API Error: {e.response['error']}")
                success = False
                
        return success

class Notifier:
    def __init__(
        self,
        stock_names: Dict[str, str],
        config: Dict[str, Any],
        *,
        uploader_factory: Optional[Callable[..., SlackUploader]] = None,
    ):
        self.stock_names = stock_names
        self.webhook_url = config.get("webhook_url")
        self.slack_token = config.get("slack_token")
        self.slack_channel = config.get("slack_channel")
        self._uploader_factory = uploader_factory or SlackUploader

        gemini_api_key = config.get("gemini_api_key")
        self.ai_client = GeminiClient(api_key=gemini_api_key, model="gemini-2.5-flash")

        # 50개 종목 데이터를 임시 저장할 버퍼
        self.status_data: List[Dict[str, Any]] = []

    def send_slack(self, text: str):
        """Slack Webhook을 통해 메시지를 전송합니다."""
        if not self.webhook_url:
            return
            
        try:
            payload = {"text": text}
            response = requests.post(self.webhook_url, json=payload, timeout=5)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Slack 전송 실패: {e}")

    # [수정] 인자 타입을 List[Dict[str, Any]]로 명확히 정의
    def send_slack_blocks(self, blocks: List[Dict[str, Any]]):
        """Slack Block Kit 메시지 전송 헬퍼"""
        if not self.webhook_url:
            return
        try:
            self._post_slack_blocks(blocks)
        except Exception as e:
            logger.error(f"Slack Block Kit 전송 실패: {e}")

    def _post_slack_blocks(self, blocks: List[Dict[str, Any]]) -> None:
        webhook_url = self.webhook_url
        if not isinstance(webhook_url, str) or not webhook_url:
            raise RuntimeError("Slack webhook is not configured")
        response = requests.post(
            webhook_url,
            json={"blocks": blocks},
            timeout=5,
        )
        response.raise_for_status()

    def start_status_session(self):
        """루프 시작 시 데이터 저장소 초기화"""
        self.status_data = []

    def collect_status(self, data: Dict[str, Any]):
        """딕셔너리 형태로 데이터를 리스트에 추가"""
        self.status_data.append(data)

    def flush_status(self, regime: str):
        """
        [분석 최적화] 50개 종목을 CSV 행 형태로 status.log에 적재합니다.
        """
        if not self.status_data:
            return

        snapshot_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        for item in self.status_data:
            forces = item.get('forces', {})
            
            name = item.get('name', '')
            price = item.get('price', 0)
            reason = item.get('reason', '')
            # CSV 포맷: 스냅샷시간,레짐,종목명,점수,모멘텀,상태
            log_line = (f"{snapshot_time},{regime},{name},{price},"
                        f"{forces.get('thrust', 0.0)},{forces.get('gravity', 0.0)},{forces.get('drag', 0.0)},"
                        f"{forces.get('magnetic', 0.0)},{forces.get('jerk', 0.0)},{forces.get('impulse', 0.0)},{forces.get('net_force', 0.0)},"
                        f"{reason}")
            status_logger.info(log_line)

    def notify_buy(self, buy_data: Dict):
        """매수 알림: 시각적 대시보드 형태 (Block Kit)"""
        # [수정] 타입 힌트에 콜론(:) 사용 및 타입 명시
        blocks: List[Dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🔵 매수 신호 발생 ({buy_data['stock_name']})"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*매수가:*\n{buy_data['buy_price']:,.0f}원"},
                    {"type": "mrkdwn", "text": f"*레짐:*\n{buy_data['buy_regime']}"},
                    {"type": "mrkdwn", "text": f"*시간:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            },
            {"type": "divider"}
        ]
        self.send_slack_blocks(blocks)

        log_line = f"BUY_SIGNAL:{buy_data['stock_name']},Price:{buy_data['buy_price']}"
        logger.info(log_line)


    def notify_sell(self, pos: Position):
        """매도 알림: 수익/손실에 따른 컬러감 및 요약 (Block Kit)"""
        profit = pos.calc_profit_rate

        emoji = "🔥" if profit > 0 else "📉"
        status_text = "수익 실현" if profit > 0 else "손절 실행"
        
        # [수정] 타입 힌트에 콜론(:) 사용 및 타입 명시
        blocks: List[Dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {status_text} ({pos.stock_name})"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*매도가:*\n{pos.sell_price:,.0f}원 ({profit:+.2f}%)"},
                    {"type": "mrkdwn", "text": f"*매도 사유:*\n{pos.sell_reason}"},
                    {"type": "mrkdwn", "text": f"*시간:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            },
            {"type": "divider"}
        ]
        self.send_slack_blocks(blocks)

        log_line = f"SELL_SIGNAL:{pos.stock_name},Profit:{profit:+.2f}%,Reason:{pos.sell_reason}"
        logger.info(log_line)

    def notify_error(self, message: str):
        """메인 루프 내 일반 에러 알림"""
        # [수정] 타입 힌트에 콜론(:) 사용 및 타입 명시
        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn", 
                    "text": f"⚠️ *[RUNTIME ERROR]*\n*발생 시간:* {datetime.now().strftime('%H:%M:%S')}\n*내용:* {message}"
                }
            }
        ]
        self.send_slack_blocks(blocks)
        logger.error(f"SLACK_ERROR_NOTIFIED: {message}")

    def notify_critical(self, message: str):
        """시스템 장애(킬스위치): 강력한 경고 디자인"""
        # [수정] 타입 힌트에 콜론(:) 사용 및 타입 명시
        blocks: List[Dict[str, Any]] = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🚨 *[SYSTEM STOP]*\n*사유:* {message}"}
            }
        ]
        self.send_slack_blocks(blocks)
        logger.error(f"CRITICAL_ERROR: {message}")

    def send_daily_post_mortem(self, stats: Dict[str, Any], csv_path: Optional[str] = None):
        """외부 프롬프트를 활용한 멀티모달 분석 요청"""
        if not self.webhook_url: return
        ai_comment = "AI 분석 환경이 준비되지 않았습니다."
        
        if self.ai_client.check_availability():
            # 💡 [V2.6] 팩토리 대신 클라이언트의 통합 메서드 호출
            result = self.ai_client.generate_daily_report(stats=stats, csv_path=csv_path)
            
            if result.get('success'):
                ai_comment = cast(str, result.get('output'))
            else:
                logger.error(f"AI 분석 중 오류 발생: {result.get('error')}")
                ai_comment = f"AI 분석 중 오류 발생: {result.get('error')}"

        blocks = _build_daily_summary_blocks(
            report_date=stats.get('date', 'N/A'),
            win_rate=stats.get('win_rate', 'N/A'),
            total_pnl=stats.get('total_pnl', '0.00%'),
            defense_count=stats.get('defense_count', 0),
            narrative=ai_comment,
        )
        
        self.send_slack_blocks(blocks)
        logger.info("일일 마감 부검 리포트 Slack 전송 완료.")

    def summary_enabled(self) -> bool:
        """Return whether the typed summary path has a webhook."""
        return bool(self.webhook_url)

    def publish_summary(
        self,
        *,
        request: DailyReportRequest,
        stats: DailyReportStats,
        narrative: str,
        trade_artifact: Optional[ReportArtifact],
    ) -> bool:
        """Publish typed daily summary blocks with safe failure detail."""
        if not self.webhook_url:
            return False

        blocks = _build_daily_summary_blocks(
            report_date=request.report_date,
            win_rate=stats.win_rate,
            total_pnl=stats.total_pnl,
            defense_count=stats.defense_count,
            narrative=narrative,
        )
        try:
            self._post_slack_blocks(blocks)
        except Exception as error:
            logger.error(
                "Slack summary publication failed (%s)",
                type(error).__name__,
            )
            raise RuntimeError("report summary publication failed") from None
        logger.info("일일 마감 부검 리포트 Slack 전송 완료.")
        return True

    def publish_telemetry(
        self,
        *,
        request: DailyReportRequest,
        trade_artifact: Optional[ReportArtifact],
        minute_artifacts: Sequence[ReportArtifact],
    ) -> bool:
        """Upload typed report artifact references using legacy chunking."""
        if not self.slack_token or not self.slack_channel:
            return False

        minute_paths = [artifact.reference for artifact in minute_artifacts]
        trade_path = (
            trade_artifact.reference
            if trade_artifact is not None
            else None
        )
        if trade_path is None and not minute_paths:
            return False

        report_day = request.report_date.replace("-", "")
        try:
            uploader = self._uploader_factory(
                token=self.slack_token,
                channel_id=self.slack_channel,
            )
            upload_failed = False
            if trade_path and not uploader.upload_csv(
                trade_path,
                f"📊 *[{report_day}] V3.0 엔진 매매 분석 리포트*",
            ):
                upload_failed = True
            if minute_paths and not uploader.upload_multiple_files(
                minute_paths,
                (
                    f"📈 *[{report_day}] 1분봉 백업 데이터 일괄 업로드 "
                    f"({len(minute_paths)}개 종목)*"
                ),
            ):
                upload_failed = True
            if upload_failed:
                raise RuntimeError("one or more artifact uploads failed")
        except Exception as error:
            logger.error(
                "Slack telemetry publication failed (%s)",
                type(error).__name__,
            )
            raise RuntimeError("report telemetry publication failed") from None
        return True
