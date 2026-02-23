# src/kiwoom_stock/core/state_manager.py
import math
import logging
import asyncio
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple, Any
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.core.physics_engine import calculate_net_velocity, calculate_physical_score

logger = logging.getLogger(__name__)

class PhysicalStateTracker:
    def __init__(self, db_logger: TradeLogger):
        self._l1_cache: Dict[str, float] = {}
        self._strength_history: Dict[str, List[Tuple[datetime, float]]] = {}
        self._last_volume: Dict[str, float] = {} # [추가] 거래량 추적용 캐시
        self.db = db_logger
        
        # [방어 로직] 메인 스레드 블로킹을 막기 위한 전용 백그라운드 워커 1개 배정
        self._db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="PhysDBWorker")

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

    def _background_async_log(self, stock_code: str, forces_dict: Dict[str, Any]):
        """[내부 헬퍼] 격리된 스레드에서 자체 이벤트 루프를 생성하여 비동기 DB 함수를 안전하게 실행"""
        try:
            asyncio.run(self.db.async_log_physical_state(stock_code, forces_dict))
        except Exception as e:
            logger.error(f"[{stock_code}] 백그라운드 DB 기록 중 치명적 오류: {e}", exc_info=True)

    def process_tick(
        self, stock_code: str, strength: float, current_price: float, vwap: float, 
        atr_percent: float, vol_ratio: float, rsi: float, tot_sel_req: float, 
        tot_buy_req: float, max_instant_amt_100m: float, current_volume: float = 0.0
    ) -> Dict[str, Any]:
        
        # 1. 거래량 동결 방어벽
        last_vol = self._last_volume.get(stock_code, -1.0)
        if last_vol == current_volume and current_volume >= 0.0:
            velocity = self._l1_cache.get(stock_code, 0.0)
            return {"total_score": calculate_physical_score(velocity), "forces": {}}
        
        self._last_volume[stock_code] = current_volume
        
        # 초기 속도 주입
        if stock_code not in self._l1_cache:
            initial_velocity = (rsi - 50.0) / 10.0
            self._l1_cache[stock_code] = max(0.0, initial_velocity)
            
        previous_velocity = self._l1_cache[stock_code]
        prev_strength_5m = self._get_and_update_prev_strength(stock_code, strength)
        
        forces_dict = calculate_net_velocity(
            strength=strength, current_price=current_price, vwap=vwap,
            atr_percent=atr_percent, previous_velocity=previous_velocity,
            vol_ratio=vol_ratio, rsi=rsi, tot_sel_req=tot_sel_req,
            tot_buy_req=tot_buy_req, prev_strength_5m=prev_strength_5m,
            max_instant_amt_100m=max_instant_amt_100m
        )
        
        current_velocity = forces_dict["current_velocity"]
        self._l1_cache[stock_code] = current_velocity

        # 2. [방어 로직] 런타임 에러(no running event loop) 원천 차단
        # ThreadPoolExecutor를 통해 메인 엔진의 동기 틱을 전혀 방해하지 않고 비동기 DB 로그를 발사(Fire & Forget)
        self._db_executor.submit(self._background_async_log, stock_code, forces_dict)
            
        return {"total_score": calculate_physical_score(current_velocity), "forces": forces_dict}