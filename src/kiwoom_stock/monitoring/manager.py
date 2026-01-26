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
    def __init__(self, client, db, filter_config: Dict, strategy_config: Dict):
        self.client = client
        self.db = db
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 50)
        
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}

        raw_positions = self.db.load_open_positions()
        # [개선] Position 객체로 관리
        self.active_positions: Dict[str, Position] = {
            code: Position(**data) for code, data in raw_positions.items()
        }
        # [안전장치] 계좌 전체 손실 제한 (예: -5%)
        self.total_loss_limit = strategy_config.get("total_loss_limit", -5)

        # [최적화] 문자열을 time 객체로 미리 변환 (루프 내 오버헤드 제거)
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        # [수정] 장 마감 3분 전 강제 청산 시간 계산 (오버헤드 방지를 위해 미리 계산)
        # datetime.combine을 사용하여 안전하게 시간 연산 수행
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()
        
        # [신규] 익절/손절/감쇠 설정 로드
        self.decay_rate = strategy_config.get("score_decay_rate", 0.15)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025) # 기본 2.5%
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

    def check_kill_switch(self, status_log: Dict) -> bool:
        """DB에 기록된 당일 확정 손익과 현재 보유 종목의 미실현 손익을 합산합니다."""
        
        # 1. 기존 매매 데이터(DB)에서 오늘 확정된 누적 수익률 가져오기
        # TradeLogger에 오늘 날짜의 'CLOSED' 상태인 profit_rate 합계를 구하는 메서드가 있다고 가정
        realized_pnl = self.db.get_today_realized_pnl() # [핵심 개선] DB 데이터 참조
        
        # 2. 현재 보유 중인 종목(active_positions)의 실시간 손익 계산
        unrealized_pnl = 0.0
        for code, pos in self.active_positions.items():
            log = status_log.get(code)
            if log and "price" in log:
                # 내 기존 매수 데이터(pos['buy_price'])와 현재가 비교
                profit = (log['price'] / pos.buy_price - 1) * 100
                unrealized_pnl += profit
                
        # 3. 전체 합산 (확정 + 미실현)
        total_pnl = realized_pnl + unrealized_pnl
        
        if total_pnl <= self.total_loss_limit:
            logger.critical(f"🚨 [KILL-SWITCH] 오늘 전체 손실 {total_pnl:.2f}% 도달 (한도: {self.total_loss_limit}%)")
            return True
        return False

    def get_exit_reason(self, pos: Position, current_score: float, strong_threshold: float) -> Optional[str]:
        """
        설정된 익절/손절/시간/점수 조건을 검사하여 매도 사유를 반환합니다.
   
        """
        # 현재 수익률 계산 (소수점 단위)
        profit_rate = (pos.sell_price / pos.buy_price - 1)
        
        # 1. 시간 기반 당일 청산 (장 마감 3분 전부터 최우선 수행)
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        # 2. 하드 손절 (Stop Loss) - 설정값 이하로 하락 시 즉시 매도
        if profit_rate <= self.stop_loss_rate:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        # 3. 지능형 익절 (Take Profit)
        # 수익률이 목표치 이상이지만, 점수가 여전히 강하면(strong_threshold 이상) 매도를 미룹니다.
        if profit_rate >= self.target_profit_rate:
            if current_score >= strong_threshold:
                return None # 기세가 좋으므로 익절 보류 (Let the winner run)
            return f"Take Profit (+{profit_rate*100:.1f}%)"

        # 4. 상대적 점수 하락 (Score Decay)
        sell_threshold = pos.buy_score * (1 - self.decay_rate)
        if current_score < sell_threshold:
            return f"Score Decay (-{self.decay_rate*100:.0f}%)"

        return None

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
        
        # [추상화 호출] 판정은 평가기에게 맡깁니다.
        pos.sell_reason = self.get_exit_reason(pos, log['score'], strong_threshold)
        
        if pos.sell_reason:
            # 매도 기록 및 포지션 제거
            self.db.record_sell(pos)
            notifier.notify_sell(pos)
            del self.active_positions[stock_code]

    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (에러 수정 버전)"""
        now = datetime.now()
        if now.weekday() >= 5: return False
        
        # 시작 시간(09:00 권장)과 종료 시간(exit_time) 사이인지 비교
        return time(8, 30) <= now.time() <= self.exit_time_obj