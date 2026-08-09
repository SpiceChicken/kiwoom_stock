import statistics
import logging
from datetime import datetime
from collections import deque
from typing import Callable, List, Dict, Mapping, NoReturn, Optional, Sequence

from .collector import (
    MarketDataCollectionError,
    MarketDataCollector,
    MarketDataFailureKind,
)
from kiwoom_stock.application.ports import MarketDataGateway
from kiwoom_stock.application.ports import PhysicalStatePersistenceError
from kiwoom_stock.core import indicators as ind
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.models import PhysicalContinuityEvidence, PhysicalObservation
from kiwoom_stock.domain.indicators import INDICATOR_PERIOD
from kiwoom_stock.domain.state import PhysicalStateValidationError

logger = logging.getLogger(__name__)

class MarketAnalyzer:
    """
    [Helper] 시장 환경 분석기 (Physics-Engine Version)
    - 역할: 데이터 수집 및 물리 엔진(StateTracker)으로의 파이프라인 연결
    """
    def __init__(
        self,
        market_gateway: MarketDataGateway,
        market_config: Dict,
        state_tracker: PhysicalStateTracker,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.collector = MarketDataCollector(market_gateway)
        self.state_tracker = state_tracker
        self._clock = clock or (lambda: datetime.now().astimezone())
        
        self.market_proxy_code = market_config.get("proxy_code", "069500")
        self.market_rsi = 50.0
        self.market_regime = MarketRegime.UNKNOWN
        self.market_atr_history: deque[float] = deque(maxlen=20)
        
        self.supply_cache: Dict[str, SupplyData] = {}
        self.last_supply_update = datetime.now()
        self.supply_cache[self.market_proxy_code] = SupplyData(stock_code=self.market_proxy_code)

    def update_regime(self) -> None:
        """[Market Regime] 시장 성격 정의"""
        try:
            self._update_chart_data(self.market_proxy_code, "60")
            raw_chart = self.supply_cache[self.market_proxy_code].chart_data
            chart_data = raw_chart
            closes = [abs(float(item["cur_prc"])) for item in chart_data]
            market_rsi = ind.calculate_rsi(closes, period=INDICATOR_PERIOD)
            tr_list = self._calculate_true_ranges(chart_data)
            atr = statistics.mean(tr_list[-INDICATOR_PERIOD:])
            next_atr_history = (*self.market_atr_history, atr)[-20:]
            avg_atr = (
                statistics.mean(next_atr_history)
                if len(next_atr_history) >= 5
                else atr
            )
            is_volatile = atr > (avg_atr * 1.1)
            prev_regime = self.market_regime
            if market_rsi >= 60:
                next_regime = (
                    MarketRegime.VOLATILE_BULL
                    if is_volatile
                    else MarketRegime.STABLE_BULL
                )
            elif market_rsi <= 40:
                next_regime = (
                    MarketRegime.PANIC_BEAR
                    if is_volatile
                    else MarketRegime.QUIET_BEAR
                )
            else:
                next_regime = MarketRegime.NEUTRAL
            self.market_rsi = market_rsi
            self.market_atr_history.clear()
            self.market_atr_history.extend(next_atr_history)
            self.market_regime = next_regime
            self.supply_cache[self.market_proxy_code].chart_data = chart_data
            if prev_regime != next_regime:
                logger.info(
                    "Market Regime Changed: %s -> %s",
                    prev_regime.value,
                    next_regime.value,
                )
        except MarketDataCollectionError:
            self.market_regime = MarketRegime.UNKNOWN
            self.supply_cache[self.market_proxy_code].chart_data = []
            raise
        except Exception as error:
            self.market_regime = MarketRegime.UNKNOWN
            self.supply_cache[self.market_proxy_code].chart_data = []
            raise MarketDataCollectionError(
                MarketDataFailureKind.MALFORMED,
                "market_regime_60m",
            ) from error

    def update_priority_supply(self, stock_codes: List[str]):
        try:
            pending_inputs: List[tuple[SupplyData, Dict[str, float]]] = []
            for stock_code in stock_codes:
                data = SupplyData(stock_code=stock_code)
                chart_5m = self.collector.fetch_indicator_chart(stock_code, tic="5")

                self._update_basic_data(data, stock_code)
                self._update_strength_data(data, stock_code)
                self._update_vwap_data(data, chart_5m)
                self._update_volatility_data(data, chart_5m) 
                self._update_rsi_data(data, chart_5m) 
                
                # 🛡️ [Rate Limit 방어벽] 
                prior_data = self.supply_cache.get(stock_code)
                current_velocity = (
                    prior_data.forces.get('current_velocity', 0.0)
                    if prior_data is not None
                    else 0.0
                )
                
                if data.strength > 100.0 or current_velocity > 0.0:
                    order_book = self._fetch_safe_order_book(stock_code) 
                else:
                    order_book = {'tot_sel_req': 0.0, 'tot_buy_req': 0.0}

                pending_inputs.append((data, order_book))

            observed_at = self._clock()
            observations = tuple(
                PhysicalObservation(
                    stock_code=data.stock_code,
                    observed_at=observed_at,
                    strength=data.strength,
                    prev_strength_5m=data.prev_strength_5m,
                    current_price=data.cur_prc,
                    cumulative_volume=data.trde_qty,
                    vwap=data.vwap,
                    atr_percent=getattr(data, 'atr_percent', 0.5),
                    vol_ratio=data.vol_ratio,
                    rsi=getattr(data, 'trend_rsi', 50.0),
                    tot_sel_req=order_book.get('tot_sel_req', 0.0),
                    tot_buy_req=order_book.get('tot_buy_req', 0.0),
                    market_cap=(data.mac * 100_000_000.0),
                )
                for data, order_book in pending_inputs
            )
            tracker_results = self.state_tracker.process_observations(observations)
            if tuple(tracker_results) != tuple(stock_codes):
                raise PhysicalStateValidationError(
                    "tracker batch result does not match targets"
                )
            validated_results = []
            for data, _ in pending_inputs:
                tracker_result = tracker_results[data.stock_code]
                continuity = tracker_result["continuity"]
                if not isinstance(continuity, PhysicalContinuityEvidence):
                    raise PhysicalStateValidationError(
                        "tracker returned invalid continuity evidence"
                    )
                forces = tracker_result["forces"]
                if not isinstance(forces, dict):
                    raise PhysicalStateValidationError(
                        "tracker returned invalid forces"
                    )
                validated_results.append((data, forces, continuity))

            next_supply_cache = dict(self.supply_cache)
            for data, forces, continuity in validated_results:
                data.forces = forces
                data.continuity = continuity
                next_supply_cache[data.stock_code] = data
            self.supply_cache = next_supply_cache

            self.last_supply_update = datetime.now()
        except Exception as error:
            self._fail_physical_supply_update(stock_codes, error)

    def _fail_physical_supply_update(
        self,
        stock_codes: List[str],
        error: Exception,
    ) -> NoReturn:
        """Clear stale physical outputs and terminate every failed supply cycle."""

        for stock_code in stock_codes:
            cached_data = self.supply_cache.get(stock_code)
            if cached_data is not None:
                cached_data.forces = {}
                cached_data.continuity = None
        if isinstance(
            error,
            (
                MarketDataCollectionError,
                PhysicalStateValidationError,
                PhysicalStatePersistenceError,
            ),
        ):
            raise error
        raise PhysicalStateValidationError(
            f"physical supply pipeline failed ({type(error).__name__})"
        ) from error

    def _fetch_safe_order_book(self, code: str) -> Dict[str, float]:
        """[New] [ka10004] 주식호가요청 - 자기력(Magnetic Force) 계산용"""
        ob = self.collector.fetch_order_book(code)
        if not ob:
            raise MarketDataCollectionError(
                MarketDataFailureKind.EMPTY,
                "order_book",
            )
        return {
            "tot_sel_req": float(ob["tot_sel_req"]),
            "tot_buy_req": float(ob["tot_buy_req"]),
        }

    def _update_strength_data(self, data: SupplyData, code: str):
        """[New] 현재 체결강도와 5분 전 체결강도(Jerk 산출용)를 동시에 수집합니다."""
        # [ka10046] 체결강도추이시간별요청 대체 구현부
        strength_history = self.collector.fetch_tick_strength(code)
        if not strength_history:
            raise MarketDataCollectionError(
                MarketDataFailureKind.EMPTY,
                "tick_strength",
            )
        if len(strength_history) < 5:
            raise MarketDataCollectionError(
                MarketDataFailureKind.MALFORMED,
                "tick_strength",
            )
        data.strength = float(strength_history[0]["cntr_str"])
        data.prev_strength_5m = float(strength_history[4]["cntr_str"])

    def _update_rsi_data(self, data: SupplyData, chart_5m: List[Dict]):
        """[New] Drag 과열 계산용 5분봉 RSI 산출"""
        closes = [abs(float(d['cur_prc'])) for d in chart_5m]
        data.trend_rsi = round(ind.calculate_rsi(closes, period=INDICATOR_PERIOD), 2)

    def _update_basic_data(self, data: SupplyData, code: str):
        basic = self.collector.fetch_stock_basic(code)
        if not basic:
            raise MarketDataCollectionError(
                MarketDataFailureKind.EMPTY,
                "stock_basic",
            )
        data.vol_ratio = float(basic["trde_pre"])
        data.trde_qty = int(basic["trde_qty"])
        data.cur_prc = abs(float(basic["cur_prc"]))
        data.mac = float(basic["mac"])

    def _update_chart_data(self, code: str, tic: str):
        chart = self.collector.fetch_indicator_chart(code, tic)
        self.supply_cache[code].chart_data = chart

    @staticmethod
    def _calculate_true_ranges(
        chart: Sequence[Mapping[str, object]],
    ) -> List[float]:
        true_ranges = []
        for previous, current in zip(chart, chart[1:]):
            previous_close = abs(float(str(previous["cur_prc"])))
            high = abs(float(str(current["high_pric"])))
            low = abs(float(str(current["low_pric"])))
            if previous_close <= 0.0 or high <= 0.0 or low <= 0.0 or high < low:
                raise MarketDataCollectionError(
                    MarketDataFailureKind.MALFORMED,
                    "chart_true_range",
                )
            true_ranges.append(
                max(
                    high - low,
                    abs(high - previous_close),
                    abs(low - previous_close),
                )
            )
        if not true_ranges:
            raise MarketDataCollectionError(
                MarketDataFailureKind.MALFORMED,
                "chart_true_range",
            )
        return true_ranges

    def _update_vwap_data(self, data: SupplyData, chart_5m: List[Dict]):
        if not chart_5m: return
        prices = [abs(float(d['cur_prc'])) for d in chart_5m]
        vols = [float(d['trde_qty']) for d in chart_5m]

        total_val = sum(p * v for p, v in zip(prices, vols))
        total_vol = sum(vols)
        vwap = total_val / total_vol if total_vol > 0 else prices[-1]
        
        data.vwap = round(vwap, 2)
        data.price = prices[-1]

    def _update_volatility_data(self, data: SupplyData, chart_5m: List[Dict]):
        if not chart_5m: return
        
        opens = [abs(float(d['open_pric'])) for d in chart_5m]
        highs = [abs(float(d['high_pric'])) for d in chart_5m]
        lows = [abs(float(d['low_pric'])) for d in chart_5m]
        closes = [abs(float(d['cur_prc'])) for d in chart_5m]
        
        # 1. 일반 ATR (기존 로직 유지)
        data.atr_percent = ind.calculate_atr_percent(
            highs=highs,
            lows=lows,
            closes=closes,
            period=INDICATOR_PERIOD,
            current_price=data.cur_prc)
        
        # 2. Down-ATR (순수 하방 변동성) 
        fake_closes = opens[1:] + [opens[-1]] 
        
        data.down_atr_percent = ind.calculate_atr_percent(
            highs=opens, 
            lows=lows, 
            closes=fake_closes, 
            period=INDICATOR_PERIOD,
            current_price=closes[-1]  
        )
