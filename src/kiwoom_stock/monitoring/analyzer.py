import statistics
import logging
from enum import Enum
from datetime import datetime
from collections import deque
from typing import List, Dict
from .collector import MarketDataCollector
from ..core.indicators import Indicators

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
    def __init__(self, client, market_config: Dict):
        self.collector = MarketDataCollector(client) # 수집기 주입
        self.trend_calc = Indicators(14)
        self.market_proxy_code = market_config.get("proxy_code", "069500")
        self.market_rsi = 50.0
        self.market_regime = MarketRegime.UNKNOWN
        self.market_atr_history = deque(maxlen=20)
        self.supply_cache: Dict[str, Dict] = {}
        self.last_supply_update = datetime.now() # [추가] 마지막 업데이트 시간 추적
        self.supply_cache[self.market_proxy_code] = self._get_default_supply()

    def _get_default_supply(self) -> Dict:
        """
        [Helper] 종목 캐시(supply_cache)의 초기 구조 및 기본값을 정의합니다.
        데이터 수집 전 엔진이 참조하더라도 에러가 발생하지 않도록 설계되었습니다.
        """
        return {
            # 1. 실시간 기술적 지표 (Indicators)
            'strength': 100.0,      # 체결강도 (100 미만 매도우위, 100 초과 매수우위)
            'vol_ratio': 0.0,       # 전일 대비 거래량 비중 (Safety Pin)
            'price': 0.0,           # 현재가 (최신 종가)
            'vwap': 0.0,            # 거래대금 가중평균가격
            'alpha_score': 0.0,     # 가격 가속도 점수
            'trend_rsi': 50.0,      # 장기 추세 RSI (중립값 50.0 설정)
            'prev_vwap': 0.0,       # 기울기 계산용 이전 값
            'vol_factor': 1.0,      # 수급 신뢰도 가중치
            'price_series': [],
            'volume_series': [],
            'trde_qty': 0,
            'cur_prc': 0,

            # 2. 프로그램 매매 데이터 (Program Trade)
            'pgm_data': {
                'net_amt': 0.0,     # 프로그램 순매수 금액
                'ratio': 0.0,       # 프로그램 참여율
                'buy_amt': 0.0,     # 프로그램 매수 금액
                'sel_amt': 0.0      # 프로그램 매도 금액
            },

            # 3. 외국계 창구 데이터 (Foreign Window)
            'foreign_data': {
                'netprps_prica': 0.0, # 외국계 순매수 금액
                'trde_prica': 1.0     # 외국계 전체 거래금액 (ZeroDivision 방어를 위해 1.0 초기화)
            }
        }

    def update_regime(self):
        """RSI와 ATR 분석을 통한 시장 성격 정의
        수집된 데이터를 바탕으로 시장 성격만 정의
        
        """
        try:
            # 1. 수집 위임
            self._update_chart_data(self.market_proxy_code, "60")
            chart_data = self.supply_cache[self.market_proxy_code]['chart_data']
            if not chart_data: return

            # 2. 데이터 정제 및 지표 계산 (I/O 없음)
            closes = [abs(float(item['cur_prc'])) for item in chart_data]
            self.market_rsi = self.trend_calc.calculate(closes)
            
            # ATR 계산 로직 (수치 계산에만 집중)
            tr_list = []
            for i in range(1, len(chart_data)):
                h = abs(float(chart_data[i]['high_pric']))
                l = abs(float(chart_data[i]['low_pric']))
                pc = abs(float(chart_data[i-1]['cur_prc']))
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
        """[Orchestrator] 모든 데이터를 카테고리별로 독립 업데이트 수행"""
        try:
            # 1. 벌크 데이터 수집 (루프 밖에서 1회 실행)
            pgm_map = self.collector.fetch_program_trade()
            frgn_map = self.collector.fetch_foreign_window_trade()

            # 2. 개별 종목별 원자적 업데이트 실행
            for code in stock_codes:
                if code not in self.supply_cache:
                    self.supply_cache[code] = self._get_default_supply()

                chart_60m = self.collector.fetch_minute_chart(code, tic="60") # 60분봉 데이터
                chart_5m = self.collector.fetch_minute_chart(code, tic="5") # 5분봉 데이터
                chart_1m = self.collector.fetch_minute_chart(code, tic="1") # 1분봉 데이터

                chart_60m.reverse()
                chart_5m.reverse()
                chart_1m.reverse()

                # 각 데이터 파트별 독립적 업데이트
                self._update_program_data(code, pgm_map)
                self._update_foreign_data(code, frgn_map)
                self._update_basic_data(code) 
                self._update_strength_data(code)
                self._update_alpha_data(code, chart_1m)
                self._update_vwap_data(code, chart_5m)
                self._update_trend_rsi(code, chart_60m)
                self._update_volatility_data(code, chart_5m)
                self._update_trend_data(code, chart_5m)

            self.last_supply_update = datetime.now()
        except Exception as e:
            logger.error(f"전체 수급 데이터 통합 중 오류: {e}")

    def _update_basic_data(self, code: str):
        """안전핀(vol_ratio) 데이터 업데이트"""
        basic = self.collector.fetch_stock_basic(code)
        self.supply_cache[code]['vol_ratio'] = basic.get('trde_pre', 0.0)
        self.supply_cache[code]['trde_qty'] = basic.get('trde_qty', 0)
        self.supply_cache[code]['cur_prc'] = basic.get('cur_prc', 0)

    def _update_strength_data(self, code: str):
        """체결강도 데이터 업데이트"""
        strength_history = self.collector.fetch_tick_strength(code)
        strength = strength_history[0].get("cntr_str", 100.0)
        self.supply_cache[code]['strength'] = strength

    def _update_chart_data(self, code: str, tic: str):
        """차트 데이터 업데이트"""
        chart = self.collector.fetch_minute_chart(code, tic)
        self.supply_cache[code]['chart_data'] = chart

    def _update_program_data(self, code: str, pgm_map: Dict):
        """
        [Atomic Update] 특정 종목의 프로그램 매매 데이터를 캐시에 반영
        :param code: 종목코드
        :param pgm_map: Collector가 반환한 프로그램 매매 맵핑 데이터
        """
        try:
            if code in pgm_map:
                # 해당 종목의 프로그램 데이터가 존재하면 캐시 업데이트
                self.supply_cache[code]['pgm_data'] = pgm_map[code]
            else:
                # 데이터가 없을 경우 이전 상태를 유지하거나 로그 기록
                # (장 시작 직후나 거래가 없는 경우 발생 가능)
                pass
        except Exception as e:
            logger.error(f"[{code}] pgm_data 캐시 구조가 초기화 실패: {e}")

    def _update_foreign_data(self, code: str, frgn_map: Dict):
        """
        [Atomic Update] 특정 종목의 외국계 창구 데이터를 캐시에 반영
        :param code: 종목코드
        :param frgn_map: Collector가 반환한 외국계 창구 맵핑 데이터
        """
        try:
            if code in frgn_map:
                # 해당 종목의 외국계 데이터가 존재하면 캐시 업데이트
                self.supply_cache[code]['foreign_data'] = frgn_map[code]
            else:
                # 외국계 창구 발생 내역이 없는 경우
                pass
        except Exception as e:
            logger.error(f"[{code}] foreign_data 캐시 구조가 초기화 실패: {e}")

    def _update_alpha_data(self, code: str, chart_1m: List[Dict]):
        """Alpha Score(가속도) 독립 업데이트"""
        try:
            if len(chart_1m) < 6: return
            
            prices = [d['cur_prc'] for d in chart_1m]
            volumes = [d['trde_qty'] for d in chart_1m]

            # vol_factor 선계산 (VWAP Score에서도 참조 가능하도록)
            avg_prev_vol = max(1.0, sum(volumes[-5:-1]) / 4)
            curr_vol = volumes[-1]
            vol_factor = min(2.0, curr_vol / avg_prev_vol)
            
            self.supply_cache[code]['price_series'] = prices
            self.supply_cache[code]['volume_series'] = volumes
            self.supply_cache[code]['vol_factor'] = vol_factor
        except Exception as e:
            logger.error(f"[{code}] 1분봉 데이터 업데이트 중 오류: {e}")

    def _update_vwap_data(self, code: str, chart_5m: List[Dict]):
        """VWAP(거래대금가중평균) 및 현재가 독립 업데이트"""
        # VWAP 연산에 필요한 5분봉 데이터 수집
        try:
            if not chart_5m: return

            prices = [abs(float(d['cur_prc'])) for d in chart_5m]
            vols = [float(d['trde_qty']) for d in chart_5m]

            # VWAP 산출: Sum(가격 * 거래량) / Sum(거래량)
            total_val = sum(p * v for p, v in zip(prices, vols))
            total_vol = sum(vols)
            vwap = total_val / total_vol if total_vol > 0 else prices[-1]

            # 캐시 반영 (현재가 포함)
            self.supply_cache[code]['prev_vwap'] = self.supply_cache[code]['vwap']
            self.supply_cache[code]['vwap'] = round(vwap, 2)
            self.supply_cache[code]['price'] = prices[-1]

            if self.supply_cache[code]['prev_vwap'] == 0:
                self.supply_cache[code]['prev_vwap'] = self.supply_cache[code]['vwap']
        except Exception as e:
            logger.error(f"[{code}] VWAP(거래대금가중평균) 업데이트 중 오류: {e}")

    def _update_trend_rsi(self, code: str, chart_60m: List[Dict]):
        """[Atomic] 60분봉 기반 장기 추세(Trend RSI) 업데이트"""
        try:
            if not chart_60m:
                return

            # 2. 데이터 정렬 (과거 -> 현재 순서) 및 가격 리스트 추출
            # Collector에서 이미 clean_numeric 가공이 완료된 상태임
            prices = [d['cur_prc'] for d in chart_60m]

            # 3. 내재화된 trend_calc(Indicators 객체)를 사용하여 RSI 계산
            # RSI 계산에 필요한 최소 데이터(period) 확보 여부 확인
            if len(prices) > self.trend_calc.period:
                rsi_val = self.trend_calc.calculate(prices)
                self.supply_cache[code]['trend_rsi'] = round(rsi_val, 2)
                
        except Exception as e:
            logger.error(f"[{code}] Trend RSI 업데이트 중 오류: {e}")

    def _update_volatility_data(self, code: str, chart_5m: List[Dict]):
        try:
            """종목별 동적 변동성(ATR%) 업데이트"""
            if not chart_5m: return

            highs = [d['high_pric'] for d in chart_5m]
            lows = [d['low_pric'] for d in chart_5m]
            closes = [d['cur_prc'] for d in chart_5m]

            # atr_percent 계산 및 저장
            atr_p = self.trend_calc.calculate_atr_percent(highs, lows, closes)
            self.supply_cache[code]['atr_percent'] = atr_p
        except Exception as e:
            logger.error(f"[{code}] 동적 변동성(ATR%) 업데이트 중 오류: {e}")

    def _update_trend_data(self, code: str, chart_5m: List[Dict]):
        # 60분봉 혹은 5분봉 데이터를 충분히 가져옴 (예: 120개)
        if len(chart_5m) < 60: return

        prices = [d['cur_prc'] for d in chart_5m]
       
        # 1. 이전 EMA 60 보존 (기울기 계산용)
        self.supply_cache[code]['prev_ema60'] = self.supply_cache[code].get('ema60', 0)

        # 2. 지표 계산기를 통해 각 평단 산출
        self.supply_cache[code]['ema5'] = self.trend_calc.calculate_ema(prices, 5)
        self.supply_cache[code]['ema20'] = self.trend_calc.calculate_ema(prices, 20)
        self.supply_cache[code]['ema60'] = self.trend_calc.calculate_ema(prices, 60)
        
        # 3. [방어] 첫 실행 시 slope 노이즈 방지
        if self.supply_cache[code]['prev_ema60'] == 0:
            self.supply_cache[code]['prev_ema60'] = self.supply_cache[code]['ema60']