import statistics
import logging
from datetime import datetime
from collections import deque
from typing import List, Dict

from .collector import MarketDataCollector
from kiwoom_stock.core import indicators as ind
from kiwoom_stock.core.schema import SupplyData, PgmData, ForeignData
from kiwoom_stock.core.types import MarketRegime

logger = logging.getLogger(__name__)

class MarketAnalyzer:
    """
    [Helper] 시장 환경 분석기 (Refactored to use SupplyData)
    - 역할 1: 전체 시장의 성격(Regime)을 진단 (RSI, ATR 활용)
    - 역할 2: 개별 종목의 수급 및 차트 데이터를 수집하여 SupplyData 객체로 캐싱
    - 데이터 구조: 기존의 암시적인 Dict 대신 명시적인 SupplyData Dataclass 사용
    """
    def __init__(self, client, market_config: Dict):
        self.collector = MarketDataCollector(client)
        self.market_proxy_code = market_config.get("proxy_code", "069500") # KODEX 200 등 시장 대표 ETF
        self.market_rsi = 50.0
        self.market_regime = MarketRegime.UNKNOWN
        self.market_atr_history = deque(maxlen=20)
        
        # [Refactor] Dict[str, Dict] -> Dict[str, SupplyData]
        # 종목 코드를 키로, 정형화된 데이터 객체(SupplyData)를 값으로 저장
        self.supply_cache: Dict[str, SupplyData] = {}
        self.last_supply_update = datetime.now()
        self.supply_cache[self.market_proxy_code] = SupplyData(stock_code=self.market_proxy_code)

    def update_regime(self):
        """
        [Market Regime] 시장 성격 정의
        - 시장 대표 종목(Proxy)의 60분봉 데이터를 분석하여 현재 장세 판단
        - RSI: 추세 강도 및 방향성 (60 이상: 강세 / 40 이하: 약세)
        - ATR: 변동성 (평균 대비 1.1배 이상: 변동성 장세)
        """
        try:
            self._update_chart_data(self.market_proxy_code, "60")
            # SupplyData 객체에서 chart_data 접근 (타입 안전성 확보)
            chart_data = self.supply_cache[self.market_proxy_code].chart_data
            if not chart_data: return

            # 최신 데이터가 뒤로 오도록 정렬 [과거 -> 최신]
            closes = [abs(float(item['cur_prc'])) for item in chart_data][::-1]
            self.market_rsi = ind.calculate_rsi(closes, period=14)
            
            # ATR(변동폭) 계산
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

            # RSI와 변동성(ATR)을 조합하여 4사분면 매트릭스로 장세 구분
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
        """
        [Data Pipeline] 감시 대상 종목 데이터 일괄 업데이트
        - API 호출 효율화를 위해 벌크 데이터(프로그램, 외인)를 먼저 가져온 후 개별 종목에 분배
        """
        try:
            # 1. 벌크 데이터 수집 (1회 호출로 전체 종목 커버)
            pgm_map = self.collector.fetch_program_trade()
            frgn_map = self.collector.fetch_foreign_window_trade()

            for stock_code in stock_codes:
                # 캐시 초기화
                if stock_code not in self.supply_cache:
                    self.supply_cache[stock_code] = SupplyData(stock_code=stock_code)

                # 2. 개별 종목 차트 데이터 수집
                chart_60m = self.collector.fetch_minute_chart(stock_code, tic="60")
                chart_5m = self.collector.fetch_minute_chart(stock_code, tic="5")
                chart_1m = self.collector.fetch_minute_chart(stock_code, tic="1")

                # 데이터 시간순 정렬 [과거 -> 최신] (Indicators 모듈 호환성)
                chart_60m.reverse()
                chart_5m.reverse()
                chart_1m.reverse()

                # 3. SupplyData 객체 필드 업데이트 (직관적 접근)
                data = self.supply_cache[stock_code]
                
                self._update_program_data(data, pgm_map)
                self._update_foreign_data(data, frgn_map)
                self._update_basic_data(data, stock_code)
                self._update_strength_data(data, stock_code)
                
                # 지표별 업데이트 (지표 계산에 필요한 데이터가 모이면 수행)
                self._update_alpha_data(data, chart_1m)
                self._update_vwap_data(data, chart_5m)
                self._update_trend_rsi(data, chart_60m)
                self._update_volatility_data(data, chart_5m)
                self._update_trend_data(data, chart_5m)

            self.last_supply_update = datetime.now()
        except Exception as e:
            logger.error(f"전체 수급 데이터 통합 중 오류: {e}")

    # --- Helper Methods: SupplyData 객체를 직접 조작하여 데이터 무결성 유지 ---

    def _update_basic_data(self, data: SupplyData, code: str):
        """기본 시세 및 거래량 비율 업데이트"""
        basic = self.collector.fetch_stock_basic(code)
        data.vol_ratio = float(basic.get('trde_pre', 0.0))
        data.trde_qty = int(basic.get('trde_qty', 0))
        data.cur_prc = float(basic.get('cur_prc', 0))
        # 거래대금 추정 (거래량 * 현재가)
        data.market_total_amount = max(1.0, float(data.trde_qty) * float(data.cur_prc))

    def _update_strength_data(self, data: SupplyData, code: str):
        """체결강도 업데이트"""
        strength_history = self.collector.fetch_tick_strength(code)
        data.strength = float(strength_history[0].get("cntr_str", 100.0)) if strength_history else 100.0

    def _update_chart_data(self, code: str, tic: str):
        """차트 데이터(Raw) 업데이트"""
        chart = self.collector.fetch_minute_chart(code, tic)
        self.supply_cache[code].chart_data = chart

    def _update_program_data(self, data: SupplyData, pgm_map: Dict):
        """프로그램 매매 데이터 업데이트 (PgmData 객체 활용)"""
        if data.stock_code in pgm_map:
            p_info = pgm_map[data.stock_code]
            data.pgm_data = PgmData(
                net_amt=float(p_info.get('net_amt', 0)),
                ratio=float(p_info.get('ratio', 0)),
                buy_amt=float(p_info.get('buy_amt', 0)),
                sel_amt=float(p_info.get('sel_amt', 0))
            )

    def _update_foreign_data(self, data: SupplyData, frgn_map: Dict):
        """외국계 창구 데이터 업데이트 (ForeignData 객체 활용)"""
        if data.stock_code in frgn_map:
            f_info = frgn_map[data.stock_code]
            data.foreign_data = ForeignData(
                netprps_prica=float(f_info.get('netprps_prica', 0)),
                trde_prica=float(f_info.get('trde_prica', 1.0))
            )

    def _update_alpha_data(self, data: SupplyData, chart_1m: List[Dict]):
        """Alpha Score 계산을 위한 1분봉 시계열 준비"""
        if len(chart_1m) < 6: return
        data.price_series = [float(d['cur_prc']) for d in chart_1m]
        data.volume_series = [float(d['trde_qty']) for d in chart_1m]
        
        # 거래량 급증 팩터 미리 계산 (Volume Power)
        avg_prev_vol = max(1.0, sum(data.volume_series[-5:-1]) / 4)
        data.vol_factor = min(2.0, data.volume_series[-1] / avg_prev_vol)

    def _update_vwap_data(self, data: SupplyData, chart_5m: List[Dict]):
        """VWAP (거래량 가중 평균가) 업데이트"""
        if not chart_5m: return
        prices = [abs(float(d['cur_prc'])) for d in chart_5m]
        vols = [float(d['trde_qty']) for d in chart_5m]

        total_val = sum(p * v for p, v in zip(prices, vols))
        total_vol = sum(vols)
        vwap = total_val / total_vol if total_vol > 0 else prices[-1]

        data.prev_vwap = data.vwap if data.vwap > 0 else vwap
        data.vwap = round(vwap, 2)
        data.price = prices[-1]

    def _update_trend_rsi(self, data: SupplyData, chart_60m: List[Dict]):
        """장기 추세 판단용 60분봉 RSI 업데이트"""
        if not chart_60m: return
        prices = [float(d['cur_prc']) for d in chart_60m]
        if len(prices) > 14:
            data.trend_rsi = round(ind.calculate_rsi(prices, period=14), 2)

    def _update_volatility_data(self, data: SupplyData, chart_5m: List[Dict]):
        """변동성 지표(ATR %) 업데이트"""
        if not chart_5m: return
        highs = [float(d['high_pric']) for d in chart_5m]
        lows = [float(d['low_pric']) for d in chart_5m]
        closes = [float(d['cur_prc']) for d in chart_5m]
        data.atr_percent = ind.calculate_atr_percent(highs, lows, closes, period=14)

    def _update_trend_data(self, data: SupplyData, chart_5m: List[Dict]):
        """이동평균선(EMA) 업데이트"""
        if len(chart_5m) < 60: return
        prices = [float(d['cur_prc']) for d in chart_5m]
        
        # 기울기 계산을 위해 이전 EMA 값 보존
        data.prev_ema60 = data.ema60 if data.ema60 > 0 else ind.calculate_ema(prices, 60)
        
        # 최신 EMA 계산
        data.ema5 = ind.calculate_ema(prices, 5)
        data.ema20 = ind.calculate_ema(prices, 20)
        data.ema60 = ind.calculate_ema(prices, 60)