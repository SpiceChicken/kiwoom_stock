import logging
from typing import Dict, List, Optional, Any
from datetime import datetime
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
    buy_time: str
    buy_regime: str
    status: str = 'OPEN'
    # [추가] DB에서 읽어올 때 포함될 수 있는 필드들 (기본값 None)
    sell_price: Optional[float] = None
    profit_rate: Optional[float] = None
    sell_time: Optional[str] = None
    sell_reason: Optional[str] = None
    current_score: Optional[float] = None
    # DB에는 없지만, 프로그램 실행 중(Runtime)에만 사용하는 메모리 변수
    atr_percent: Optional[float] = None
    
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
    """
    [Helper] 종목 및 인벤토리 관리자: 감시 종목 및 보유 종목 상태 관리
    
    """
    def __init__(self, client, db, strategy, filter_config: Dict):
        self.client = client
        self.db = db
        self.strategy = strategy
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 50)
        self.cooldown_minutes = filter_config.get("cooldown_minutes", 10)
        
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}

        raw_positions = self.db.load_open_positions()
        # [개선] Position 객체로 관리
        self.active_positions: Dict[str, Position] = {
            code: Position(**data) for code, data in raw_positions.items()
        }

    def update_target_stocks(self):
        """
        [Manager] 보유 종목을 최우선으로 포함하여 감시 리스트를 갱신합니다.
        
        """
        try:
            new_stocks = []
            seen_codes = set() # 중복 체크용
            
            # 1. 실시간 거래대금 상위 종목 먼저 추가
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            for item in upper_list:
                if len(new_stocks) >= self.max_stocks: break
                code, name = item['stk_cd'], item['stk_nm']
                
                # ETF 제외 필터
                if any(kw in name for kw in self.etf_keywords): continue
                
                if code not in seen_codes:
                    new_stocks.append(code)
                    seen_codes.add(code)
                    self.stock_names[code] = name

            # 2. [핵심] 보유 종목을 리스트 끝에 추가 (단, max_stocks 여유가 있을 때)
            # 보유 종목은 반드시 감시해야 하므로, 슬라이싱 전에 추가하는 것이 안전합니다.
            for code, pos in self.active_positions.items():
                if code not in seen_codes:
                    new_stocks.append(code)
                    seen_codes.add(code)
                    self.stock_names[code] = pos.stock_name

            # 3. 최종 감시 종목 설정 (보유 종목을 포함한 리스트)
            # 주의: 여기서 [:self.max_stocks]로 자르면 뒤에 붙인 보유 종목이 잘릴 수 있습니다.
            self.stocks = new_stocks
            logger.info(f"감시 종목 갱신 (총 {len(self.stocks)}개 | 보유: {len(self.active_positions)}개 포함)")
        except Exception as e:
            logger.error(f"종목 갱신 실패: {e}")

    def evaluate_position(self, verdict: Dict, strong_threshold):
        """
        [Manager] 보유 종목의 상태를 최신화하고 매도 사유(Exit Reason)가 있는지 평가합니다.
        
        """
        stock_code = verdict['stock_code']

        if stock_code not in self.active_positions:
            return None

        pos = self.active_positions[stock_code]

        pos.sell_price = verdict['price']
        pos.current_score = verdict['score']

        # [New] ATR 정보를 Position 객체에 임시 저장 (메모리 전용)
        # DB 스키마에 없어도 객체 속성으로는 동적 할당 가능
        # verdict에 'atr_percent'가 없으면 기본값 1.5 사용
        pos.atr_percent = verdict['atr_percent']
        
        # [추상화 호출] 판정은 평가기에게 맡깁니다.
        sell_reason = self.strategy.get_exit_reason(pos, strong_threshold)

        return sell_reason

    def get_total_pnl_status(self, realized_pnl: float) -> float:
        """
        [Manager] 실현 손익과 미실현 손익을 합산하여 현재 총 손익률을 반환합니다.
        
        """
        # 미실현 손익 합산 (pos.calc_profit_rate 활용)
        unrealized_pnl = sum(pos.calc_profit_rate for pos in self.active_positions.values())
        return realized_pnl + unrealized_pnl

    def process_buy_order(self, verdict: Dict) -> tuple[bool, Optional[Dict]]:
        """
        [Manager] 실제 매수 주문 집행 및 데이터 처리 전담

        """
        stock_code = verdict['stock_code']
        
        try:
            # 1. 실제 키움증권/API 주문 전송 (TBD)

            # 2. 최종 buy_data 구성
            buy_data = {
                "stock_code": stock_code,
                "stock_name": self.stock_names[stock_code],
                "buy_price": verdict.get('price'),
                "buy_score": verdict.get('score'),
                "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "buy_regime": verdict.get('regime')
            }
            
            # 3. DB 기록 및 내부 포지션 업데이트
            buy_data['id'] = self.db.record_buy(buy_data)
            self.active_positions[stock_code] = Position(**buy_data)
            
            return True, buy_data

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            return False, None

    def process_sell_order(self, verdict: Dict, reason: str) -> tuple[bool, Optional[Position]]:
        """
        [Manager] 실제 매도 주문 집행 및 데이터 처리 전담
        """
        stock_code = verdict['stock_code']
        
        try:
            # 1. 실제 키움증권/API 주문 전송 (TBD)

            # 2. DB 기록용 데이터 생성
            pos = self.active_positions[stock_code]
            pos.sell_price = verdict['price']
            pos.current_score = verdict['score']
            pos.sell_reason = reason

            # 3. DB 기록 및 내부 포지션 업데이트
            self.db.record_sell(pos)
            del self.active_positions[pos.stock_code]
            
            return True, pos

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            return False, None

    def is_not_recent_exit(self, stock_code: str) -> bool:
        """
        [Manager] DB의 sell_time을 조회하여 냉각기 경과 여부를 판정합니다.
        프로그램이 재시작되어도 DB 기록을 바탕으로 정확히 판정합니다.
        
        """
        # DB에서 직접 마지막 매도 시간 조회
        last_sell_time = self.db.get_last_sell_time(stock_code)
        
        if not last_sell_time:
            return True # 매도 기록이 없으면 진입 가능
            
        # 현재 시간과의 차이 계산
        elapsed_seconds = (datetime.now() - last_sell_time).total_seconds()
        cooldown_seconds = self.cooldown_minutes * 60
        
        if elapsed_seconds < cooldown_seconds:
            remaining_min = int((cooldown_seconds - elapsed_seconds) // 60)
            logger.info(f"[{stock_code}] DB 기록 기준 냉각기 진행 중 ({remaining_min}분 남음)")
            return False
            
        return True