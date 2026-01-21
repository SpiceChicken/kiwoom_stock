"""
올-웨더 지능형 모니터링 엔진 (검수 및 최적화 버전)
시장 레짐 분석, 상대적 수급 비중 분석, DB 영속성을 통합한 전략을 수행합니다.
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
        """시스템 초기화 및 상태 복구"""
        self.client = client
        self.config = config
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}
        self.check_interval = config.get("check_interval", 60)
        
        # 지표 계산기 (core/indicators.py)
        self.trend_calc = Indicators(period=config.get("trend_timeframe", {}).get("rsi_period", 14))
        self.entry_calc = Indicators(period=config.get("entry_timeframe", {}).get("rsi_period", 9))
        
        # DB 연결 및 보유 종목 복구
        self.db = TradeLogger()
        self.active_positions = self.db.load_open_positions()
        
        # 상태 추적 변수 (수급 캐시 구조 개선)
        self.status_log: Dict[str, Dict] = {}
        self.supply_cache: Dict[str, Dict] = {}  # {'code': {'f': 외인, 'i': 기관}}
        self.score_history: Dict[str, float] = {}
        
        # 시장 레짐 분석용 변수
        self.market_rsi = 50.0
        self.market_proxy_code = "069500"
        self.market_regime = "Unknown"
        self.market_rsi_history = deque(maxlen=20)
        self.breadth_history = deque(maxlen=20)
        
        self.dynamic_rsi_high = 60.0
        self.dynamic_rsi_low = 40.0
        self.dynamic_breadth_th = 1.5
        self.momentum_threshold = config.get("momentum_threshold", 10.0)

    # --- [데이터 수집 및 수급 분석 고도화] ---

    def _fetch_market_supply(self):
        """외인/기관 수급을 분리하여 캐싱합니다 (양매수 시너지 분석용)."""
        try:
            self.supply_cache = {}
            # 외인(6), 기관(7) 데이터를 각각 키값 'f', 'i'로 저장
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
        """거래량 대비 비중(상대강도)과 주체별 협응도를 분석하여 점수를 산출합니다."""
        total_vol = metrics.get('volume', 1)
        net_buy = metrics.get('net_buy', 0)
        
        # 1. 수급 비중 점수 (전체 거래량 중 순매수 비중이 2% 이상이면 100점)
        supply_ratio = (net_buy / total_vol) * 100
        base_score = min(100, max(0, supply_ratio * 50))

        # 2. 수급 주체 협응도 보너스 (외인/기관 양매수 시 가점)
        f_buy = metrics.get('f_buy', 0)
        i_buy = metrics.get('i_buy', 0)
        
        synergy_bonus = 0
        if f_buy > 0 and i_buy > 0: synergy_bonus = 20
        elif f_buy < 0 and i_buy < 0: synergy_bonus = -20

        return max(0, min(100, base_score + synergy_bonus))

    def _calculate_conviction_score(self, metrics: Dict) -> float:
        """모든 지표에 선형 매핑을 적용하여 유동적인 점수를 산출합니다."""
        weights = self._get_scoring_weights()
        score = 0.0

        # 1. Alpha (상대강도): +20 이상 시 100점 도달
        alpha_s = 50 + (metrics['alpha'] * 2.5)
        score += max(0, min(100, alpha_s)) * weights['alpha']

        # 2. 수급 (비중 및 시너지)
        score += self._calculate_supply_score(metrics) * weights['supply']

        # 3. VWAP (이격도): 현재가가 VWAP 대비 +2%면 100점
        if metrics['vwap'] > 0:
            dev_pct = (metrics['price'] / metrics['vwap'] - 1) * 100
            vwap_s = 50 + (dev_pct * 25)
            score += max(0, min(100, vwap_s)) * weights['vwap']

        # 4. 추세 (RSI): 50~70 구간을 50~100점으로 분할
        t_rsi = metrics['trend_rsi']
        t_score = 50 + ((t_rsi - 50) * 2.5) if t_rsi >= 50 else (t_rsi)
        score += max(0, min(100, t_score)) * weights['trend']

        return round(score, 1)

    # --- [시장 레짐 및 임계값 관리] ---

    def _update_market_status(self):
        """시장 지수와 확산 지표를 분석하여 레짐을 정의합니다."""
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
                self.market_regime = "과열 구간" if self.breadth_ratio > self.dynamic_breadth_th else "쏠림 구간"
            elif self.market_rsi < self.dynamic_rsi_low:
                self.market_regime = "위축 구간"
            else:
                self.market_regime = "평온 구간"
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

    # --- [매도 감시 및 메인 루프] ---

    def monitor_active_signals(self, stock_code, current_price, current_score):
        """보유 종목의 매도 조건을 감시하고 DB에 상태를 저장합니다."""
        if stock_code not in self.active_positions: return

        pos = self.active_positions[stock_code]
        if current_score < 50:
            profit = round((current_price / pos['buy_price'] - 1) * 100, 2)
            self.db.record_sell(pos['id'], current_price, profit, "Score Decay")
            print(f"📉 [가상 매도] {pos['stock_name']} | 수익률: {profit:+}% | 사유: 점수 하락")
            del self.active_positions[stock_code]

    def check_conditions(self, stock_code: str) -> Optional[Dict]:
        """개별 종목 지표 산출 및 점수 업데이트"""
        try:
            trend_data = self.client.market.get_minute_chart(stock_code, tic="60")
            entry_data = self.client.market.get_minute_chart(stock_code, tic="5")
            if not trend_data or len(entry_data) < 20: return None

            curr_price = entry_data[0]['close']
            curr_vol = sum(d['volume'] for d in entry_data)
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
            momentum = round(score - self.score_history.get(stock_code, score), 1)
            self.score_history[stock_code] = score
            
            th = self._get_dynamic_thresholds()
            status = "🔥강력추천" if score >= th['strong'] else ("👀관심" if score >= th['interest'] else "관망")
            if momentum >= self.momentum_threshold: status = "🚀수급폭발"

            self.status_log[stock_code] = {"price": curr_price, "score": score, "momentum": momentum, "reason": status}
            
            if score >= th['alert'] or momentum >= self.momentum_threshold:
                return {**metrics, "stock_code": stock_code, "score": score, "momentum": momentum}
            return None
        except: return None

    def update_target_stocks(self):
        """보유 종목 우선 감시 및 상위 종목 통합"""
        try:
            new_stocks = list(self.active_positions.keys())
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            etf_keywords = ('KODEX', 'TIGER', 'ACE', 'SOL', 'RISE', 'HANARO', 'PLUS', 'KoAct')
            
            for item in upper_list:
                code, name = item['stk_cd'], item['stk_nm']
                if any(kw in name for kw in etf_keywords): continue
                if code not in new_stocks: new_stocks.append(code)
                self.stock_names[code] = name
            self.stocks = new_stocks[:40]
            print(f"INFO: 감시 종목 갱신 완료 (총 {len(self.stocks)}개)")
        except Exception as e: print(f"종목 갱신 실패: {e}")

    def _is_monitoring_time(self) -> bool:
        now = datetime.now()
        if now.weekday() >= 5: return False
        return now.replace(hour=8, minute=30, second=0) <= now <= now.replace(hour=15, minute=40, second=0)

    def run(self):
        """메인 루프: 정렬 출력 및 자동 종료 포함"""
        self.update_target_stocks()
        while True:
            if not self._is_monitoring_time():
                print(f"\n🔔 장 종료. 시스템을 안전하게 중단합니다.")
                break

            self._update_market_status()
            self._fetch_market_supply()
            
            for stock in self.stocks: self.check_conditions(stock)
            sorted_stocks = sorted(self.stocks, key=lambda x: self.status_log.get(x, {}).get('momentum', 0), reverse=True)

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {self.market_regime}")
            print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
            print("-" * 55)

            for stock in sorted_stocks:
                res = self.check_conditions(stock)
                log = self.status_log.get(stock)
                if not log or "price" not in log: continue

                self.monitor_active_signals(stock, log['price'], log['score'])
                
                name = self.stock_names.get(stock, stock)
                m_str = f"+{log['momentum']}" if log['momentum'] > 0 else f"{log['momentum']}"
                print(f"{name:<10} | {log['score']:>5.1f} | {m_str:>6} | {log['reason']:<10}")
                
                if res:
                    if res['score'] >= 80 and stock not in self.active_positions:
                        buy_data = {"stock_code": stock, "stock_name": name, "buy_price": log['price'], 
                                    "buy_score": log['score'], "buy_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 
                                    "buy_regime": self.market_regime}
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
        print(f"🚀 [수급 폭발] {name}({res['stock_code']}) 점수 급상승! ({res['momentum']:+})")