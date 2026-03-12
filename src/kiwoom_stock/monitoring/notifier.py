import logging
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Any

from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.utils.gemini_client import GeminiClient
from kiwoom_stock.core.prompts import SystemPrompts

# [1] 일반 운영/에러 로그용
logger = logging.getLogger(__name__)

# [2] 상태 테이블 전용
status_logger = logging.getLogger("status")

class Notifier:
    def __init__(self, stock_names: Dict[str, str], config: Dict):
        self.stock_names = stock_names
        self.webhook_url = config.get("webhook_url")

        gemini_api_key = config.get("gemini_api_key")
        self.ai_client = GeminiClient(api_key=gemini_api_key, model="gemini-2.5-flash")

        # 50개 종목 데이터를 임시 저장할 버퍼
        self.status_data: List[Dict[str, Any]] = []

    def _send_slack(self, text: str):
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
    def _send_slack_blocks(self, blocks: List[Dict[str, Any]]):
        """Slack Block Kit 메시지 전송 헬퍼"""
        if not self.webhook_url:
            return
        try:
            response = requests.post(self.webhook_url, json={"blocks": blocks}, timeout=5)
            response.raise_for_status()
        except Exception as e:
            logger.error(f"Slack Block Kit 전송 실패: {e}")

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
        self._send_slack_blocks(blocks)

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
                    {"type": "mrkdwn", "text": f"*수익률:*\n{profit:+.2f}%"},
                    {"type": "mrkdwn", "text": f"*매도 사유:*\n{pos.sell_reason}"},
                    {"type": "mrkdwn", "text": f"*시간:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            },
            {"type": "divider"}
        ]
        self._send_slack_blocks(blocks)

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
        self._send_slack_blocks(blocks)
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
        self._send_slack_blocks(blocks)
        logger.error(f"CRITICAL_ERROR: {message}")

    def send_daily_post_mortem(self, stats: Dict[str, Any], csv_path: Optional[str] = None):
        """[V3.2] CSV 파일을 인자로 직접 전달받아 AI에게 멀티모달로 분석 요청"""
        if not self.webhook_url: return
        ai_comment = "AI 분석 환경이 준비되지 않았습니다."
        
        if self.ai_client.check_availability():
            prompt = SystemPrompts.build_daily_post_mortem(stats)
            
            # 💡 파라미터로 받은 csv_path를 제미나이 멀티모달 메서드에 바로 꽂아넣음!
            result = self.ai_client.generate_content(prompt, file_path=csv_path)
            
            if result.get('success'):
                ai_comment = result.get('output')
            else:
                logger.error(f"AI 분석 중 오류 발생: {result.get('error')}")
                ai_comment = f"AI 분석 중 오류 발생: {result.get('error')}"

        # 4. Slack Block Kit 조립 (이전과 동일)
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text", 
                    "text": "📈 일일 마감 부검 리포트"
                }
            },
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn", 
                        "text": f"*📅 날짜:*\n{stats.get('date', 'N/A')}"
                    },
                    {
                        "type": "mrkdwn", 
                        "text": f"*✅ 승률:*\n{stats.get('win_rate', 'N/A')}"
                    },
                    {
                        "type": "mrkdwn", 
                        "text": f"*💰 총 수익률:*\n*{stats.get('total_pnl', '0.00%')}*"
                    },
                    {
                        "type": "mrkdwn", 
                        "text": f"*🛡️ 쉴드 방어 (수급 락 등):*\n{stats.get('defense_count', 0)}건 차단"
                    }
                ]
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn", 
                    "text": f"*📝 아키텍트 AI 총평*\n{ai_comment}"
                }
            }
        ]
        
        self._send_slack_blocks(blocks)
        logger.info("일일 마감 부검 리포트 Slack 전송 완료.")