import statistics
import logging
from enum import Enum
from collections import deque
from typing import Dict
from ..api.parser import clean_numeric

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

class MarketRegime(Enum):
    STABLE_BULL = "안정적 강세장"
    VOLATILE_BULL = "변동성 강세장"
    QUIET_BEAR = "조용한 하락장"
    PANIC_BEAR = "패닉 하락장"
    NEUTRAL = "평온 구간"
    UNKNOWN = "Unknown"

class MarketAnalyzer:
    """[Helper] 시장 환경 분석기: 레짐 진단 및 수급 캐싱 담당"""
    def __init__(self, client, trend_calc, market_config: Dict):
        self.client = client
        self.trend_calc = trend_calc
        self.market_proxy_code = market_config.get("proxy_code", "069500")
        self.market_rsi = 50.0
        self.market_regime = MarketRegime.UNKNOWN
        self.market_atr_history = deque(maxlen=20)
        self.supply_cache: Dict[str, Dict] = {}

    def update_regime(self):
        """RSI와 ATR 분석을 통한 시장 성격 정의"""
        try:
            chart_data = self.client.market.get_minute_chart(self.market_proxy_code, tic="60")
            closes = [item['close'] for item in chart_data]
            self.market_rsi = self.trend_calc.calculate(closes)
            
            tr_list = []
            for i in range(1, len(chart_data)):
                h, l, pc = chart_data[i]['high'], chart_data[i]['low'], chart_data[i-1]['close']
                tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
            
            atr = statistics.mean(tr_list[-14:]) if tr_list else 0.0
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

    def fetch_supply_data(self):
        """외인/기관 수급 데이터를 분리하여 캐싱합니다."""
        try:
            self.supply_cache = {}
            for invsr, key in [("6", "f"), ("7", "i")]:
                items = self.client.market.get_investor_supply(market_tp="001", investor_tp=invsr)
                for item in items:
                    code = item.get("stk_cd", "").split('_')[0]
                    if not code: continue
                    qty = clean_numeric(item.get("netprps_qty", "0"))
                    if code not in self.supply_cache: self.supply_cache[code] = {'f': 0, 'i': 0}
                    self.supply_cache[code][key] = qty
        except Exception as e:
            logger.error(f"수급 캐싱 실패: {e}")