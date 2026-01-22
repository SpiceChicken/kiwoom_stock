"""
올-웨더 지능형 모니터링 엔진 (완전 모듈화 버전)
모듈 구성: MarketAnalyzer, TradingStrategy, Notifier, StockManager, Engine
"""

import time
import statistics
import sys
import logging
import os
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
from typing import Dict, List, Optional
from collections import deque

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

# --- [모듈별 로깅 적용] ---
class MarketAnalyzer:
    """[Helper] 시장 환경 분석기: 레짐 진단 및 수급 캐싱 담당"""
    def __init__(self, client, trend_calc: Indicators, market_config: Dict):
        self.client = client
        self.trend_calc = trend_calc
        self.market_proxy_code = market_config.get("proxy_code", "069500")
        self.market_rsi = 50.0
        self.market_regime = "Unknown"
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
                self.market_regime = "안정적 강세장" if not is_volatile else "변동성 강세장"
            elif self.market_rsi <= 40:
                self.market_regime = "조용한 하락장" if not is_volatile else "패닉 하락장"
            else:
                self.market_regime = "평온 구간"

            if prev_regime != self.market_regime:
                logger.info(f"Market Regime Changed: {prev_regime} -> {self.market_regime}")
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

        self.exit_time = strategy_config.get("day_trade_exit_time", "15:30")
        self.decay_rate = strategy_config.get("score_decay_rate", 0.15)

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

    def monitor_active_signals(self, stock_code, current_price, current_score):
        """보유 종목의 매도 조건을 감시하고 DB에 기록합니다."""
        if stock_code not in self.active_positions: return

        pos = self.active_positions[stock_code]
        profit = round((current_price / pos['buy_price'] - 1) * 100, 2)
        
        # 1. 당일 종가 매도 로직 (Time-based Exit)
        now_time = datetime.now().strftime("%H:%M")
        
        if now_time >= self.exit_time:
            self.db.record_sell(pos['id'], current_price, profit, "Day Trade Close")
            print(f"🕒 [종가 매도] {pos['stock_name']} | 수익률: {profit:+.2f}% | 사유: 장 마감 강제 청산")
            del self.active_positions[stock_code]
            return

        # 2. 기존 점수 하락 매도 (Score Decay)
        sell_threshold = pos['buy_score'] * (1 - self.decay_rate)
        
        # 2-2. 상대적 점수 이탈 시 매도 실행
        if current_score < sell_threshold:
            self.db.record_sell(pos['id'], current_price, profit, "Relative Score Decay")
            print(f"📉 [매도 실행] {pos['stock_name']} | 수익률: {profit:+.2f}% | "
                f"사유: 점수 {self.decay_rate*100:.0f}% 이탈 (기준: {sell_threshold:.1f})")
            del self.active_positions[stock_code]
            return

    def is_monitoring_time(self) -> bool:
        """장 운영 시간 체크 (에러 수정 버전)"""
        now = datetime.now()
        if now.weekday() >= 5: return False
        
        now_str = now.strftime("%H:%M")
        # 시작 시간(09:00 권장)과 종료 시간(exit_time) 사이인지 문자열로 안전하게 비교
        return "08:30" <= now_str <= self.exit_time

