"""
올-웨더 지능형 모니터링 엔진 (완전 모듈화 버전)
모듈 구성: MarketAnalyzer, TradingStrategy, Notifier, StockManager, Engine
"""

import sys
import logging
import time as time_mod
from datetime import datetime, time
from typing import Dict, Optional

from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager, Position
from .notifier import Notifier
from kiwoom_stock.core.database import TradeLogger
from ..core.indicators import Indicators

# utils에서 설정한 핸들러를 상속받기 위해 로거 선언
logger = logging.getLogger(__name__)

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
        self.db = TradeLogger()
        self.analyzer = MarketAnalyzer(client, self.trend_calc, market_config)
        self.strategy = TradingStrategy(strategy_config)
        self.stock_mgr = StockManager(client, TradeLogger(), filter_config, strategy_config)
        self.notifier = Notifier(self.stock_mgr.stock_names, config)

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
                # [안전장치] 시장 마감 확인 및 가동 중단
                if not self.stock_mgr.is_monitoring_time():
                    logger.info("Market is closed. Shutting down system.")
                    break
                
                # 1. 감시 대상 종목 갱신
                # 거래대금 상위 종목 및 보유 종목을 합쳐 이번 루프에서 감시할 실시간 리스트를 생성합니다.
                self.stock_mgr.update_target_stocks() 

                # 2. 시장 레짐(Regime) 분석
                # 시장 지수(KODEX 200 등)의 RSI와 ATR을 계산하여 현재가 강세장인지, 패닉 하락장인지 진단합니다.
                # 이 진단 결과에 따라 시스템 전체의 공격성과 방어 모드가 결정됩니다.
                self.analyzer.update_regime()

                # 3. 전략 컨텍스트 동기화 및 캐싱
                # 분석된 시장 레짐에 맞춰 지표별 가중치(Weights)와 진입/청산 임계값(Thresholds)을 동적으로 변경합니다.
                # 루프 내 중복 연산을 막기 위해 레짐이 바뀔 때만 딱 한 번 설정을 로드하여 성능을 최적화합니다.
                self.strategy.update_context(self.analyzer.market_regime)

                # 4. 외인/기관 수급 데이터 일괄 확보 (Batch Fetch)
                # 현재 감시 중인 모든 종목에 대한 투자자별 매매동향 데이터를 한 번에 가져와 내부 캐시에 저장합니다.
                # 이후 개별 종목 점수 계산 시 매번 API를 호출하지 않고 이 캐시를 참조하여 실행 속도를 2배 이상 높입니다.
                self.analyzer.fetch_supply_data()
                
                # [최적화] API 호출 중복 제거: 한 번의 루프에서 모든 데이터 수집 및 결과 저장
                scan_results = {}
                for stock in self.stock_mgr.stocks:
                    res = self.check_conditions(stock)
                    if res: scan_results[stock] = res
                
                # 킬스위치 작동
                if self.stock_mgr.check_kill_switch(self.status_log):
                    kill_switch_text = "블랙 스완 대응: 전 종목 시장가 매도 및 시스템 긴급 셧다운"
                    logger.critical(kill_switch_text)
                    
                    for code in list(self.stock_mgr.active_positions.keys()):
                        pos = self.stock_mgr.active_positions[code]
                        log = self.status_log.get(code)

                        pos.sell_price = log['price'] if log else pos.buy_price
                        pos.sell_reason = "KILL-SWITCH ACTIVATED"
                        
                        # [개선] 판정 로직을 거치지 않고 직접 DB 기록 및 포지션 삭제
                        self.db.record_sell(pos)
                        self.notifier.notify_sell(pos)

                    self.notifier.notify_critical(kill_switch_text)
                        
                    break # 메인 루프 탈출
                
                # 모멘텀 기준 정렬 (status_log 참조)
                sorted_stocks = sorted(self.stock_mgr.stocks, 
                                       key=lambda x: self.status_log.get(x, {}).get('momentum', 0), 
                                       reverse=True)

                self.notifier.start_status_session()

                for stock in sorted_stocks:
                    res = scan_results.get(stock)
                    log = self.status_log.get(stock)
                    if not log or "price" not in log: continue

                    # 보유 종목 매도 감시 위임
                    strong_thresholds = self.strategy.entry_thresholds.get('strong', 85.0)
                    self.stock_mgr.monitor_active_signals(stock, log, strong_thresholds, self.notifier)
                    
                    # 화면 출력 및 알림

                    # log 딕셔너리에 분석에 필요한 종목명(name)을 추가
                    log['name'] = self.stock_mgr.stock_names.get(stock, stock)
                    self.notifier.collect_status(log)
                    
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
                                self.stock_mgr.active_positions[stock] = Position(**buy_data)
                                self.notifier.notify_buy(buy_data)

                        if log['momentum'] >= self.strategy.momentum_threshold:
                            self.notifier.notify_momentum(res)

                # 모든 종목 처리가 끝나면 한 번에 전송
                self.notifier.flush_status(self.analyzer.market_regime.value)

                time_mod.sleep(self.config.get("check_interval", 60))
            except KeyboardInterrupt:
                logger.warning("System interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                time_mod.sleep(10) # 치명적 에러 시 잠시 대기 후 재시도
        
        sys.exit(0)