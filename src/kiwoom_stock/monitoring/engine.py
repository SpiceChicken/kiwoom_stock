"""
[Engine] 트레이딩 시스템의 핵심 실행 엔진 (Lightweight Version)
- 역할: 모듈 조율 및 흐름 제어 (분석 -> 판단 -> 매매)
- 원칙: 세부 로직은 각 매니저(Manager, Strategy)에게 위임하고, 엔진은 '결정'만 내린다.
"""

import sys
import logging
import time as time_mod
from typing import Dict, List, Optional, Union, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

from .analyzer import MarketAnalyzer
from .strategy import TradingStrategy
from .manager import StockManager
from .notifier import Notifier
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.core.database import TradeLogger
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

logger = logging.getLogger(__name__)

class TradingEngine:
    """[Control Tower] 트레이딩 시스템 메인 컨트롤러"""
    
    def __init__(self, client, config: Dict):
        self.config = config
        self.check_interval = config.get("check_interval", 60)
        
        # [Modules] 기능별 모듈 초기화
        self.db = TradeLogger()
        self.state_tracker = PhysicalStateTracker(self.db)
        self.analyzer = MarketAnalyzer(client, config.get("market", {}), self.state_tracker)
        self.strategy = TradingStrategy(config.get("strategy", {}))
        self.stock_mgr = StockManager(client, self.db, config.get("filters", {}))
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
            msg = f"🚨 KILL-SWITCH ACTIVATED (PnL: {total_pnl:.1f}%)"
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
        
        for stock_code in self.stock_mgr.stocks:
            if stock_code not in self.state_tracker._l1_cache:
                self.state_tracker.recover_state_from_crash(stock_code)
                
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
        # metrics는 이제 SupplyData 객체임
        metrics: SupplyData = self.analyzer.supply_cache.get(code)
        if not metrics: return None
        
        # Strategy.evaluate는 이제 SupplyData 객체를 받음
        if verdict := self.strategy.evaluate(metrics):
            # verdict(Dict)에 metrics(SupplyData) 내용을 합칠 필요가 있는지 확인
            # 기존에는 {**metrics, **verdict}로 합쳤으나, metrics가 객체이므로 
            # 필요한 데이터만 verdict에 담겨 나오도록 strategy.evaluate를 수정했음 (price, stock_code)
            return verdict
        return None

    def _process_decisions(self, verdicts: List[Dict]):
        """[Decision] 전략 결과를 바탕으로 매수/매도/관망 결정"""
        for verdict in verdicts:
            # 상태 로깅
            self._log_status(verdict)
            
            stock_code = verdict['stock_code']

            # =================================================================
            # 1. 매도(청산) 검사 - 보유 중인 경우만
            # =================================================================
            if stock_code in self.stock_mgr.active_positions:
                
                # 1-1. Manager에게는 '순수 데이터 갱신'만 지시하고 객체를 돌려받음
                # (이전에 함께 수정한 manager.py의 update_position_data 활용)
                pos = self.stock_mgr.update_position_data(verdict)
                if not pos:
                    continue
                
                # 1-2. Engine이 직접 Strategy를 호출하여 매도 사유 판별
                # 물리 엔진 상세 데이터 호환성 처리 (forces 또는 score_detail)
                forces = verdict.get('forces', {})
                
                exit_reason = self.strategy.get_exit_reason(pos, verdict['price'], forces)
                
                # 1-3. 매도 사유가 발생했다면 매도 집행
                if exit_reason:
                    self._execute_order('SELL', verdict, exit_reason)
                    
                    # (옵션) 매도 완료 후 Strategy 내부의 동적 상태 캐시 정리
                    if hasattr(self.strategy, '_kinetic_state'):
                        self.strategy._kinetic_state.pop(stock_code, None)
            
            # =================================================================
            # 2. 매수(진입) 검사 - 미보유 시
            # =================================================================
            elif self._should_enter(verdict):
                self._execute_order('BUY', verdict)

    def _should_enter(self, verdict: Dict) -> bool:
        """[Filter] 진입 조건 종합 검증 (시간, 중복, 신호)"""
        return (
            self.strategy.is_trading_window() and
            verdict['is_buy_signal'] and
            verdict['stock_code'] not in self.stock_mgr.active_positions and
            self.stock_mgr.is_not_recent_exit(verdict['stock_code'])
        )

    def _execute_order(self, side: str, verdict: Dict, reason: Optional[str] = None):
        """[Execution] 매매 집행 통합 메서드"""
        code = verdict['stock_code']
        success = False
        # data 변수가 Dict와 Position 모두 담을 수 있도록 Union 처리
        data: Union[Dict, Position, None] = None

        if side == 'BUY':
            success, data = self.stock_mgr.process_buy_order(verdict)
            # data가 Dict인지 확인 후 전달 (Type Guard)
            if success and isinstance(data, dict): 
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            # reason이 None일 경우 안전 처리
            safe_reason = reason if reason else "Unknown"
            success, data = self.stock_mgr.process_sell_order(verdict, safe_reason)
            # data가 Position인지 확인 후 전달
            if success and isinstance(data, Position):
                self.notifier.notify_sell(data)

        if not success:
            logger.error(f"❌ {side} Order Failed: {code}")

    def _force_liquidate(self):
        """[Emergency] 전량 매도 (비상 시)"""
        for code in list(self.stock_mgr.active_positions.keys()):
            self.stock_mgr.sell_stock(code, "KILL-SWITCH")

    def _log_status(self, verdict: Dict):
        """Notifier로 상태 전송"""
        try:
            self.notifier.collect_status({
                "name": self.stock_mgr.stock_names.get(verdict['stock_code'], verdict['stock_code']),
                "price": verdict["price"],
                "score": verdict['score'],
                "momentum": verdict['momentum'],
                "reason": verdict['status'],
                "forces": verdict.get('forces', {})  # [수정] 물리 엔진의 7대 벡터 힘 데이터 전달
            })
        except Exception:
            pass