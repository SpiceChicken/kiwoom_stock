"""
[Engine] 트레이딩 시스템의 핵심 실행 엔진 (Physics-Sniper Version)
- 역할: 모듈 조율 및 동적 폴링(Dynamic Polling) 기반 흐름 제어
- 원칙: '관심도'에 따라 종목별 검사 주기(Interval)를 다르게 배정하여 API 제한을 완벽 방어한다.
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
        
        # -------------------------------------------------------------
        # 🚀 [동적 폴링 타이머 세팅]
        # fast_interval: 보유 중이거나 엔진이 켜진 종목 (10초 단위 밀착 감시)
        # slow_interval: 죽어있는 심해 종목 (60초 단위 여유 감시)
        # -------------------------------------------------------------
        self.fast_interval = config.get("fast_interval", 10)
        self.slow_interval = config.get("slow_interval", 60)
        
        self._last_check_time: Dict[str, float] = {}
        self._last_global_update = 0.0  # 시황/조건검색 업데이트용 타이머
        
        # [Modules] 기능별 모듈 초기화
        self.db = TradeLogger()
        self.state_tracker = PhysicalStateTracker(self.db)
        self.analyzer = MarketAnalyzer(client, config.get("market", {}), self.state_tracker)
        self.strategy = TradingStrategy(config.get("strategy", {}))
        self.stock_mgr = StockManager(client, self.db, config.get("filters", {}))
        self.notifier = Notifier(self.stock_mgr.stock_names, config)
        
        self.executor = ThreadPoolExecutor(max_workers=config.get("max_workers", 8))
        logger.info("Trading Engine Initialized with Dynamic Polling.")

    def run(self):
        """[Main Loop] 무한 루프: 분석 -> 판단 -> 행동"""
        logger.info(f"Engine Start (Fast Track: {self.fast_interval}s / Slow Track: {self.slow_interval}s)")
        
        while True:
            try:
                # 1. 운영 시간 및 리스크 점검
                if not self._check_system_status():
                    if not self.strategy.is_monitoring_time(): break # 장 마감 시 종료
                    time_mod.sleep(60)
                    continue

                now = time_mod.time()

                # 2. [글로벌 업데이트] API 부하가 큰 시황/조건검색은 60초에 한 번만 갱신
                if now - getattr(self, '_last_global_update', 0) >= 60.0:
                    self.analyzer.update_regime()
                    self.strategy.update_context(self.analyzer.market_regime)
                    self.stock_mgr.update_target_stocks()
                    self._last_global_update = now

                # 3. [동적 폴링] 이번 틱(Tick)에 검사해야 할 타겟만 영리하게 추출
                due_targets = self._get_due_targets()

                if not due_targets:
                    # 검사할 종목이 없으면 엔진은 1초만 대기하고 바로 다음 루프 회전
                    time_mod.sleep(1)
                    continue

                # 4. 사이클 준비 및 평가 (추출된 타겟만 API 호출)
                self._prepare_cycle(due_targets)
                verdicts = self._evaluate_stocks(due_targets)

                # 5. 매매 의사 결정 및 집행
                self._process_decisions(verdicts)

                # 6. 주기적 리포팅
                self.notifier.flush_status(self.analyzer.market_regime.value)
                
                # 메인 루프는 1초마다 초고속으로 회전함 (종목별 간격 조절은 _get_due_targets가 담당)
                time_mod.sleep(1)

            except KeyboardInterrupt:
                logger.warning("User Terminated.")
                break
            except Exception as e:
                logger.critical(f"Main Loop Error: {e}", exc_info=True)
                self.notifier.notify_error(str(e))
                time_mod.sleep(10)
        
        sys.exit(0)

    def _get_due_targets(self) -> List[str]:
        """[Scheduler] 투트랙 인터벌 정책에 따라 현재 검사해야 할 종목 리스트 반환"""
        now = time_mod.time()
        due_stocks = []
        
        for code in self.stock_mgr.stocks:
            last_checked = self._last_check_time.get(code, 0.0)
            
            # 상태 추출
            is_active = code in self.stock_mgr.active_positions
            cached_data = self.analyzer.supply_cache.get(code)
            last_strength = getattr(cached_data, 'strength', 0.0) if cached_data else 0.0
            
            # [핵심 로직] 내 돈이 들어갔거나, 세력이 개입(체결강도 100 이상)한 종목은 Fast Track
            if is_active or last_strength >= 100.0:
                target_interval = self.fast_interval
            else:
                target_interval = self.slow_interval
                
            # 시간이 도래한 종목만 스캔 리스트에 추가
            if now - last_checked >= target_interval:
                due_stocks.append(code)
                self._last_check_time[code] = now
                
        return due_stocks

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

    def _prepare_cycle(self, targets: List[str]):
        """[Pre-process] 선택된 타겟(Fast or Slow)의 최신 API 데이터만 선별 수집"""
        for stock_code in targets:
            if stock_code not in self.state_tracker._l1_cache:
                self.state_tracker.recover_state_from_crash(stock_code)
                
        # [최적화] 전체 50개가 아닌 due_targets(예: 3개)에 대해서만 API 호출
        self.analyzer.update_priority_supply(targets)
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
        metrics: SupplyData = self.analyzer.supply_cache.get(code)
        if not metrics: return None
        
        if verdict := self.strategy.evaluate(metrics):
            return verdict
        return None

    def _process_decisions(self, verdicts: List[Dict]):
        """[Decision] 전략 결과를 바탕으로 매수/매도/관망 결정 (Orchestrator 역할)"""
        for verdict in verdicts:
            self._log_status(verdict)
            stock_code = verdict['stock_code']

            # 1. 매도(청산) 검사 - 보유 중인 경우
            if stock_code in self.stock_mgr.active_positions:
                pos = self.stock_mgr.update_position_data(verdict)
                if not pos: continue
                
                forces = verdict.get('forces', {}) 
                exit_reason = self.strategy.get_exit_reason(pos, verdict['price'], forces)
                
                if exit_reason:
                    self._execute_order('SELL', verdict, exit_reason)
                    if hasattr(self.strategy, '_kinetic_state'):
                        self.strategy._kinetic_state.pop(stock_code, None)
            
            # 2. 매수(진입) 검사 - 미보유 시
            elif self._should_enter(verdict):
                self._execute_order('BUY', verdict)

    def _should_enter(self, verdict: Dict) -> bool:
        """[Filter] 진입 조건 종합 검증 (시간, 중복, 신호)"""
        return (
            self.strategy.is_trading_window() and
            verdict['is_buy_signal'] and
            verdict['stock_code'] not in self.stock_mgr.active_positions
        )

    def _execute_order(self, side: str, verdict: Dict, reason: Optional[str] = None):
        """[Execution] 매매 집행 통합 메서드"""
        code = verdict['stock_code']
        success = False
        data: Union[Dict, Position, None] = None

        if side == 'BUY':
            success, data = self.stock_mgr.process_buy_order(verdict)
            if success and isinstance(data, dict): 
                self.notifier.notify_buy(data)
        elif side == 'SELL':
            safe_reason = reason if reason else "Unknown"
            success, data = self.stock_mgr.process_sell_order(verdict, safe_reason)
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
                "reason": verdict['status'],
                "forces": verdict.get('forces', {}) 
            })
        except Exception:
            pass