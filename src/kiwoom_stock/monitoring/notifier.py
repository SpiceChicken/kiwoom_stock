import logging
import requests
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Any

from kiwoom_stock.monitoring.manager import Position

# [1] 일반 운영/에러 로그용
logger = logging.getLogger(__name__)

# [2] 상태 테이블 전용
status_logger = logging.getLogger("status")

class Notifier:
    def __init__(self, stock_names: Dict[str, str], config: Dict):
        self.stock_names = stock_names
        self.webhook_url = config.get("webhook_url")

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

    def notify_momentum(self, res: Dict):
        """수급 폭발 알림"""
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        msg = f"🚀 [수급 폭발] {name}({res['stock_code']}) 점수 급상승! ({res['momentum']:+})"
        
        print(msg)
        logger.info(msg)
        self._send_slack(msg)

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
            # CSV 포맷: 스냅샷시간,레짐,종목명,점수,모멘텀,상태
            log_line = (f"{snapshot_time},{regime},{item['name']},"
                        f"{item['alpha_score']},{item['supply_score']},{item['vwap_score']},{item['trend_score']},"
                        f"{item['score']:.1f},{item['momentum']:.1f},{item['reason']}")
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
                    {"type": "mrkdwn", "text": f"*점수:*\n{buy_data['buy_score']:.1f}점"},
                    {"type": "mrkdwn", "text": f"*레짐:*\n{buy_data['buy_regime']}"},
                    {"type": "mrkdwn", "text": f"*시간:*\n{datetime.now().strftime('%H:%M:%S')}"}
                ]
            },
            {"type": "divider"}
        ]
        self._send_slack_blocks(blocks)

        log_line = f"BUY_SIGNAL:{buy_data['stock_name']},buy_score:{buy_data['buy_score']},Price:{buy_data['buy_price']}"
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