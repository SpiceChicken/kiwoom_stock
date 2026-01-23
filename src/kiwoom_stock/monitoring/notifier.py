import logging
from typing import Dict

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class Notifier:
    def __init__(self, stock_names: Dict[str, str]):
        self.stock_names = stock_names

    def notify_buy(self, buy_data: Dict):
        """매수 실행 정보 출력 및 로깅"""
        name = buy_data['stock_name']
        code = buy_data['stock_code']
        score = buy_data['buy_score']
        price = buy_data['buy_price']
        
        msg = f"🔥 [매수 실행] {name}({code}) | 점수: {score} | 가격: {price:,.0f}원"
        
        # 1. 콘솔 출력
        print(f"\n{msg}")
        # 2. trading.log 적재 (JSON 필터 적용됨)
        logger.info(msg)

    def notify_sell(self, stock_name: str, profit: float, reason: str):
        """매도 실행 정보 출력 및 로깅"""
        msg = f"📉 [매도 실행] {stock_name} | 수익률: {profit:+.2f}% | 사유: {reason}"
        
        print(msg)
        logger.info(msg)

    def notify_momentum(self, res: Dict):
        """수급 폭발 알림"""
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        msg = f"🚀 [수급 폭발] {name}({res['stock_code']}) 점수 급상승! ({res['momentum']:+})"
        
        print(msg)
        logger.info(msg)
    
    def print_status_table_header(self, regime_value: str):
        """화면 출력용 헤더 관리"""
        from datetime import datetime
        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {regime_value}")
        print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
        print("-" * 55)

    def notify_status(self, name: str, score: float, momentum: float, status: str):
        """실시간 종목 상태를 한 줄로 출력합니다."""
        m_str = f"+{momentum}" if momentum > 0 else f"{momentum}"
        # 통일된 포맷으로 출력
        print(f"{name:<10} | {score:>5.1f} | {m_str:>6} | {status:<10}")