class TradingStrategy:
    """[Strategy] 트레이딩 전략 및 점수 산출: 하드코딩된 가중치/임계값 제거"""
    def __init__(self, strategy_config: Dict):
        self.settings = strategy_config
        self.momentum_threshold = strategy_config.get("momentum_threshold", 10.0)

    def _get_regime_config(self, regime: str) -> Dict:
        regimes = self.settings.get("regimes", {})
        return regimes.get(regime, regimes.get("default", {}))

    def get_scoring_weights(self, regime: str) -> Dict[str, float]:
        return self._get_regime_config(regime).get("weights", {})

    def get_dynamic_thresholds(self, regime: str) -> Dict[str, float]:
        return self._get_regime_config(regime).get("thresholds", {})

    def get_min_thresholds(self, regime: str) -> Dict[str, float]:
        """레짐별 4대 지표 하한선 로드"""
        return self._get_regime_config(regime).get("min_thresholds", {})

    def calculate_conviction_score(self, metrics: Dict, regime: str):
        """총점과 상세 지표 점수를 함께 반환합니다."""
        w = self._get_regime_config(regime).get("weights", {"alpha":0.25, "supply":0.25, "vwap":0.25, "trend":0.25})
        
        # 1. Alpha 점수: 민감도 하향 (2.5 -> 1.5) 
        # 시장 지수 대비 초과 수익률이 더 높아야 고득점이 가능하도록 변경
        alpha_raw = max(0, min(100, 50 + (metrics['alpha'] * 1.5)))
        
        # 2. Supply 점수: 기존 로직 유지 (고정)
        total_vol = max(1, metrics.get('volume', 1))
        s_ratio = (metrics.get('net_buy', 0) / total_vol) * 100
        synergy = 20 if (metrics['f_buy'] > 0 and metrics['i_buy'] > 0) else (-20 if (metrics['f_buy'] < 0 and metrics['i_buy'] < 0) else 0)
        supply_raw = max(0, min(100, (s_ratio * 50) + synergy))
        
        # 3. VWAP 점수: 민감도 대폭 하향 (25 -> 10) 
        # 1월 22일 모든 종목이 100점을 기록했던 현상을 방어하기 위해,
        # 가격 이격도가 5% 이상일 때만 100점에 도달하도록 수정 (기존은 2%에서 100점)
        dev = (metrics['price'] / metrics['vwap'] - 1) * 100 if metrics['vwap'] > 0 else 0
        vwap_raw = max(0, min(100, 50 + (dev * 10)))
        
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
        self.strategy = TradingStrategy(config['strategy'])
        self.stock_mgr = StockManager(client, TradeLogger(), config.get("filters", {}), config['strategy'])
        self.notifier = Notifier(self.stock_mgr.stock_names)
        
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

            score, score_details = self.strategy.calculate_conviction_score(metrics, self.analyzer.market_regime)
            momentum = round(score - self.score_history.get(stock_code, score), 1)
            self.score_history[stock_code] = score
            
            th = self.strategy.get_dynamic_thresholds(self.analyzer.market_regime)
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

    def run(self):
        """메인 실행 루프"""
        logger.info("Starting Monitoring Loop...")
        while True:
            try:
                self.stock_mgr.update_target_stocks()
                # if not self.stock_mgr.is_monitoring_time():
                #     logger.info("Market is closed. Shutting down system.")
                #     break

                self.analyzer.update_regime()
                self.analyzer.fetch_supply_data()

                # 현재 시간 확인
                now_str = datetime.now().strftime('%H:%M')
                entry_deadline = self.config.get("strategy", {}).get("entry_deadline", "14:30")
                is_entry_allowed = now_str < entry_deadline
                
                for stock in self.stock_mgr.stocks: self.check_conditions(stock)
                
                sorted_stocks = sorted(self.stock_mgr.stocks, key=lambda x: self.status_log.get(x, {}).get('momentum', 0), reverse=True)
                print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {self.analyzer.market_regime}")
                print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
                print("-" * 55)

                for stock in sorted_stocks:
                    res = self.check_conditions(stock)
                    log = self.status_log.get(stock)
                    if not log or "price" not in log: continue

                    # 보유 종목 매도 감시 위임
                    self.stock_mgr.monitor_active_signals(stock, log['price'], log['score'])
                    
                    # 화면 출력 및 알림
                    name = self.stock_mgr.stock_names.get(stock, stock)
                    m_str = f"+{log['momentum']}" if log['momentum'] > 0 else f"{log['momentum']}"
                    print(f"{name:<10} | {log['score']:>5.1f} | {m_str:>6} | {log['reason']:<10}")
                    
                    if res:
                        th = self.strategy.get_dynamic_thresholds(self.analyzer.market_regime)
            
                        # [비즈니스 로직] 4개 지표 하한선 필터 (교집합 필터)
                        min_th = self.strategy.get_min_thresholds(self.analyzer.market_regime)
                        
                        is_qualified = (
                            res['alpha_score'] >= min_th['alpha'] and
                            res['supply_score'] >= min_th['supply'] and
                            res['vwap_score'] >= min_th['vwap'] and
                            res['trend_score'] >= min_th['trend']
                        )
                        
                        # [최종 진입 조건] 총점 통과 + 지표 하한선 통과 + 진입 가능 시간 내
                        if res['score'] >= th['strong'] and is_qualified and is_entry_allowed:
                            if stock not in self.stock_mgr.active_positions:
                                # 매수 실행 및 DB 기록
                                buy_data = {
                                    "stock_code": stock, "stock_name": self.stock_mgr.stock_names.get(stock, stock),
                                    "buy_price": log['price'], "buy_score": log['score'],
                                    "alpha_score": res['alpha_score'], "supply_score": res['supply_score'],
                                    "vwap_score": res['vwap_score'], "trend_score": res['trend_score'],
                                    "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                    "buy_regime": self.analyzer.market_regime
                                }
                                buy_data['id'] = self.db.record_buy(buy_data)
                                self.stock_mgr.active_positions[stock] = buy_data
                                self.notifier.send_buy_alert(res)

                        if log['momentum'] >= self.strategy.momentum_threshold:
                            self.notifier.send_momentum_alert(res)

                time.sleep(self.config.get("check_interval", 60))
            except KeyboardInterrupt:
                logger.warning("System interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                time.sleep(10) # 치명적 에러 시 잠시 대기 후 재시도
        
        sys.exit(0)