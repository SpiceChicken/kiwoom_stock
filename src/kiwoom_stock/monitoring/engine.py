"""
올-웨더 지능형 모니터링 엔진 (완전 모듈화 버전)
모듈 구성: MarketAnalyzer, TradingStrategy, Notifier, StockManager, Engine
"""

import sys
import logging
import time as time_mod
from typing import Dict, List

from concurrent.futures import ThreadPoolExecutor, as_completed

from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager
from .notifier import Notifier
from kiwoom_stock.core.database import TradeLogger

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
        
        # 모듈 초기화 (Config 분배)
        self.db = TradeLogger()
        self.analyzer = MarketAnalyzer(client, market_config)
        self.strategy = TradingStrategy(strategy_config)
        self.stock_mgr = StockManager(client, TradeLogger(), self.strategy, filter_config)
        self.notifier = Notifier(self.stock_mgr.stock_names, config)

        logger.info("Monitoring Engine Initialized.")

    def _parallel_worker(self, stock_code: str):
        """[Worker] 데이터를 하나로 합쳐서 반환 ({**metrics, **verdict})"""
        try:
            metrics = self.analyzer.supply_cache.get(stock_code)
            if not metrics: return None
            
            # 1. 전략 평가
            verdict = self.strategy.evaluate(metrics)
            if not verdict: return None
            
            return {**metrics, **verdict}
        except Exception as e:
            logger.error(f"Worker Error [{stock_code}]: {e}")
            return None

    def _run_parallel_evaluate(self, stock_list: List[str]) -> list:
        """
        [Engine] 모든 감시 종목에 대해 병렬로 전략 평가를 수행하고 결과를 수집합니다.
        """
        results = []
        # 설정에서 max_workers를 가져오거나 기본값 8 사용
        max_workers = self.config.get("max_workers", 8)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # 1. 각 종목별로 워커 스레드에 작업 할당
            futures = {
                executor.submit(self._parallel_worker, stock_code): stock_code 
                for stock_code in stock_list
            }
            
            # 2. 완료된 순서대로 결과 수집
            for f in as_completed(futures):
                try:
                    res = f.result()
                    if res:
                        results.append(res)
                except Exception as e:
                    # 특정 종목 연산 중 에러 발생 시 해당 종목만 기록하고 계속 진행
                    stock_code = futures[f]
                    logger.error(f"Parallel evaluate failed for {stock_code}: {e}")
                    
        return results

    def evaluate_entry_signal(self, verdict: Dict) -> bool:
        """
        시간 제한을 포함한 모든 진입 조건을 한곳에서 판정합니다.
    
        """
        stock_code = verdict['stock_code']
        buy_signal = verdict['is_buy_signal']

        # 1. 시간 제한 체크
        if not self.strategy.is_monitoring_time():
            return False

        # 2. 진입 가능 시간 확인
        if not self.strategy.is_trading_window():
            return False
            
        # 3. 보유 종목 확인
        if stock_code in self.stock_mgr.active_positions:
            return False

        # 4. 최근 매도 종목 냉각기 체크
        if not self.stock_mgr.is_not_recent_exit(stock_code):
            return False # 판지 얼마 안 된 놈은 점수가 좋아도 일단 참는다.

        if not buy_signal:
            return False

        return True

    def check_kill_switch(self):
        """[Engine] 전체 계좌의 리스크를 확인하고 시스템 중단 여부를 결정합니다."""
        # 1. DB에서 오늘 확정된 실현 손익 가져오기
        realized_pnl = self.db.get_today_realized_pnl()
        
        # 2. 매니저에게 실현+미실현 손익을 합산해달라고 요청 (계산 위임)
        total_pnl = self.stock_mgr.get_total_pnl_status(realized_pnl)

        # 3. 전략에게 이 정도 손실이면 멈춰야 하는지 물어보기 (판단 위임)
        if self.strategy.is_kill_switch_activated(total_pnl):
            logger.critical(f"🚨 [KILL SWITCH] 누적 손실 {total_pnl}% 도달. 시스템을 종료합니다.")
            self.notifier.notify_critical(f"🚨 킬스위치 발동: {total_pnl}% 손실로 시스템을 종료합니다.")
            return True
            
        return False

    def force_liquidate_all(self):
        """[Engine] 킬스위치 발동 시 모든 보유 종목을 즉시 정리합니다."""
        # 보유 중인 종목 코드를 복사 (순회 중 딕셔너리 변경 에러 방지)
        holding_codes = list(self.stock_mgr.active_positions.keys())
        
        if not holding_codes:
            logger.info("보유 중인 종목이 없습니다.")
            return

        results = self._run_parallel_evaluate(holding_codes)

        for verdict in results:
            try:
                self.execute_sell(verdict, "KILL-SWITCH ACTIVATED")
            except Exception as e:
                logger.error(f"Failed to liquidate {verdict['stock_code']} during kill-switch: {e}")

    def execute_buy(self, verdict: dict):
        """
        [Engine] 최종 매수 집행 조율
        - Manager에게 주문 및 기록 위임
        - 성공 시 Notifier에게 알림 요청
        """
        stock_code = verdict['stock_code']
        
        # 1. 매니저에게 주문 및 사후 처리(DB/잔고) 요청
        # 이 한 줄로 주문 + DB기록 + 내부 포지션 등록이 끝납니다.
        success, buy_data = self.stock_mgr.process_buy_order(verdict)
        
        if success:
            # 2. 주문 성공 시 알림 전송
            # buy_data에는 DB에 저장된 실제 체결 정보가 들어있습니다.
            self.notifier.notify_buy(buy_data)
            
        else:
            logger.error(f"❌ [ORDER_FAILED] {stock_code} 주문 집행에 실패했습니다.")

    def execute_sell(self, verdict: dict, reason: str):
        """
        [Engine] 최종 매도 집행 조율
        - Manager에게 매도 주문 및 포지션 정리 위임
        - 성공 시 Notifier에게 수익률 정보와 함께 알림 요청
        """
        stock_code = verdict['stock_code']

        # 1. 매니저에게 매도 집행 및 사후 처리 요청
        success, sell_data = self.stock_mgr.process_sell_order(verdict, reason)
        # success: 성공 여부, sell_data: 최종 수익률 등이 포함된 결과 데이터
        
        if success:
            # 2. 매도 성공 시 알림 전송 (수익률 포함)
            self.notifier.notify_sell(sell_data)

        else:
            logger.error(f"❌ [SELL_FAILED] {stock_code} 매도 집행에 실패했습니다.")

    def _prepare_cycle(self):
        """
        [Engine] 루프 시작 전 전처리. 여기서 전략 문맥을 1회만 업데이트함
        
        """
        # 1. 시황 파악 및 전략 컨텍스트 업데이트 (핵심 개선 지점)
        self.analyzer.update_regime()
        self.strategy.update_context(self.analyzer.market_regime)
        
        # 2. 감시 대상 및 데이터 준비 (기존 로직)
        self.stock_mgr.update_target_stocks()
        self.analyzer.update_priority_supply(self.stock_mgr.stocks)
        self.notifier.start_status_session()

    def run(self):
        """메인 실행 루프"""
        logger.info("Starting Monitoring Loop...")
        while True:
            try:
                # [안전장치] 시장 마감 확인 및 가동 중단
                if not self.strategy.is_monitoring_time():
                    logger.info("Market is closed. Shutting down system.")
                    break
                
                self._prepare_cycle()

                # 비상 정지 체크
                if self.check_kill_switch():
                    self.force_liquidate_all()
                    break

                # 병렬 분석 시작
                results = self._run_parallel_evaluate(self.stock_mgr.stocks)

                # 결과 순차 처리 (주문 집행)
                for verdict in results:
                    # 로그 전송
                    self.notifier.collect_status({
                        "name": self.stock_mgr.stock_names[verdict['stock_code']],
                        "price": verdict["price"],
                        "score": verdict['score'], **{f"{k}_score": v for k, v in verdict['score_detail'].items()},
                        "momentum": verdict['momentum'],
                        "reason": verdict['status']})
                    
                    # 매도 감시 및 매수 판단 (res를 통째로 전달)
                    sell_reason = self.stock_mgr.evaluate_position(verdict, self.strategy.curr_strong_th)
                    if sell_reason:
                        self.execute_sell(verdict, sell_reason)
                    elif self.evaluate_entry_signal(verdict):
                        self.execute_buy(verdict)
                # ---------------------------------------------

                self.notifier.flush_status(self.analyzer.market_regime.value)
                time_mod.sleep(self.config.get("check_interval", 60))
            except KeyboardInterrupt:
                logger.warning("System interrupted by user.")
                break
            except Exception as e:
                # 1. 상세 로그 기록 (파일/콘솔)
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                
                # 2. [추가] Slack 알림 전송
                # 에러 메시지의 핵심 내용을 Slack으로 전송합니다.
                self.notifier.notify_error(str(e))
                
                # 3. 일시 대기 후 루프 재시도 (시스템 안정성 확보)
                time_mod.sleep(10)
        
        sys.exit(0)