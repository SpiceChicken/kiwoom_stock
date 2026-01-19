"""
올-웨더 지능형 모니터링 엔진
시장 지수(Regime)에 따라 동적으로 매수 기준을 변경하는 멀티 타임프레임 전략을 수행합니다.
"""

import time
from datetime import datetime
from typing import Dict, List, Optional

from ..api.parser import clean_numeric
from ..core.indicators import RSICalculator


class MultiTimeframeRSIMonitor:
    def __init__(self, client, config: Dict):
        """
        시스템 초기화
        
        Args:
            client: KiwoomClient 인스턴스 (인증 및 통신 담당)
            config: 모니터링 설정 (임계값, 주기 등)
        """
        self.client = client
        self.config = config
        self.stocks: List[str] = []
        self.stock_names: Dict[str, str] = {}
        self.check_interval = config.get("check_interval", 60)
        
        # 지표 계산기 초기화 (core/indicators.py 활용)
        self.trend_calc = RSICalculator(period=config.get("trend_timeframe", {}).get("rsi_period", 14))
        self.entry_calc = RSICalculator(period=config.get("entry_timeframe", {}).get("rsi_period", 9))
        
        # 상태 추적 변수
        self.is_bottom_zone: Dict[str, bool] = {}
        self.status_log: Dict[str, Dict] = {}
        self.supply_cache: Dict[str, int] = {}
        
        # 시장 레짐 정보 (KOSPI 지수 대용으로 KODEX 200 활용)
        self.market_rsi = 50.0
        self.market_proxy_code = "069500"  # KODEX 200

    # --- [시장 레짐 분석] ---

    def _update_market_status(self):
        """시장 전체의 심리 지수(RSI)를 업데이트합니다."""
        try:
            # client.market을 통해 정제된 차트 데이터 수신
            chart_data = self.client.market.get_minute_chart(self.market_proxy_code, tic="60")
            prices = [item['close'] for item in chart_data]
            if prices:
                self.market_rsi = self.trend_calc.calculate(prices)
        except Exception as e:
            print(f"시장 상태 업데이트 실패: {e}")

    def _get_dynamic_thresholds(self) -> tuple:
        """시장 RSI에 비례하여 매수 임계값을 유동적으로 산출합니다."""
        # 시장이 강할수록(RSI 높음) 매수 타점 완화, 약할수록 엄격한 종목 선별
        pct = (self.market_rsi * 0.3) + 5
        alpha = max(0, 15 - (self.market_rsi * 0.2))
        return round(pct, 2), round(alpha, 2)

    def _fetch_market_supply(self):
        """코스피 시장의 외인/기관 수급 데이터를 통합 조회하여 캐싱합니다."""
        try:
            self.supply_cache = {}
            # 외인(6), 기관(7) 순매수 합산
            for invsr in ["6", "7"]:
                items = self.client.market.get_investor_supply(market_tp="001", investor_tp=invsr)
                for item in items:
                    code = item.get("stk_cd", "").split('_')[0]
                    if not code: continue
                    # parser의 clean_numeric을 사용하여 안전하게 변환
                    qty = clean_numeric(item.get("netprps_qty", "0"))
                    self.supply_cache[code] = self.supply_cache.get(code, 0) + qty
        except Exception as e:
            print(f"수급 데이터 캐싱 실패: {e}")

    # --- [핵심 모니터링 로직] ---

    def check_conditions(self, stock_code: str) -> Optional[Dict]:
        """개별 종목의 매수 조건을 검증합니다."""
        try:
            # 1H(추세 확인) 및 5M(진입 시점) 데이터 조회
            trend_data = self.client.market.get_minute_chart(stock_code, tic="60")
            entry_data = self.client.market.get_minute_chart(stock_code, tic="5")
            
            if not trend_data or len(entry_data) < 40:
                return None

            t_prices = [item['close'] for item in trend_data]
            curr_price = entry_data[0]['close']
            
            # 1. 지표 계산
            trend_rsi = self.trend_calc.calculate(t_prices)
            curr_rsi = self.entry_calc.calculate([d['close'] for d in entry_data])
            alpha_rsi = curr_rsi - self.market_rsi
            
            # 2. VWAP(거래량 가중 평균 가격) 계산
            total_pv = sum(d['close'] * d['volume'] for d in entry_data)
            total_v = sum(d['volume'] for d in entry_data)
            vwap = total_pv / total_v if total_v > 0 else 0
            
            # 3. 동적 임계값 비교
            pct_th, alpha_th = self._get_dynamic_thresholds()
            net_buy = self.supply_cache.get(stock_code, 0)

            reason = "관망"
            # 1H 추세가 살아있어야 함
            if trend_rsi < 45:
                reason = "1H추세하락"
                self.is_bottom_zone[stock_code] = False
            # 과매도 구간(Percentile 기준) 진입 확인
            # (주석: _calculate_rsi_percentile 로직은 생략 가능하거나 core/indicators에 추가 가능)
            
            # 간소화된 조건 예시: 상대강도(Alpha) 및 VWAP 돌파 여부 확인
            is_above_vwap = curr_price > vwap
            is_smart_money = net_buy > 0
            is_stronger = alpha_rsi > alpha_th
            
            if is_above_vwap and is_smart_money and is_stronger:
                reason = "OK"
            
            self.status_log[stock_code] = {"alpha": alpha_rsi, "reason": reason}

            if reason == "OK":
                return {
                    "stock_code": stock_code, 
                    "price": curr_price, 
                    "vwap": vwap, 
                    "alpha": alpha_rsi,
                    "net_buy": net_buy
                }
            return None
        except:
            return None

    def update_target_stocks(self):
        """코스피 거래대금 상위 종목 갱신 (ETF 제외)"""
        try:
            upper_list = self.client.market.get_top_trading_value(market_tp="001")
            etf_keywords = ('KODEX', 'TIGER', 'ACE', 'SOL', 'RISE', 'HANARO', 'PLUS')
            
            new_stocks = []
            for item in upper_list:
                code, name = item['stk_cd'], item['stk_nm']
                if not any(kw in name for kw in etf_keywords):
                    new_stocks.append(code)
                    self.stock_names[code] = name
            
            self.stocks = new_stocks[:30]
        except Exception as e:
            print(f"종목 갱신 실패: {e}")

    def run(self):
        """메인 모니터링 루프 실행"""
        self.update_target_stocks()
        while True:
            self._update_market_status()
            self._fetch_market_supply()
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] KOSPI 지수 RSI: {self.market_rsi:.1f}")
            print(f"{'종목명':<10} | {'Alpha':<6} | {'상태':<12}")
            print("-" * 40)
            
            for stock in self.stocks:
                res = self.check_conditions(stock)
                log = self.status_log.get(stock, {})
                if log:
                    name = self.stock_names.get(stock, stock)
                    print(f"{name:<10} | {log['alpha']:>6.1f} | {log['reason']:<12}")
                
                if res:
                    self._send_alert(res)
            
            time.sleep(self.check_interval)

    def _send_alert(self, res: Dict):
        """신호 포착 시 알림 출력"""
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"\n🚀 [매수 신호] {name}({res['stock_code']})")
        print(f"- 현재가: {res['price']:,.0f}원 | VWAP: {res['vwap']:,.0f}")
        print(f"- 상대강도: {res['alpha']:.2f} | 수급: {res['net_buy']:,}주")
        print(f"{'-'*50}")