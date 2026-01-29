"""
올-웨더 지능형 모니터링 엔진 (완전 모듈화 버전)
모듈 구성: MarketAnalyzer, TradingStrategy, Notifier, StockManager, Engine
"""

import sys
import logging
import time as time_mod
from typing import Dict, Optional

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
        self.status_log = {}

        logger.info("Monitoring Engine Initialized.")

    def _sync_stock_state(self, stock_code: str):
        """
        [책임 1] 데이터 동기화 전담
        신호 여부와 상관없이 무조건 최신 지표와 점수를 로그/객체에 기록합니다.
        """
        metrics = self.analyzer.supply_cache.get(stock_code)
        if not metrics: return None

        # 전략가에게 단순 '점수 계산'만 요청 (판단이 아님)
        verdict = self.strategy.evaluate(metrics, self.analyzer.market_regime)
        
        if stock_code in self.stock_mgr.active_positions:
            self.stock_mgr.active_positions[stock_code].sell_price = metrics['price']
        
        # 시스템 내부 상태(로그 등)만 업데이트하고 끝
        self.status_log[stock_code] = {
            "name": self.stock_mgr.stock_names[stock_code],
            "price": metrics["price"],
            "score": verdict['score'], **{f"{k}_score": v for k, v in verdict['score_detail'].items()},
            "momentum": verdict['momentum'],
            "reason": verdict['status']}
        
        return {**metrics, **verdict}

    def evaluate_entry_signal(self) -> bool:
        """
        시간 제한을 포함한 모든 진입 조건을 한곳에서 판정합니다.
    
        """
        # 1. 시간 제한 체크
        if not self.strategy.is_monitoring_time():
            return False

        # 2. 진입 가능 시간 확인
        if not self.strategy.is_trading_window():
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

        for stock_code in holding_codes:
            try:
                verdict = self._sync_stock_state(stock_code)
                self.execute_sell(verdict, "KILL-SWITCH ACTIVATED")
            except Exception as e:
                logger.error(f"Failed to liquidate {stock_code} during kill-switch: {e}")


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
        [Engine] 매 루프 시작 전, 시장 상황과 타겟 종목을 동기화합니다.
        이 함수는 '시장의 판을 짜는' 역할을 합니다.
        """
        # 1. 시황 파악 (전략 임계값의 기준)
        self.analyzer.update_regime()
        
        # 2. 감시 대상 확정 (매니저가 종목 리스트 갱신)
        self.stock_mgr.update_target_stocks()
        
        # 3. 데이터 준비 (확정된 종목들의 수급 데이터 로드)
        self.analyzer.update_priority_supply(self.stock_mgr.stocks)

        # 4. 루프 시작 시 데이터 저장소 초기화
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
                
                # [최적화] API 호출 중복 제거: 한 번의 루프에서 모든 데이터 수집 및 결과 저장
                for stock_code in self.stock_mgr.stocks:
                    # 1. 일단 모든 종목의 정보를 최신화 (SRP: 데이터 동기화)
                    # 이 과정에서 킬스위치에 필요한 모든 가격 정보가 status_log에 채워집니다.
                    verdict = self._sync_stock_state(stock_code)
                    log = self.status_log[stock_code]
                    if not verdict: continue

                    self.notifier.collect_status(log)

                    # 2. 보유 종목 매도 감시 위임
                    strong_thresholds = self.strategy.entry_thresholds.get('strong', 85.0)
                    sell_reason = self.stock_mgr.evaluate_position(verdict, strong_thresholds)
                    if sell_reason:
                        self.execute_sell(verdict, sell_reason)
                        continue

                    # 3. 매수 기회 탐색 (SRP: 판단)
                    if verdict.get('is_buy_signal') and self.evaluate_entry_signal():
                        self.execute_buy(verdict)
                # ---------------------------------------------

                self.notifier.flush_status(self.analyzer.market_regime.value)
                time_mod.sleep(self.config.get("check_interval", 60))
            except KeyboardInterrupt:
                logger.warning("System interrupted by user.")
                break
            except Exception as e:
                logger.critical(f"Critical error in main loop: {e}", exc_info=True)
                time_mod.sleep(10) # 치명적 에러 시 잠시 대기 후 재시도
        
        sys.exit(0)