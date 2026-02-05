"""
[Engine] 트레이딩 시스템의 핵심 실행 엔진 (Lightweight Version)
- 역할: 모듈 조율 및 흐름 제어 (분석 -> 판단 -> 매매)
- 원칙: 세부 로직은 각 매니저(Manager, Strategy)에게 위임하고, 엔진은 '결정'만 내린다.
"""

import sys
import logging
import time as time_mod
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager
from .notifier import Notifier
from kiwoom_stock.core.database import TradeLogger

logger = logging.getLogger(__name__)

class TradingEngine:
    """[Control Tower] 트레이딩 시스템 메인 컨트롤러"""
    
    def __init__(self, client, config: Dict):
        self.config = config
        self.check_interval = config.get("check_interval", 60)
        
        # [Modules] 기능별 모듈 초기화
        self.db = TradeLogger()
        self.analyzer = MarketAnalyzer(client, config.get("market", {}))
        self.strategy = TradingStrategy(config.get("strategy", {}))
        self.stock_mgr = StockManager(client, self.db, self.strategy, config.get("filters", {}))
        self.notifier = Notifier(self.stock_mgr.stock_names, config)
        
        self.executor = ThreadPoolExecutor(max_workers=config.get("max_workers", 8))
        logger.info("Trading Engine Initialized.")

    def run(self):
        """[Main Loop] 무한 루프: 분석 -> 판단 -> 행동"""
        logger.info(f"Engine Start (Interval: {self.check_interval}s)")
        
        while True:
            try:
                # 1. 운영 시간 및 리스크 점검 (Fail-Fast)
                if not self._check_system_status():
                    if not self.strategy.is_monitoring_time(): break # 장 마감 시 종료
                    time_mod.sleep(60)
                    continue

                # 2. 사이클 준비 (시황 분석 & 타겟 갱신)
                self._prepare_cycle()

                # 3. 병렬 전략 평가 (Core Logic)
                verdicts = self._evaluate_stocks(self.stock_mgr.stocks)

                # 4. 매매 의사 결정 및 집행
                self._process_decisions(verdicts)

                # 5. 주기적 리포팅
                self.notifier.flush_status(self.analyzer.market_regime.value)
                time_mod.sleep(self.check_interval)

            except KeyboardInterrupt:
                logger.warning("User Terminated.")
                break
            except Exception as e:
                logger.critical(f"Main Loop Error: {e}", exc_info=True)
                self.notifier.notify_error(str(e))
                time_mod.sleep(10)
        
        sys.exit(0)

    def _check_system_status(self) -> bool:
        """[Check 1] 장 운영 시간 및 킬스위치 점검"""
        # 1. 시간 체크
        if not self.strategy.is_monitoring_time():
            logger.info("Outside of trading hours.")
            return False

        # 2. 킬스위치(누적 손실) 체크
        total_pnl = self.stock_mgr.get_total_pnl_status(self.db.get_today_realized_pnl())
        if self.strategy.is_kill_switch_activated(total_pnl):
            msg = f"🚨 KILL-SWITCH ACTIVATED (PnL: {total_pnl}%)"
            logger.critical(msg)
            self.notifier.notify_critical(msg)
            self._force_liquidate() # 비상 청산
            return False
            
        return True

    def _prepare_cycle(self):
        """[Pre-process] 데이터 갱신 및 준비"""
        self.analyzer.update_regime()
        self.strategy.update_context(self.analyzer.market_regime)
        self.stock_mgr.update_target_stocks()
        self.analyzer.update_priority_supply(self.stock_mgr.stocks)
        self.notifier.start_status_session()

    def _evaluate_stocks(self, targets: List[str]) -> List[Dict]:
        """[Parallel] 워커 스레드를 통한 전략 평가"""
        results = []
        futures = {self.executor.submit(self._worker_task, code): code for code in targets}
        
        for f in as_completed(futures):
            try:
                if res := f.result(): results.append(res)
            except Exception as e:
                logger.error(f"Eval Error ({futures[f]}): {e}")
        return results

    def _worker_task(self, code: str) -> Optional[Dict]:
        """[Worker] 단위 작업: 데이터 조회 + 전략 계산"""
        metrics = self.analyzer.supply_cache.get(code)
        if not metrics: return None
        
        if verdict := self.strategy.evaluate(metrics):
            return {**metrics, **verdict}
        return None

    def _process_decisions(self, verdicts: List[Dict]):
        """[Decision] 전략 결과를 바탕으로 매수/매도/관망 결정"""
        for v in verdicts:
            # 상태 로깅
            self._log_status(v)

            # 1. 매도(청산) 검사 - 보유 중인 경우만
            # strategy.py 리팩토링 반영: curr_strong_th -> curr_strict_th (엄격 기준 적용)
            if reason := self.stock_mgr.evaluate_position(v, self.strategy.curr_strict_th):
                self._execute_order('SELL', v, reason)
            
            # 2. 매수(진입) 검사 - 미보유 시
            elif self._should_enter(v):
                self._execute_order('BUY', v)

    def _should_enter(self, v: Dict) -> bool:
        """[Filter] 진입 조건 종합 검증 (시간, 중복, 신호)"""
        return (
            self.strategy.is_trading_window() and
            v['is_buy_signal'] and
            v['stock_code'] not in self.stock_mgr.active_positions and
            self.stock_mgr.is_not_recent_exit(v['stock_code'])
        )

    def _execute_order(self, side: str, verdict: Dict, reason: str = None):
        """[Execution] 매매 집행 통합 메서드"""
        code = verdict['stock_code']
        success, data = False, {}

        if side == 'BUY':
            success, data = self.stock_mgr.process_buy_order(verdict)
            if success: self.notifier.notify_buy(data)
        elif side == 'SELL':
            success, data = self.stock_mgr.process_sell_order(verdict, reason)
            if success: self.notifier.notify_sell(data)

        if not success:
            logger.error(f"❌ {side} Order Failed: {code}")

    def _force_liquidate(self):
        """[Emergency] 전량 매도 (비상 시)"""
        for code in list(self.stock_mgr.active_positions.keys()):
            self.stock_mgr.sell_stock(code, "KILL-SWITCH")

    def _log_status(self, v: Dict):
        """Notifier로 상태 전송"""
        try:
            self.notifier.collect_status({
                "name": self.stock_mgr.stock_names.get(v['stock_code'], v['stock_code']),
                "price": v["price"],
                "score": v['score'],
                "momentum": v['momentum'],
                "reason": v['status'],
                **{f"{k}_score": val for k, val in v['score_detail'].items()}
            })
        except Exception:
            pass