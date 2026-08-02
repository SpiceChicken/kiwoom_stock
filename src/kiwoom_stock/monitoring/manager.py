import logging
from typing import Callable, Dict, List, Optional
from datetime import datetime

from kiwoom_stock.application.ports import MarketDataGateway
from kiwoom_stock.domain.models import Position

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class StockManager:
    """
    [Helper] 종목 및 인벤토리 관리자: 감시 종목 및 보유 종목 상태 관리
    
    """
    def __init__(
        self,
        market_gateway: MarketDataGateway,
        db,
        filter_config: Dict,
        *,
        clock: Optional[Callable[[], datetime]] = None,
        paper_transition_guard: Callable[[], None] = lambda: None,
        strict_paper_errors: bool = False,
    ):
        self.market_gateway = market_gateway
        self.db = db
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 50)
        self._clock = clock
        self._paper_transition_guard = paper_transition_guard
        self._strict_paper_errors = strict_paper_errors
        
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
            upper_list = self.market_gateway.get_top_trading_value(market_tp="001")
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

    def update_position_data(self, verdict: Dict):
        """
        [Manager] 보유 종목의 상태를 최신화하고 매도 사유(Exit Reason)가 있는지 평가합니다.
        
        """
        stock_code = verdict['stock_code']

        if stock_code not in self.active_positions:
            return None

        pos = self.active_positions[stock_code]

        pos.sell_price = verdict['price']

        # [New] ATR 정보를 Position 객체에 임시 저장 (메모리 전용)
        # DB 스키마에 없어도 객체 속성으로는 동적 할당 가능
        # verdict에 'atr_percent'가 없으면 기본값 0.5 사용
        pos.atr_percent = verdict['atr_percent']
        pos.down_atr_percent = verdict['down_atr_percent']
        
        return pos

    def get_total_pnl_status(self, realized_pnl: float) -> float:
        """
        [Manager] 실현 손익과 미실현 손익을 합산하여 현재 총 손익률을 반환합니다.
        
        """
        # 미실현 손익 합산 (pos.calc_profit_rate 활용)
        unrealized_pnl = sum(pos.calc_profit_rate for pos in self.active_positions.values())
        return realized_pnl + unrealized_pnl

    def apply_paper_buy(self, verdict: Dict) -> tuple[bool, Optional[Dict]]:
        """
        [Manager] 브로커 주문 없이 paper 매수 상태만 기록합니다.

        """
        stock_code = verdict['stock_code']
        forces = verdict.get('forces', {})
        
        try:
            # 1. 최종 paper buy_data 구성
            buy_data = {
                "stock_code": stock_code,
                "stock_name": self.stock_names[stock_code],
                "buy_price": verdict.get('price'),

                # 개별 물리적 힘 매핑 (없을 경우 0.0)
                "thrust": forces.get('thrust', 0.0),
                "gravity": forces.get('gravity', 0.0),
                "drag": forces.get('drag', 0.0),
                "magnetic": forces.get('magnetic', 0.0),
                "jerk": forces.get('jerk', 0.0),
                "impulse": forces.get('impulse', 0.0),
                "net_force": forces.get('net_force', 0.0),

                "buy_time": (self._clock or datetime.now)().strftime('%Y-%m-%d %H:%M:%S'),
                "buy_regime": verdict.get('regime')
            }
            
            # 2. 격리된 paper DB 기록 및 내부 포지션 업데이트
            self._paper_transition_guard()
            buy_data['id'] = self.db.record_buy(buy_data)
            self.active_positions[stock_code] = Position(**buy_data)
            
            return True, buy_data

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            if self._strict_paper_errors:
                raise
            return False, None

    def apply_paper_sell(self, verdict: Dict, reason: str) -> tuple[bool, Optional[Position]]:
        """
        [Manager] 브로커 주문 없이 paper 매도 상태만 기록합니다.
        """
        stock_code = verdict['stock_code']
        
        try:
            # 1. paper DB 기록용 데이터 생성
            pos = self.active_positions[stock_code]
            pos.sell_price = verdict['price']
            pos.sell_reason = reason

            # 2. 격리된 paper DB 기록 및 내부 포지션 업데이트
            self._paper_transition_guard()
            self.db.record_sell(pos)
            del self.active_positions[pos.stock_code]
            
            return True, pos

        except Exception as e:
            logger.error(f"Manager order processing error: {e}")
            if self._strict_paper_errors:
                raise
            return False, None

    def process_buy_order(self, verdict: Dict) -> tuple[bool, Optional[Dict]]:
        """Legacy paper-only compatibility wrapper; never submit a broker order here."""

        return self.apply_paper_buy(verdict)

    def process_sell_order(self, verdict: Dict, reason: str) -> tuple[bool, Optional[Position]]:
        """Legacy paper-only compatibility wrapper; never submit a broker order here."""

        return self.apply_paper_sell(verdict, reason)
