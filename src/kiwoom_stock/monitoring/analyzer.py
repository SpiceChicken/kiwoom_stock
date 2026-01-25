import statistics
import logging
from enum import Enum
from datetime import datetime
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
        self.last_supply_update = datetime.now() # [추가] 마지막 업데이트 시간 추적

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
        """
        외인/기관 수급 데이터를 분리하여 캐싱합니다. 
        [개선] 실패 시 기존 데이터를 유지하고 성공 시에만 부분 업데이트(Atomic Update)합니다.
        """
        
        success_count = 0
        for invsr, key in [("6", "f"), ("7", "i")]:
            try:
                items = self.client.market.get_investor_supply(market_tp="001", investor_tp=invsr)
                
                # 데이터가 정상 수신된 경우에만 업데이트 프로세스 진행
                if items and len(items) > 0:
                    for item in items:
                        code = item.get("stk_cd", "").split('_')[0]
                        if not code: continue
                        
                        qty = clean_numeric(item.get("netprps_qty", "0"))
                        
                        # 원자적 업데이트: 해당 종목-주체 데이터만 교체
                        if code not in self.supply_cache:
                            self.supply_cache[code] = {'f': 0, 'i': 0}
                        self.supply_cache[code][key] = qty
                    success_count += 1
                else:
                    logger.warning(f"수급 데이터({key}) 수신 결과가 비어있습니다. 이전 캐시를 유지합니다.")
                    
            except Exception as e:
                # 에러 발생 시에도 self.supply_cache는 이전 루프의 상태를 유지함 (안전)
                logger.error(f"수급 캐싱 중 예외 발생 (investor_tp={invsr}): {e}")

        # 업데이트 시간 기록 및 신선도 체크
        if success_count > 0:
            self.last_supply_update = datetime.now()
        
        # [추가] 10분(600초) 이상 업데이트 실패 시 치명적 경고
        if (datetime.now() - self.last_supply_update).total_seconds() > 600:
            logger.critical("🚨 수급 데이터가 10분 이상 동결되었습니다. 키움 API 연결 상태를 확인하십시오.")