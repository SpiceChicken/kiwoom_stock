import statistics
import logging
import math
from datetime import datetime
from collections import deque
from typing import List, Dict

from .collector import MarketDataCollector
from kiwoom_stock.core import indicators as ind
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.types import MarketRegime
from kiwoom_stock.core.state_manager import PhysicalStateTracker

logger = logging.getLogger(__name__)

class MarketAnalyzer:
    """
    [Helper] 시장 환경 분석기 (Physics-Engine Version)
    - 역할: 데이터 수집 및 물리 엔진(StateTracker)으로의 파이프라인 연결
    """
    def __init__(self, client, market_config: Dict, state_tracker: PhysicalStateTracker):
        self.collector = MarketDataCollector(client)
        self.state_tracker = state_tracker
        
        self.market_proxy_code = market_config.get("proxy_code", "069500")
        self.market_rsi = 50.0
        self.market_regime = MarketRegime.UNKNOWN
        self.market_atr_history: deque[float] = deque(maxlen=20)
        
        self.supply_cache: Dict[str, SupplyData] = {}
        self.last_supply_update = datetime.now()
        self.supply_cache[self.market_proxy_code] = SupplyData(stock_code=self.market_proxy_code)

    def update_regime(self):
        """[Market Regime] 시장 성격 정의"""
        try:
            self._update_chart_data(self.market_proxy_code, "60")
            chart_data = self.supply_cache[self.market_proxy_code].chart_data
            if not chart_data: return

            closes = [abs(float(item['cur_prc'])) for item in chart_data][::-1]
            self.market_rsi = ind.calculate_rsi(closes, period=14)
            
            tr_list = []
            for i in range(1, len(chart_data)):
                h = abs(float(chart_data[i]['high_pric']))
                l = abs(float(chart_data[i]['low_pric']))
                pc = abs(float(chart_data[i-1]['cur_prc']))
                tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
            
            atr = statistics.mean(tr_list[:14]) if tr_list else 0.0
            self.market_atr_history.append(atr)
            avg_atr = statistics.mean(self.market_atr_history) if len(self.market_atr_history) >= 5 else atr
            
            is_volatile = atr > (avg_atr * 1.1)
            prev_regime = self.market_regime

            if self.market_rsi >= 60:
                self.market_regime = MarketRegime.VOLATILE_BULL if is_volatile else MarketRegime.STABLE_BULL
            elif self.market_rsi <= 40:
                self.market_regime = MarketRegime.PANIC_BEAR if is_volatile else MarketRegime.QUIET_BEAR
            else:
                self.market_regime = MarketRegime.NEUTRAL

            if prev_regime != self.market_regime:
                logger.info(f"Market Regime Changed: {prev_regime.value} -> {self.market_regime.value}")
        except Exception as e:
            logger.error(f"시장 분석 실패: {e}")

    def update_priority_supply(self, stock_codes: List[str]):
        try:
            for stock_code in stock_codes:
                if stock_code not in self.supply_cache:
                    self.supply_cache[stock_code] = SupplyData(stock_code=stock_code)

                data = self.supply_cache[stock_code]
                chart_5m = self.collector.fetch_minute_chart(stock_code, tic="5")
                chart_5m.reverse()

                self._update_basic_data(data, stock_code)
                self._update_strength_data(data, stock_code)
                self._update_vwap_data(data, chart_5m)
                self._update_volatility_data(data, chart_5m) 
                self._update_rsi_data(data, chart_5m) 
                
                # 🛡️ [Rate Limit 방어벽] 
                current_velocity = getattr(data, 'forces', {}).get('current_velocity', 0.0)
                
                if data.strength > 100.0 or current_velocity > 0.0:
                    order_book = self._fetch_safe_order_book(stock_code) 
                else:
                    order_book = {'tot_sel_req': 0.0, 'tot_buy_req': 0.0}

                tracker_result = self.state_tracker.process_tick(
                    stock_code=stock_code,
                    strength=data.strength,
                    current_price=data.cur_prc,
                    vwap=data.vwap,
                    atr_percent=getattr(data, 'atr_percent', 0.5),
                    vol_ratio=data.vol_ratio,
                    rsi=getattr(data, 'trend_rsi', 50.0),
                    tot_sel_req=order_book.get('tot_sel_req', 0.0),
                    tot_buy_req=order_book.get('tot_buy_req', 0.0),
                    total_volume=data.trde_qty,
                    market_cap=(data.mac * 100_000_000.0)
                )
                
                data.forces = tracker_result["forces"]

            self.last_supply_update = datetime.now()
        except Exception as e:
            logger.error(f"데이터 파이프라인 오류: {e}")

    def _fetch_safe_order_book(self, code: str) -> Dict[str, float]:
        """[New] [ka10004] 주식호가요청 - 자기력(Magnetic Force) 계산용"""
        try:
            ob = self.collector.fetch_order_book(code)
            return {
                'tot_sel_req': float(ob.get('tot_sel_req', 0.0)),
                'tot_buy_req': float(ob.get('tot_buy_req', 0.0))
            }
        except Exception:
            return {'tot_sel_req': 0.0, 'tot_buy_req': 0.0}

    def _update_strength_data(self, data: SupplyData, code: str):
        """[New] 현재 체결강도와 5분 전 체결강도(Jerk 산출용)를 동시에 수집합니다."""
        # [ka10046] 체결강도추이시간별요청 대체 구현부
        strength_history = self.collector.fetch_tick_strength(code)
        
        if strength_history and len(strength_history) > 0:
            data.strength = float(strength_history[0].get("cntr_str", 100.0))
            # 5분 전(index 5 내외) 데이터가 있다면 Jerk 기준점(prev_strength_5m)으로 저장
            if len(strength_history) >= 5:
                data.prev_strength_5m = float(strength_history[4].get("cntr_str", data.strength))
            else:
                data.prev_strength_5m = data.strength
        else:
            data.strength = 100.0
            data.prev_strength_5m = 100.0

    def _update_rsi_data(self, data: SupplyData, chart_5m: List[Dict]):
        """[New] Drag 과열 계산용 5분봉 RSI 산출"""
        if not chart_5m or len(chart_5m) < 14:
            data.trend_rsi = 50.0
            return
        closes = [float(d['cur_prc']) for d in chart_5m]
        data.trend_rsi = round(ind.calculate_rsi(closes, period=14), 2)

    def _update_basic_data(self, data: SupplyData, code: str):
        basic = self.collector.fetch_stock_basic(code)
        data.vol_ratio = float(basic.get('trde_pre', 0.0))
        data.trde_qty = int(basic.get('trde_qty', 0))
        data.cur_prc = float(basic.get('cur_prc', 0))
        data.mac = float(basic.get('mac', 0))

    def _update_chart_data(self, code: str, tic: str):
        chart = self.collector.fetch_minute_chart(code, tic)
        self.supply_cache[code].chart_data = chart

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
        
        opens = [float(d['open_pric']) for d in chart_5m]
        highs = [float(d['high_pric']) for d in chart_5m]
        lows = [float(d['low_pric']) for d in chart_5m]
        closes = [float(d['cur_prc']) for d in chart_5m]
        
        # 1. 일반 ATR (기존 로직 유지)
        data.atr_percent = ind.calculate_atr_percent(
            highs=highs,
            lows=lows,
            closes=closes,
            period=14,
            current_price=data.cur_prc)
        
        # 2. Down-ATR (순수 하방 변동성) 
        fake_closes = opens[1:] + [opens[-1]] 
        
        data.down_atr_percent = ind.calculate_atr_percent(
            highs=opens, 
            lows=lows, 
            closes=fake_closes, 
            period=14,
            current_price=closes[-1]  
        )