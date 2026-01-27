import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional
from dataclasses import dataclass

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

@dataclass
class Position:
    id: int
    stock_code: str
    stock_name: str
    buy_price: float
    buy_score: float
    alpha_score: float
    supply_score: float
    vwap_score: float
    trend_score: float
    buy_time: str
    buy_regime: str
    status: str = 'OPEN'
    # [추가] DB에서 읽어올 때 포함될 수 있는 필드들 (기본값 None)
    sell_price: Optional[float] = None
    profit_rate: Optional[float] = None
    sell_time: Optional[str] = None
    sell_reason: Optional[str] = None
    current_score: Optional[float] = None
    
    @property
    def calc_profit_rate(self) -> float:
        """
        매수가 대비 수익률을 계산합니다.
        """
        # 0으로 나누기 방지 및 가격 미지정 시 0.0 반환
        if not self.buy_price or not self.sell_price:
            return 0.0
            
        # sell_price가 0이면(아직 매도 전) 현재가를 대신 넣거나 0.0을 반환하도록 설계 가능
        return round((self.sell_price / self.buy_price - 1) * 100, 2)

class StockManager:
    """[Helper] 종목 및 인벤토리 관리자: 감시 종목 및 보유 종목 상태 관리"""
    def __init__(self, client, db, strategy, filter_config: Dict):
        self.client = client
        self.db = db
        self.strategy = strategy
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 50)
        
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}

        raw_positions = self.db.load_open_positions()
        # [개선] Position 객체로 관리
        self.active_positions: Dict[str, Position] = {
            code: Position(**data) for code, data in raw_positions.items()
        }

    def update_target_stocks(self):
        """보유 종목을 최우선으로 포함하여 감시 리스트를 갱신합니다."""
        try:
            new_stocks = list(self.active_positions.keys())
            seen_codes = set(new_stocks) # [최적화] 중복 체크용 Set
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            
            for item in upper_list:
                if len(new_stocks) >= self.max_stocks: break
                code, name = item['stk_cd'], item['stk_nm']
                if any(kw in name for kw in self.etf_keywords): continue
                if code not in seen_codes:
                    new_stocks.append(code)
                    seen_codes.add(code)
                self.stock_names[code] = name
            
            self.stocks = new_stocks[:self.max_stocks]
            logger.info(f"감시 종목 갱신 (총 {len(self.stocks)}개 | 보유: {len(self.active_positions)}개)")
        except Exception as e:
            logger.error(f"종목 갱신 실패: {e}")

    def monitor_active_signals(self, stock_code, log: Dict, strong_threshold, notifier):
        """보유 종목의 매도 조건을 감시하고 DB에 기록합니다."""
        if stock_code not in self.active_positions:
            return

        pos = self.active_positions[stock_code]
        pos.sell_price = log['price']
        pos.current_score = log['score']
        
        # [추상화 호출] 판정은 평가기에게 맡깁니다.
        pos.sell_reason = self.strategy.get_exit_reason(pos, strong_threshold)
        
        if pos.sell_reason:
            self._execute_sell(pos, notifier)

    def _execute_sell(self, pos: Position, notifier):
        """매도 프로세스 집중화"""
        pos.sell_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        self.db.record_sell(pos)
        notifier.notify_sell(pos)
        del self.active_positions[pos.stock_code]