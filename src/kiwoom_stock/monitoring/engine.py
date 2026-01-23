"""
올-웨더 지능형 모니터링 엔진 (완전 모듈화 버전)
모듈 구성: MarketAnalyzer, TradingStrategy, Notifier, StockManager, Engine
"""

import statistics
import sys
import logging
import os
from logging.handlers import TimedRotatingFileHandler
import time as time_mod  # time.sleep() 등에 사용
from datetime import datetime, time, timedelta  # datetime.time 객체로 사용
from typing import Dict, List, Optional
from collections import deque
from enum import Enum

from ..api.parser import clean_numeric
from ..core.indicators import Indicators
from kiwoom_stock.core.database import TradeLogger

# --- [로깅 시스템 고도화 설정] ---

# 에러 로그를 제외하기 위한 필터 클래스 정의
class ExcludeErrorFilter(logging.Filter):
    def filter(self, record):
        # ERROR(40) 레벨보다 낮은 로그(DEBUG, INFO, WARNING)만 허용합니다.
        return record.levelno < logging.ERROR
        
def setup_structured_logging():
    """로그 폴더 생성 및 핸들러 설정 (에러 분리 필터 적용)"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)

    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)

    # 콘솔 핸들러 (기존 유지)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))

    # 2. trading.log 핸들러 설정 (필터 적용)
    file_format = logging.Formatter(
        '{"timestamp": "%(asctime)s", "level": "%(levelname)s", "module": "%(module)s", "message": "%(message)s"}'
    )
    file_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/trading.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8"
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(file_format)
    
    # [핵심] 필터를 추가하여 ERROR 이상의 로그가 trading.log에 기록되는 것을 방지합니다.
    file_handler.addFilter(ExcludeErrorFilter())

    # 3. error.log 핸들러 (에러만 수집 - 기존 유지)
    error_handler = TimedRotatingFileHandler(
        filename=f"{log_dir}/error.log",
        when="D",
        interval=1,
        backupCount=90,
        encoding="utf-8"
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(file_format)

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(error_handler)

setup_structured_logging()
logger = logging.getLogger(__name__)

class MarketRegime(Enum):
    STABLE_BULL = "안정적 강세장"
    VOLATILE_BULL = "변동성 강세장"
    QUIET_BEAR = "조용한 하락장"
    PANIC_BEAR = "패닉 하락장"
    NEUTRAL = "평온 구간"
    UNKNOWN = "Unknown"

# --- [모듈별 로깅 적용] ---
class MarketAnalyzer:
    """[Helper] 시장 환경 분석기: 레짐 진단 및 수급 캐싱 담당"""
    def __init__(self, client, trend_calc: Indicators, market_config: Dict):
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

class StockManager:
    """[Helper] 종목 및 인벤토리 관리자: 감시 종목 및 보유 종목 상태 관리"""
    def __init__(self, client, db: TradeLogger, filter_config: Dict, strategy_config: Dict):
        self.client = client
        self.db = db
        self.etf_keywords = tuple(filter_config.get("etf_keywords", []))
        self.max_stocks = filter_config.get("max_stocks", 40)
        
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}
        self.active_positions = self.db.load_open_positions()

        # [최적화] 문자열을 time 객체로 미리 변환 (루프 내 오버헤드 제거)
        exit_str = strategy_config.get("day_trade_exit_time", "15:30")
        self.exit_time_obj = time.fromisoformat(exit_str)
        # [수정] 장 마감 3분 전 강제 청산 시간 계산 (오버헤드 방지를 위해 미리 계산)
        # datetime.combine을 사용하여 안전하게 시간 연산 수행
        dummy_dt = datetime.combine(datetime.today(), self.exit_time_obj)
        self.forced_exit_time = (dummy_dt - timedelta(minutes=3)).time()
        
        # [신규] 익절/손절/감쇠 설정 로드
        self.decay_rate = strategy_config.get("score_decay_rate", 0.15)
        self.target_profit_rate = strategy_config.get("target_profit_rate", 0.025) # 기본 2.5%
        self.stop_loss_rate = strategy_config.get("stop_loss_rate", -0.015)

    def get_exit_reason(self, pos: Dict, current_price: float, current_score: float, strong_threshold: float) -> Optional[str]:
        """
        설정된 익절/손절/시간/점수 조건을 검사하여 매도 사유를 반환합니다.
   
        """
        # 현재 수익률 계산 (소수점 단위)
        profit_rate = (current_price / pos['buy_price'] - 1)
        
        # 1. 시간 기반 당일 청산 (장 마감 3분 전부터 최우선 수행)
        if datetime.now().time() >= self.forced_exit_time:
            return "Day Trade Close (3m Early)"
            
        # 2. 하드 손절 (Stop Loss) - 설정값 이하로 하락 시 즉시 매도
        if profit_rate <= self.stop_loss_rate:
            return f"Stop Loss ({profit_rate*100:.1f}%)"
            
        # 3. 지능형 익절 (Take Profit)
        # 수익률이 목표치 이상이지만, 점수가 여전히 강하면(strong_threshold 이상) 매도를 미룹니다.
        if profit_rate >= self.target_profit_rate:
            if current_score >= strong_threshold:
                return None # 기세가 좋으므로 익절 보류 (Let the winner run)
            return f"Take Profit (+{profit_rate*100:.1f}%)"

        # 4. 상대적 점수 하락 (Score Decay)
        sell_threshold = pos['buy_score'] * (1 - self.decay_rate)
        if current_score < sell_threshold:
            return f"Score Decay (-{self.decay_rate*100:.0f}%)"

        return None

    def update_target_stocks(self):
        """보유 종목을 최우선으로 포함하여 감시 리스트를 갱신합니다."""
        try:
            new_stocks = list(self.active_positions.keys())
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            
            for item in upper_list:
                code, name = item['stk_cd'], item['stk_nm']
                if any(kw in name for kw in self.etf_keywords): continue
                if code not in new_stocks: new_stocks.append(code)
                self.stock_names[code] = name
            
            self.stocks = new_stocks[:self.max_stocks]
            logger.info(f"감시 종목 갱신 (총 {len(self.stocks)}개 | 보유: {len(self.active_positions)}개)")
        except Exception as e:
            logger.error(f"종목 갱신 실패: {e}")

    def monitor_active_signals(self, stock_code, current_price, current_score, strong_threshold):
        """보유 종목의 매도 조건을 감시하고 DB에 기록합니다."""
        if stock_code not in self.active_positions:
            return

        pos = self.active_positions[stock_code]
        
        # [추상화 호출] 판정은 평가기에게 맡깁니다.
        reason = self.get_exit_reason(pos, current_price, current_score, strong_threshold)
        
        if reason:
            profit = round((current_price / pos['buy_price'] - 1) * 100, 2)
            # 매도 기록 및 포지션 제거
            self.db.record_sell(pos['id'], current_price, profit, reason)
            print(f"📉 [매도 실행] {pos['stock_name']} | 수익률: {profit:+.2f}% | 사유: {reason}")
            del self.active_positions[stock_code]

    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (에러 수정 버전)"""
        now = datetime.now()
        if now.weekday() >= 5: return False
        
        # 시작 시간(09:00 권장)과 종료 시간(exit_time) 사이인지 문자열로 안전하게 비교
        return time(8, 30) <= now.time() <= self.exit_time_obj

