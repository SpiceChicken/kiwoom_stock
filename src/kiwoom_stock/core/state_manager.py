# src/kiwoom_stock/core/state_manager.py
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, cast
from kiwoom_stock.application.ports import PhysicalStateRepository
from kiwoom_stock.core.physics_engine import calculate_net_velocity
from kiwoom_stock.domain.state import (
    calculate_elapsed_hours,
    calculate_initial_velocity_from_rsi,
    calculate_interval_impulse,
    calculate_recovered_velocity,
    calculate_reference_mass,
    calculate_volume_interval,
    calculate_volume_window,
)

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


def _system_now() -> datetime:
    return cast(datetime, getattr(datetime, "now")())


class PhysicalStateTracker:
    def __init__(self, state_repository: PhysicalStateRepository, clock: Optional[Clock] = None):
        self._l1_cache: Dict[str, float] = {}
        self._strength_history: Dict[str, List[Tuple[datetime, float]]] = {}
        self._last_volume: Dict[str, float] = {} # 거래량 추적용 캐시
        self._last_price: Dict[str, float] = {} # 직전 가격 추적용 캐시
        self._vol_history: Dict[str, List[float]] = {} # 💡 틱 거래량 타임스탬프 캐시
        self.state_repository = state_repository
        self._clock = clock or _system_now

    def _get_and_update_prev_strength(self, stock_code: str, current_strength: float) -> float:
        """내부 캐시를 활용하여 5분(300초) 전의 체결강도를 추출합니다."""
        now = self._clock()
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
        last_state = self.state_repository.get_last_physical_state(stock_code)
        if not last_state:
            self._l1_cache[stock_code] = 0.0
            return
            
        db_velocity = last_state['velocity']
        db_timestamp = last_state['timestamp']
        now = self._clock()
        delta_t_hours = calculate_elapsed_hours(db_timestamp, now)
        
        decayed_velocity = calculate_recovered_velocity(db_velocity, db_timestamp, now, decay_constant)
        self._l1_cache[stock_code] = decayed_velocity
        logger.info(f"[{stock_code}] V:{db_velocity:.2f} -> {decayed_velocity:.2f} ({delta_t_hours:.2f}h 경과)")

    def process_tick(
        self, stock_code: str, strength: float, current_price: float, vwap: float, 
        atr_percent: float, vol_ratio: float, rsi: float, tot_sel_req: float, 
        tot_buy_req: float, total_volume: float = 0.0, market_cap: float = 1_000_000_000_000.0
    ) -> Dict[str, Any]:
        
        # 1. 거래량 동결 여부 판독 (시간 정지 방어)
        last_vol = self._last_volume.get(stock_code, -1.0)
        volume_interval = calculate_volume_interval(last_vol, total_volume)
        is_frozen = volume_interval.is_frozen  # 거래량이 멈춰있음을 플래그로 저장
        interval_volume = volume_interval.interval_volume
            
        self._last_volume[stock_code] = total_volume

        # 🟢 실시간 거래량 가속도 추적 (Fuel Exhaustion / Zero-Time Sliding Window)
        vol_history = self._vol_history.setdefault(stock_code, [])
        volume_window = calculate_volume_window(vol_history, interval_volume, is_frozen)
        vol_history[:] = volume_window.history
        volume_drop_ratio = volume_window.drop_ratio

        # 🟢 직전 가격(Previous Price) 로드 및 갱신
        previous_price = self._last_price.get(stock_code, current_price)
        
        # 0.0원(VI 발동)일 때는 캐시를 0으로 덮어쓰지 않음
        if current_price > 0.0:
            self._last_price[stock_code] = current_price
        
        dynamic_cutoff = calculate_reference_mass(market_cap)
        impulse = calculate_interval_impulse(
            interval_volume=interval_volume,
            current_price=current_price,
            reference_mass=dynamic_cutoff,
            is_frozen=is_frozen,
        )
        interval_impulse = impulse.interval_impulse
        interval_amount_krw = impulse.interval_amount_krw

        if is_frozen:
            strength = 0.0
            vol_ratio = 0.0
            interval_impulse = 0.0
        
        # 2. 초기 속도 주입 (첫 감시 종목은 RSI 기반으로 초기 관성 세팅)
        if stock_code not in self._l1_cache:
            self._l1_cache[stock_code] = calculate_initial_velocity_from_rsi(rsi)
            
        previous_velocity = self._l1_cache[stock_code]
        prev_strength_5m = self._get_and_update_prev_strength(stock_code, strength)
        
        forces_dict = calculate_net_velocity(
            strength=strength, current_price=current_price, vwap=vwap,
            atr_percent=atr_percent, previous_velocity=previous_velocity,
            vol_ratio=vol_ratio, rsi=rsi, tot_sel_req=tot_sel_req,
            tot_buy_req=tot_buy_req, prev_strength_5m=prev_strength_5m,
            previous_price=previous_price,
            interval_impulse=interval_impulse,
            interval_amount_krw=interval_amount_krw,
            reference_mass=dynamic_cutoff  # 🟢 컷오프를 기준 질량으로 전달!
        )

        # 산출된 거래량 비율을 엔진에 주입!
        forces_dict["volume_drop_ratio"] = volume_drop_ratio
        
        current_velocity = forces_dict["current_velocity"]
        self._l1_cache[stock_code] = current_velocity

        if not is_frozen:
            self.state_repository.submit_physical_state(stock_code, cast(Mapping[str, Any], forces_dict))
            
        return {"forces": forces_dict}
