# src/kiwoom_stock/core/state_manager.py
import math
import logging
from datetime import datetime
from typing import Dict, List, Tuple, Any
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.core.physics_engine import calculate_net_velocity, calculate_physical_score

logger = logging.getLogger(__name__)

class PhysicalStateTracker:
    def __init__(self, db_logger: TradeLogger):
        self._l1_cache: Dict[str, float] = {}
        # [수정] 5분 전 체결강도 추적을 위한 상태 저장소 (종목코드 -> [(시간, 강도)])
        self._strength_history: Dict[str, List[Tuple[datetime, float]]] = {}
        self.db = db_logger

    def _get_and_update_prev_strength(self, stock_code: str, current_strength: float) -> float:
        """내부 캐시를 활용하여 5분(300초) 전의 체결강도를 추출합니다."""
        now = datetime.now()
        history = self._strength_history.setdefault(stock_code, [])
        history.append((now, current_strength))
        
        # 5분이 훌쩍 넘은 오래된 데이터는 메모리에서 제거 (300초 기준 여유 있게 320초)
        while history and (now - history[0][0]).total_seconds() > 320:
            history.pop(0)
            
        # 큐의 첫 번째 원소가 대략 4분~5분 이상 경과한 데이터라면 5분 전 강도로 취급
        if len(history) > 1 and (now - history[0][0]).total_seconds() >= 240:
            return history[0][1]
        
        return current_strength
        
    def recover_state_from_crash(self, stock_code: str, decay_constant: float = 0.5):
        """[크래시 복구: Time-Decayed Inertia]"""
        last_state = self.db.get_last_physical_state(stock_code)
        if not last_state:
            self._l1_cache[stock_code] = 0.0
            return
            
        db_velocity = last_state['velocity']
        db_timestamp = last_state['timestamp']
        delta_t_hours = (datetime.now() - db_timestamp).total_seconds() / 3600.0
        
        decayed_velocity = db_velocity * math.exp(-decay_constant * delta_t_hours)
        self._l1_cache[stock_code] = decayed_velocity
        logger.info(f"[{stock_code}] V:{db_velocity:.2f} -> {decayed_velocity:.2f} ({delta_t_hours:.2f}h 경과)")

    def process_tick(
        self, stock_code: str, strength: float, current_price: float, vwap: float, 
        atr_percent: float, vol_ratio: float, rsi: float, tot_sel_req: float, 
        tot_buy_req: float, max_instant_amt_100m: float
    ) -> Dict[str, Any]:
        
        # [수정] 종목이 캐시에 없으면(첫 추적이면) RSI를 기반으로 초기 속도를 주입합니다.
        # 예: RSI 70이면 -> 초기 속도 2.0 (약 88점으로 즉시 시작하여 첫 사이클 매수 가능)
        if stock_code not in self._l1_cache:
            initial_velocity = (rsi - 50.0) / 10.0
            self._l1_cache[stock_code] = max(0.0, initial_velocity)
            
        previous_velocity = self._l1_cache[stock_code]
        
        # [수정] 내부 메모리에서 5분 전 체결강도 획득
        prev_strength_5m = self._get_and_update_prev_strength(stock_code, strength)
        
        # 물리 엔진 호출 (Dictionary 반환)
        forces_dict = calculate_net_velocity(
            strength=strength,
            current_price=current_price,
            vwap=vwap,
            atr_percent=atr_percent,
            previous_velocity=previous_velocity,
            vol_ratio=vol_ratio,
            rsi=rsi,
            tot_sel_req=tot_sel_req,
            tot_buy_req=tot_buy_req,
            prev_strength_5m=prev_strength_5m,
            max_instant_amt_100m=max_instant_amt_100m
        )
        
        current_velocity = forces_dict["current_velocity"]
        self._l1_cache[stock_code] = current_velocity
        
        # [수정] DB 백업 시 전체 벡터 힘 데이터를 전달
        self.db.async_log_physical_state(stock_code, forces_dict)
        
        final_score = calculate_physical_score(current_velocity)
        
        return {
            "total_score": final_score,
            "forces": forces_dict
        }