class TradingStrategy:
    """[Strategy] 트레이딩 전략 및 점수 산출: 하드코딩된 가중치/임계값 제거"""
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)

        # 캐싱을 위한 내부 상태 변수
        self._current_regime = MarketRegime.UNKNOWN
        self._cached_config = {}

    def update_context(self, regime: MarketRegime):
        """
        레짐이 변경될 때만 호출하여 관련 설정을 내부 메모리에 캐싱합니다.
       
        """
        if self._current_regime == regime and self._cached_config:
            return # 변경 사항이 없으면 유지

        self._current_regime = regime
        regimes = self.settings.get("regimes", {})
        # 해당 레짐 설정 로드, 없으면 default 로드
        self._cached_config = regimes.get(regime.value, regimes.get("default", {}))
        logger.info(f"Strategy context updated to: {regime.value}")

    @property
    def weights(self) -> Dict[str, float]:
        """현재 레짐의 가중치를 반환합니다. (누락 시 균등 가중치)"""
        return self._cached_config.get("weights", {
            "alpha": 0.25, "supply": 0.25, "vwap": 0.25, "trend": 0.25
        })

    @property
    def entry_thresholds(self) -> Dict[str, float]:
        """현재 레짐의 진입 임계값을 반환합니다. (누락 시 보수적 기준)"""
        return self._cached_config.get("thresholds", {
            "strong": 85.0, "interest": 75.0, "alert": 70.0
        })

    @property
    def min_thresholds(self) -> Dict[str, float]:
        """
        현재 레짐의 개별 지표 하한선을 반환합니다.
        레짐별 설정 -> 공통 루트 설정 순으로 참조합니다.
        """
        return self._cached_config.get("min_thresholds", self.settings.get("min_thresholds", {}))

    def calculate_conviction_score(self, metrics: Dict):
        """총점과 상세 지표 점수를 함께 반환합니다."""
        w = self.weights
        
        # 1. Alpha 점수: 민감도 하향 (2.5 -> 1.5) 
        # 시장 지수 대비 초과 수익률이 더 높아야 고득점이 가능하도록 변경
        alpha_raw = max(0, min(100, 50 + (metrics['alpha'] * 1.5)))
        
        # 2. Supply 점수: 기존 로직 유지 (고정)
        total_vol = max(1, metrics.get('volume', 1))
        s_ratio = (metrics.get('net_buy', 0) / total_vol) * 100
        synergy = 20 if (metrics['f_buy'] > 0 and metrics['i_buy'] > 0) else (-20 if (metrics['f_buy'] < 0 and metrics['i_buy'] < 0) else 0)
        supply_raw = max(0, min(100, (s_ratio * 50) + synergy))
        
        # 3. VWAP 점수: 민감도 추가 하향 (10 -> 8)
        # 이제 가격 이격도가 약 6.25% 이상일 때만 100점에 도달합니다. (기존 5%)
        dev = (metrics['price'] / metrics['vwap'] - 1) * 100 if metrics['vwap'] > 0 else 0
        vwap_raw = max(0, min(100, 50 + (dev * 8)))
        
        # 4. Trend 점수: 민감도 하향 (2.5 -> 1.5) 
        # 단순히 RSI가 70을 넘는 것만으로는 부족하며, 80 이상의 강력한 과매수 구간에 
        # 진입해야 100점에 근접하도록 문턱을 높임
        t_rsi = metrics['trend_rsi']
        trend_raw = max(0, min(100, 50 + ((t_rsi - 50) * 1.5) if t_rsi >= 50 else t_rsi))
        
        # 최종 가중치 합산
        total_score = round(
            (alpha_raw * w.get('alpha', 0.25)) + 
            (supply_raw * w.get('supply', 0.25)) + 
            (vwap_raw * w.get('vwap', 0.25)) + 
            (trend_raw * w.get('trend', 0.25)), 1
        )
        
        # 상세 점수 딕셔너리 생성
        details = {
            "alpha": round(alpha_raw, 1),
            "supply": round(supply_raw, 1),
            "vwap": round(vwap_raw, 1),
            "trend": round(trend_raw, 1)
        }
        
        return total_score, details

