"""
올-웨더 지능형 모니터링 엔진 (고도화 통합 버전)
시장 레짐 분석, 상대적 수급 강도 분석, DB 영속성을 포함한 멀티 타임프레임 전략을 수행합니다.
"""

import time
import statistics
import sys
from datetime import datetime
from typing import Dict, List, Optional
from collections import deque

from ..api.parser import clean_numeric
from ..core.indicators import Indicators
from kiwoom_stock.core.database import TradeLogger


class MultiTimeframeRSIMonitor:
    def __init__(self, client, config: Dict):
        """시스템 초기화 및 DB 복구"""
        self.client = client
        self.config = config
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}
        self.check_interval = config.get("check_interval", 60)
        
        # 지표 계산기 및 DB 초기화
        self.trend_calc = Indicators(period=config.get("trend_timeframe", {}).get("rsi_period", 14))
        self.entry_calc = Indicators(period=config.get("entry_timeframe", {}).get("rsi_period", 9))
        self.db = TradeLogger()
        
        # 상태 추적 및 캐시
        self.status_log: Dict[str, Dict] = {}
        self.supply_cache: Dict[str, Dict] = {}  # {'code': {'f': 외인수량, 'i': 기관수량}} 구조
        self.score_history: Dict[str, float] = {}
        
        # 시장 레짐 정보
        self.market_rsi = 50.0
        self.market_proxy_code = "069500"  # KODEX 200
        self.market_regime = "Unknown"
        self.market_rsi_history = deque(maxlen=20)
        self.breadth_history = deque(maxlen=20)
        
        # 동적 임계값 안전장치 기본값
        self.dynamic_rsi_high = 60.0
        self.dynamic_rsi_low = 40.0
        self.dynamic_breadth_th = 1.5
        
        # [핵심] 프로그램 시작 시 DB에서 보유 중인(OPEN) 종목 복구
        self.active_positions = self.db.load_open_positions()
        self.momentum_threshold = config.get("momentum_threshold", 10.0)

    # --- [데이터 수집 및 분석] ---

    def _fetch_market_supply(self):
        """코스피 시장의 외인/기관 수급 데이터를 분리하여 캐싱합니다 (시너지 분석용)."""
        try:
            self.supply_cache = {}
            for invsr, key in [("6", "f"), ("7", "i")]:
                items = self.client.market.get_investor_supply(market_tp="001", investor_tp=invsr)
                for item in items:
                    code = item.get("stk_cd", "").split('_')[0]
                    if not code: continue
                    qty = clean_numeric(item.get("netprps_qty", "0"))
                    
                    if code not in self.supply_cache:
                        self.supply_cache[code] = {'f': 0, 'i': 0}
                    self.supply_cache[code][key] = qty
        except Exception as e:
            print(f"수급 데이터 캐싱 실패: {e}")

    def _calculate_supply_score(self, metrics: Dict) -> float:
        """
        [고도화] 단순 수량이 아닌 '거래량 대비 비중'과 '주체별 협응도'를 분석합니다.
        """
        total_vol = metrics.get('volume', 1)
        net_buy = metrics.get('net_buy', 0)
        
        # 1. 수급 비중 계산 (Net Buy / Total Volume)
        # 전체 거래량 중 순매수 비중이 0.5%일 때 50점, 2% 이상일 때 100점 도달 (선형)
        supply_ratio = (net_buy / total_vol) * 100
        base_score = min(100, max(0, supply_ratio * 50))

        # 2. 수급 주체 협응도 (Synergy Bonus)
        f_buy = metrics.get('f_buy', 0)
        i_buy = metrics.get('i_buy', 0)
        
        synergy_bonus = 0
        if f_buy > 0 and i_buy > 0: synergy_bonus = 20    # 양매수 가점
        elif f_buy < 0 and i_buy < 0: synergy_bonus = -20 # 양매도 감점

        return max(0, min(100, base_score + synergy_bonus))

    def _calculate_conviction_score(self, metrics: Dict) -> float:
        """지표의 강도에 비례하여 0~100점 사이의 연속적인 점수를 산출합니다."""
        weights = self._get_scoring_weights()
        score = 0.0

        # 1. Alpha 점수 (상대강도): 0이면 50점, +20 이상 100점, -20 이하 0점
        alpha_base = 50 + (metrics['alpha'] * 2.5)
        score += max(0, min(100, alpha_base)) * weights['alpha']

        # 2. 수급 점수 (비중 기반 고도화 로직 적용)
        score += self._calculate_supply_score(metrics) * weights['supply']

        # 3. VWAP 점수 (이격도): VWAP와 같으면 50점, +2% 상승 시 100점
        if metrics['vwap'] > 0:
            deviation_pct = (metrics['price'] / metrics['vwap'] - 1) * 100
            vwap_score = 50 + (deviation_pct * 25)
            score += max(0, min(100, vwap_score)) * weights['vwap']

        # 4. 추세 점수 (RSI): 50(보통) ~ 70(강세) 구간을 50~100점으로 세분화
        trend_rsi = metrics['trend_rsi']
        t_score = 50 + ((trend_rsi - 50) * 2.5) if trend_rsi >= 50 else (trend_rsi)
        score += max(0, min(100, t_score)) * weights['trend']

        return round(score, 1)

    # --- [시장 레짐 및 임계값] ---

    def _update_market_status(self):
        """시장 지수 RSI와 종목 확산 지표를 분석하여 레짐을 정의합니다."""
        try:
            chart_data = self.client.market.get_minute_chart(self.market_proxy_code, tic="60")
            closes = [item['close'] for item in chart_data]
            self.market_rsi = self.trend_calc.calculate(closes)
            
            breadth = self.client.market.get_market_breadth(market_tp="001")
            self.breadth_ratio = breadth['rising'] / max(1, breadth['falling'])

            self.market_rsi_history.append(self.market_rsi)
            self.breadth_history.append(self.breadth_ratio)

            if len(self.market_rsi_history) >= 5:
                rsi_avg = statistics.mean(self.market_rsi_history)
                rsi_std = statistics.stdev(self.market_rsi_history)
                self.dynamic_rsi_high = max(55, min(70, rsi_avg + (0.5 * rsi_std)))
                self.dynamic_rsi_low = max(30, min(45, rsi_avg - (0.5 * rsi_std)))
                self.dynamic_breadth_th = max(1.2, min(2.5, statistics.mean(self.breadth_history) * 1.2))

            if self.market_rsi > self.dynamic_rsi_high:
                self.market_regime = "과열 구간 (강세)" if self.breadth_ratio > self.dynamic_breadth_th else "쏠림 구간 (독주)"
            elif self.market_rsi < self.dynamic_rsi_low:
                self.market_regime = "위축 구간 (반등대기)"
            else:
                self.market_regime = "평온 구간 (박스권)"
            
        except Exception as e:
            print(f"시장 분석 실패: {e}")

    def _get_scoring_weights(self) -> Dict[str, float]:
        if "과열" in self.market_regime or "쏠림" in self.market_regime:
            return {"alpha": 0.4, "supply": 0.2, "vwap": 0.2, "trend": 0.2}
        elif "위축" in self.market_regime:
            return {"alpha": 0.2, "supply": 0.4, "vwap": 0.2, "trend": 0.2}
        return {"alpha": 0.3, "supply": 0.3, "vwap": 0.3, "trend": 0.1}

    def _get_dynamic_thresholds(self) -> Dict[str, float]:
        base = {"strong": 80.0, "alert": 70.0, "interest": 60.0}
        if "과열" in self.market_regime: return {"strong": 85.0, "alert": 75.0, "interest": 65.0}
        if "위축" in self.market_regime: return {"strong": 75.0, "alert": 65.0, "interest": 55.0}
        return base

    # --- [모니터링 실행 로직] ---

    def monitor_active_signals(self, stock_code, current_price, current_score):
        """보유 종목의 매도 조건을 감시하고 DB에 영구 기록합니다."""
        if stock_code not in self.active_positions: return

        pos = self.active_positions[stock_code]
        if current_score < 50:
            profit = round((current_price / pos['buy_price'] - 1) * 100, 2)
            self.db.record_sell(pos['id'], current_price, profit, "Score Decay")
            print(f"📉 [가상 매도] {pos['stock_name']} | 수익률: {profit:+}% | 사유: 점수 하락")
            del self.active_positions[stock_code]

    def check_conditions(self, stock_code: str) -> Optional[Dict]:
        """개별 종목의 지표 산출 및 점수화"""
        try:
            trend_data = self.client.market.get_minute_chart(stock_code, tic="60")
            entry_data = self.client.market.get_minute_chart(stock_code, tic="5")
            if not trend_data or len(entry_data) < 20: return None

            curr_price = entry_data[0]['close']
            curr_vol = sum(d['volume'] for d in entry_data) # 당일 누적 거래량
            s_data = self.supply_cache.get(stock_code, {'f': 0, 'i': 0})
            
            metrics = {
                "alpha": self.entry_calc.calculate([d['close'] for d in entry_data]) - self.market_rsi,
                "net_buy": s_data['f'] + s_data['i'],
                "f_buy": s_data['f'], "i_buy": s_data['i'],
                "price": curr_price, "volume": curr_vol,
                "vwap": sum(d['close']*d['volume'] for d in entry_data)/curr_vol if curr_vol > 0 else curr_price,
                "trend_rsi": self.trend_calc.calculate([d['close'] for d in trend_data])
            }

            score = self._calculate_conviction_score(metrics)
            
            # 모멘텀 계산 (첫 루프 0 방지)
            momentum = round(score - self.score_history.get(stock_code, score), 1)
            self.score_history[stock_code] = score
            
            th = self._get_dynamic_thresholds()
            status = "🔥강력추천" if score >= th['strong'] else ("👀관심" if score >= th['interest'] else "관망")
            if momentum >= self.momentum_threshold: status = "🚀수급폭발"

            # 로그 및 모멘텀 데이터 저장
            self.status_log[stock_code] = {"price": curr_price, "score": score, "momentum": momentum, "reason": status}
            
            if score >= th['alert'] or momentum >= self.momentum_threshold:
                return {**metrics, "stock_code": stock_code, "score": score, "momentum": momentum}
            return None
        except: return None

    def update_target_stocks(self):
        """보유 종목 + 거래대금 상위 종목 통합 리스트 관리"""
        try:
            new_stocks = list(self.active_positions.keys()) # DB 보유 종목 우선
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            for item in upper_list:
                code, name = item['stk_cd'], item['stk_nm']
                if any(kw in name for kw in ('KODEX', 'TIGER', 'ACE', 'SOL', 'RISE', 'KoAct', 'HANARO', 'PLUS')): continue
                if code not in new_stocks: new_stocks.append(code)
                self.stock_names[code] = name
            self.stocks = new_stocks[:40]
            print(f"INFO: 감시 종목 갱신 (총 {len(self.stocks)}개 | 보유: {len(self.active_positions)}개)")
        except Exception as e: print(f"종목 갱신 실패: {e}")

    def _is_monitoring_time(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5: return False
        return now.replace(hour=8, minute=30, second=0) <= now <= now.replace(hour=15, minute=40, second=0)

    def run(self):
        """메인 모니터링 루프"""
        self.update_target_stocks()
        while True:
            if not self._is_monitoring_time():
                print(f"\n🔔 [{datetime.now().strftime('%H:%M:%S')}] 장 종료. 시스템을 안전하게 중단합니다.")
                break

            self._update_market_status()
            self._fetch_market_supply()
            
            # 모든 종목 데이터 선행 계산
            for stock in self.stocks: self.check_conditions(stock)
            
            # 모멘텀 순 정렬
            sorted_stocks = sorted(self.stocks, key=lambda x: self.status_log.get(x, {}).get('momentum', 0), reverse=True)

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {self.market_regime}")
            print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
            print("-" * 55)

            for stock in sorted_stocks:
                res = self.check_conditions(stock)
                log = self.status_log.get(stock)
                if not log or "price" not in log: continue

                self.monitor_active_signals(stock, log['price'], log['score'])
                
                # 가독성 높은 화면 출력
                name = self.stock_names.get(stock, stock)
                m_str = f"+{log['momentum']}" if log['momentum'] > 0 else f"{log['momentum']}"
                print(f"{name:<10} | {log['score']:>5.1f} | {m_str:>6} | {log['reason']:<10}")
                
                if res:
                    # 매수 신호 시 DB 저장 및 메모리 등록
                    if res['score'] >= 80 and stock not in self.active_positions:
                        buy_data = {
                            "stock_code": stock, "stock_name": name, "buy_price": log['price'], 
                            "buy_score": log['score'], "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
                            "buy_regime": self.market_regime
                        }
                        buy_data['id'] = self.db.record_buy(buy_data)
                        self.active_positions[stock] = buy_data
                        self._send_alert(res)
                    
                    if log['momentum'] >= self.momentum_threshold:
                        self._send_momentum_alert(res)

            time.sleep(self.check_interval)
        sys.exit(0)

    def _send_alert(self, res: Dict):
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"\n🔥 [강력 추천] {name}({res['stock_code']}) 매수 타점 포착! (점수: {res['score']})")
        print(f"  - 종합 점수: {res['score']}점")
        print(f"  - 현재 가격: {res['price']:,.0f}원")
        print(f"  - 상대 강도(Alpha): {res.get('alpha', 0):+.1f}")
        print(f"  - 기관/외인 수급: {res['net_buy']:,}주")
        print(f"{'='*55}")

    def _send_momentum_alert(self, res: Dict):
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"\n🚀 [수급 폭발] {name}({res['stock_code']}) 점수 급상승! ({res['momentum']:+})")