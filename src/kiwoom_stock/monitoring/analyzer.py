import statistics
import logging
from enum import Enum
from datetime import datetime
from collections import deque
from typing import List, Dict
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

    def update_priority_supply(self, stock_codes: List[str]):
        """
        매수 후보 및 보유 종목에 대해 실시간 지표를 정밀 업데이트합니다.
        체결강도(Base)와 외국계 창구(Bonus) 데이터를 확보합니다.
        """

        # 1. API로부터 리스트형 데이터 수집
        program_trade_list = self.client.market.get_program_trade()
        foreign_window_list = self.client.market.get_foreign_window_total()

        # 2. 검색 최적화를 위해 {코드: 값} 형태의 딕셔너리로 사전 변환 (O(M))
        pgm_map = {
            item['stk_cd']: {
                "netprps_prica": clean_numeric(item.get("netprps_prica", "0")),
                "all_trde_rt": clean_numeric(item.get("all_trde_rt", "0")),
                "buy_cntr_amt": clean_numeric(item.get("buy_cntr_amt", "0")),
                "sel_cntr_amt": clean_numeric(item.get("sel_cntr_amt", "0"))
            }
            for item in program_trade_list if item.get("stk_cd")
        }

        foreign_map = {
            item['stk_cd']: {
                "netprps_prica": clean_numeric(item.get("netprps_prica", "0")),
                "trde_prica": clean_numeric(item.get("trde_prica", "1")), # 분모(0) 방지
                "net_qty": clean_numeric(item.get("netprps_trde_qty", "0"))
            }
            for item in foreign_window_list if item.get("stk_cd")
        }

        for code in stock_codes:
            try:
                if code not in self.supply_cache:
                    self.supply_cache[code] = self._get_default_supply()

                # 실시간 지표 수집 (체결강도 및 거래량 비율)
                # ka10001(주식기본정보) 등에서 전일대비거래량비율(vol_rt_pre_day) 확보
                basic_info = self.client.market.get_stock_basic_info(code)

                # 1. 체결강도 (Base)
                self.supply_cache[code]['strength'] = self.client.market.get_tick_strength(code)
                self.supply_cache[code]['vol_ratio'] = clean_numeric(basic_info.get('trde_pre', 0.0))

                # 2. 프로그램 매매
                self.supply_cache[code]['pgm_data'] = pgm_map.get(code, {
                    "netprps_prica": 0.0, "all_trde_rt": 0.0, "buy_amt": 0.0, "sel_amt": 0.0
                })
                
                # 3. 외국계 창구 합계
                self.supply_cache[code]['foreign_data'] = foreign_map.get(code, {
                    "netprps_prica": 0.0, "trde_prica": 1.0, "net_qty": 0.0
                })
                
                logger.debug(f"[{code}] 정밀 수급 업데이트 완료")
            except Exception as e:
                logger.error(f"[{code}] 우선순위 수급 업데이트 실패: {e}")

    def _get_default_supply(self) -> Dict:
        """수급 데이터 초기값 정의"""
        return {
            'strength': 100.0,    # 체결강도 중립,
            'vol_ratio': 0.0,   # 전일 대비 거래량 비율 (%)
            'pgm_data': {
                "netprps_prica": 0.0, "all_trde_rt": 0.0, "buy_amt": 0.0, "sel_amt": 0.0
                },  # 프로그램 순매수
            'foreign_data': {
                "netprps_prica": 0.0, "trde_prica": 1.0, "net_qty": 0.0
                },  # 외국계 창구 순매수
        }