class Notifier:
    """[Helper] 알림 송신 서비스"""
    def __init__(self, stock_names: Dict[str, str]):
        self.stock_names = stock_names

    def send_buy_alert(self, res: Dict):
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"\n🔥 [강력 추천] {name}({res['stock_code']}) 매수 타점 포착! (점수: {res['score']})")
        print(f"  - 종합 점수: {res['score']}점 | 현재가: {res['price']:,.0f}원")
        print(f"  - 상대 강도(Alpha): {res.get('alpha', 0):+.1f} | 수급: {res['net_buy']:,}주")
        print(f"{'='*55}")

    def send_momentum_alert(self, res: Dict):
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"🚀 [수급 폭발] {name}({res['stock_code']}) 점수 급상승! ({res['momentum']:+})")

class MultiTimeframeRSIMonitor:
    """[Engine] 최종 컨트롤러: 각 모듈을 조율하여 시스템 실행"""
    def __init__(self, client, config: Dict):
        self.client = client
        self.config = config

        market_config = config.get("market", {})
        filter_config = config.get("filters", {})
        strategy_config = config.get("strategy", {})

        # 지표 계산기 초기화
        self.trend_calc = Indicators(period=config.get("trend_timeframe", {}).get("rsi_period", 14))
        self.entry_calc = Indicators(period=config.get("entry_timeframe", {}).get("rsi_period", 9))
        
        # 모듈 초기화 (Config 분배)        
        self.analyzer = MarketAnalyzer(client, self.trend_calc, market_config)
        self.db = TradeLogger()
        self.strategy = TradingStrategy(strategy_config)
        self.stock_mgr = StockManager(client, TradeLogger(), filter_config, strategy_config)
        self.notifier = Notifier(self.stock_mgr.stock_names)

        # [최적화] 진입 마감 시간을 time 객체로 캐싱
        entry_str = config['strategy'].get("entry_deadline", "14:30")
        self.entry_deadline_obj = time.fromisoformat(entry_str)
        
        self.status_log = {}
        self.score_history = {}

        logger.info("Monitoring Engine Initialized.")

    def check_conditions(self, stock_code: str) -> Optional[Dict]:
        """종목 스캔 및 전략 실행"""
        try:
            entry_data = self.client.market.get_minute_chart(stock_code, tic="5")
            trend_data = self.client.market.get_minute_chart(stock_code, tic="60")
            if not trend_data or len(entry_data) < 20: return None

            curr_price = entry_data[0]['close']
            curr_vol = sum(d['volume'] for d in entry_data)
            s_data = self.analyzer.supply_cache.get(stock_code, {'f': 0, 'i': 0})
            
            metrics = {
                "alpha": self.entry_calc.calculate([d['close'] for d in entry_data]) - self.analyzer.market_rsi,
                "net_buy": s_data['f'] + s_data['i'], "f_buy": s_data['f'], "i_buy": s_data['i'],
                "price": curr_price, "volume": curr_vol,
                "vwap": sum(d['close']*d['volume'] for d in entry_data)/curr_vol if curr_vol > 0 else curr_price,
                "trend_rsi": self.trend_calc.calculate([d['close'] for d in trend_data])
            }

            score, score_details = self.strategy.calculate_conviction_score(metrics)
            momentum = round(score - self.score_history.get(stock_code, score), 1)
            self.score_history[stock_code] = score
            
            th = self.strategy.entry_thresholds
            status = "🔥강력추천" if score >= th['strong'] else ("👀관심" if score >= th['interest'] else "관망")
            if momentum >= self.strategy.momentum_threshold: status = "🚀수급폭발"

            self.status_log[stock_code] = {"price": curr_price, "score": score, "momentum": momentum, "reason": status}
            return {
                **metrics, 
                **{f"{k}_score": v for k, v in score_details.items()}, # alpha_score 등 추가
                "stock_code": stock_code, 
                "score": score, 
                "momentum": momentum
            } if score >= th['alert'] else None
        except Exception as e:
            logger.error(f"Condition check failed for {stock_code}: {e}", exc_info=True)
            return None

    def evaluate_entry_signal(self, stock_code, res, thresholds, min_th, current_time: time) -> bool:
        """
        시간 제한을 포함한 모든 진입 조건을 한곳에서 판정합니다.
    
        """
        # 1. 시간 제한 체크 (내부로 이동)
        is_time_allowed = current_time < self.entry_deadline_obj
        
        # 2. 4대 지표 하한선(Conjunction) 체크
        # .get()을 활용해 설정 누락 방지 및 가독성 확보
        is_qualified = all([
            res['alpha_score'] >= min_th.get('alpha', 0),
            res['supply_score'] >= min_th.get('supply', 0),
            res['vwap_score'] >= min_th.get('vwap', 0),
            res['trend_score'] >= min_th.get('trend', 0)
        ])

        # 3. 최종 진입 조건 리스트 (Pythonic all 활용)
        entry_conditions = [
            is_time_allowed,                                     # 장 후반 진입 금지
            stock_code not in self.stock_mgr.active_positions,   # 중복 진입 방지
            res['score'] >= thresholds.get('strong', 80.0),      # 총점 임계값 통과
            is_qualified                                         # 개별 지표 하한선 통과
        ]
        
        return all(entry_conditions)

    def run(self):
        """메인 실행 루프"""
        logger.info("Starting Monitoring Loop...")
        while True:
            try:
                self.stock_mgr.update_target_stocks()
                if not self.stock_mgr.is_monitoring_time():
                    logger.info("Market is closed. Shutting down system.")
                    break

                # 1. 시장 상황 파악
                self.analyzer.update_regime()

                # 2. 파악된 레짐으로 전략 컨텍스트 동기화 (여기서 딱 한 번만 캐싱 수행)
                self.strategy.update_context(self.analyzer.market_regime)

                # 3. 외인/기관 수급 상황 파악
                self.analyzer.fetch_supply_data()
                
                for stock in self.stock_mgr.stocks: self.check_conditions(stock)
                
                sorted_stocks = sorted(self.stock_mgr.stocks, key=lambda x: self.status_log.get(x, {}).get('momentum', 0), reverse=True)
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {self.analyzer.market_regime.value}")
                print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
                print("-" * 55)

                for stock in sorted_stocks:
                    res = self.check_conditions(stock)
                    log = self.status_log.get(stock)
                    if not log or "price" not in log: continue

                    # 보유 종목 매도 감시 위임
                    strong_thresholds = self.strategy.entry_thresholds.get('strong', 85.0)
                    self.stock_mgr.monitor_active_signals(stock, log['price'], log['score'], strong_thresholds)
                    
                    # 화면 출력 및 알림
                    name = self.stock_mgr.stock_names.get(stock, stock)
                    m_str = f"+{log['momentum']}" if log['momentum'] > 0 else f"{log['momentum']}"
                    print(f"{name:<10} | {log['score']:>5.1f} | {m_str:>6} | {log['reason']:<10}")
                    
                    if res:                        
                        # [추상화 적용] 진입 판정 호출
                        # [Pythonic] 메서드 괄호()와 인자 전달이 사라져 가독성이 극대화됨
                        if self.evaluate_entry_signal(
                            stock, res, 
                            self.strategy.entry_thresholds, # Property 접근
                            self.strategy.min_thresholds,   # Property 접근
                            datetime.now().time()
                        ):
                            if stock not in self.stock_mgr.active_positions:
                                # 매수 실행 및 DB 기록
                                buy_data = {
                                    "stock_code": stock, "stock_name": self.stock_mgr.stock_names.get(stock, stock),
                                    "buy_price": log['price'], "buy_score": log['score'],
                                    "alpha_score": res['alpha_score'], "supply_score": res['supply_score'],
                                    "vwap_score": res['vwap_score'], "trend_score": res['trend_score'],
                                    "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    "buy_regime": self.analyzer.market_regime.value
                                }
                                buy_data['id'] = self.db.record_buy(buy_data)
                                self.stock_mgr.active_positions[stock] = buy_data
                                self.notifier.send_buy_alert(res)

                        if log['momentum'] >= self.strategy.momentum_threshold:
                            self.notifier.send_momentum_alert(res)

                time_mod.sleep(self.config.get("check_interval", 60))
            except KeyboardInterrupt:
                logger.warning("System interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                time_mod.sleep(10) # 치명적 에러 시 잠시 대기 후 재시도
        
        sys.exit(0)