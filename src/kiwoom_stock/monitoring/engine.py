"""
올-웨더 지능형 모니터링 엔진
시장 지수(Regime)에 따라 동적으로 매수 기준을 변경하는 멀티 타임프레임 전략을 수행합니다.
"""

import time
import statistics
from datetime import datetime
from typing import Dict, List, Optional
from collections import deque

from ..api.parser import clean_numeric
from ..core.indicators import Indicators


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
        self.trend_calc = Indicators(period=config.get("trend_timeframe", {}).get("rsi_period", 14))
        self.entry_calc = Indicators(period=config.get("entry_timeframe", {}).get("rsi_period", 9))
        
        # 상태 추적 변수
        self.is_bottom_zone: Dict[str, bool] = {}
        self.status_log: Dict[str, Dict] = {}
        self.supply_cache: Dict[str, int] = {}
        
        # 시장 레짐 정보 (KOSPI 지수 대용으로 KODEX 200 활용)
        self.market_rsi = 50.0
        self.market_proxy_code = "069500"  # KODEX 200

        self.market_regime = "Unknown"
        self.breadth_ratio = 1.0  # 상승/하락 비율

        # 시장 데이터 히스토리 저장을 위한 데크 (최근 20회분 샘플링)
        self.market_rsi_history = deque(maxlen=20)
        self.breadth_history = deque(maxlen=20)
        
        # 기본 임계값 (백업용)
        self.dynamic_rsi_high = 60.0
        self.dynamic_rsi_low = 40.0
        self.dynamic_breadth_th = 1.5

        # 점수 히스토리 추적 (stock_code -> previous_score)
        self.score_history: Dict[str, float] = {}
        
        # 모멘텀 임계값 설정 (예: 한 주기 만에 10점 이상 상승 시 급등으로 간주)
        self.momentum_threshold = config.get("momentum_threshold", 10.0)

    # --- [시장 레짐 분석] ---

    def _update_market_status(self):
        try:
            # 1. 데이터 수집 (기존 동일)
            chart_data = self.client.market.get_minute_chart(self.market_proxy_code, tic="60")
            closes = [item['close'] for item in chart_data]
            self.market_rsi = self.trend_calc.calculate(closes)
            
            breadth = self.client.market.get_market_breadth(market_tp="001")
            self.breadth_ratio = breadth['rising'] / max(1, breadth['falling'])

            # 2. 히스토리 업데이트
            self.market_rsi_history.append(self.market_rsi)
            self.breadth_history.append(self.breadth_ratio)

            # 3. 동적 임계값 계산 (샘플이 충분할 때만)
            if len(self.market_rsi_history) >= 5:
                rsi_avg = statistics.mean(self.market_rsi_history)
                rsi_std = statistics.stdev(self.market_rsi_history)
                breadth_avg = statistics.mean(self.breadth_history)

                # RSI 상단: 평균보다 0.5표준편차 높을 때 (상위 약 30% 지점)
                self.dynamic_rsi_high = rsi_avg + (0.5 * rsi_std)
                # RSI 하단: 평균보다 0.5표준편차 낮을 때 
                self.dynamic_rsi_low = rsi_avg - (0.5 * rsi_std)
                # Breadth 상단: 최근 평균의 1.2배 수준
                self.dynamic_breadth_th = breadth_avg * 1.2
                
                # 최소/최대 안전장치 (너무 극단적인 값 방지)
                self.dynamic_rsi_high = max(55, min(70, self.dynamic_rsi_high))
                self.dynamic_rsi_low = max(30, min(45, self.dynamic_rsi_low))
                self.dynamic_breadth_th = max(1.2, min(2.5, self.dynamic_breadth_th))

            # 4. 개선된 시장 레짐 정의
            if self.market_rsi > self.dynamic_rsi_high:
                if self.breadth_ratio > self.dynamic_breadth_th:
                    self.market_regime = "과열 구간 (전체 장세 강세)"
                else:
                    self.market_regime = "쏠림 구간 (지수주 위주 독주)"
            elif self.market_rsi < self.dynamic_rsi_low:
                self.market_regime = "위축 구간 (반등 대기)"
            else:
                self.market_regime = "평온 구간 (박스권)"

            print(f"DEBUG: 임계값 변화 [RSI-H: {self.dynamic_rsi_high:.1f}, Breadth-TH: {self.dynamic_breadth_th:.2f}]")
            
        except Exception as e:
            print(f"시장 분석 실패: {e}")

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

    def _get_scoring_weights(self) -> Dict[str, float]:
        """현재 시장 레짐에 따른 지표 가중치를 반환합니다."""
        if "과열" in self.market_regime or "쏠림" in self.market_regime:
            return {"alpha": 0.4, "supply": 0.2, "vwap": 0.2, "trend": 0.2}
        elif "위축" in self.market_regime:
            return {"alpha": 0.2, "supply": 0.4, "vwap": 0.2, "trend": 0.2}
        else: # 평온 구간
            return {"alpha": 0.3, "supply": 0.3, "vwap": 0.3, "trend": 0.1}

    def _calculate_conviction_score(self, metrics: Dict) -> float:
        """
        개별 종목의 지표를 종합하여 0~100점 사이의 점수를 산출합니다.
        """
        weights = self._get_scoring_weights()
        score = 0.0

        # 1. Alpha 점수 (상대강도) : 0~20 이상일 때 비례하여 점수 부여
        alpha_val = max(0, min(20, metrics['alpha']))
        score += (alpha_val / 20 * 100) * weights['alpha']

        # 2. 수급 점수 : 당일 순매수 여부 및 강도 (단순화: 매수면 100점)
        supply_score = 100 if metrics['net_buy'] > 0 else 0
        score += supply_score * weights['supply']

        # 3. VWAP 점수 : 현재가가 VWAP 위에 있으면 100점
        vwap_score = 100 if metrics['price'] > metrics['vwap'] else 0
        score += vwap_score * weights['vwap']

        # 4. 추세 점수 : 1H RSI가 50 이상이면 강세 추세로 인정
        trend_score = 100 if metrics['trend_rsi'] > 50 else 0
        score += trend_score * weights['trend']

        return round(score, 1)

    def _get_dynamic_thresholds(self) -> Dict[str, float]:
        """시장의 온도에 따라 알림을 보낼 기준 점수(Threshold)를 결정합니다."""
        # 기본값
        base_thresholds = {"strong": 80.0, "alert": 70.0, "interest": 60.0}
        
        if "과열" in self.market_regime:
            # 시장이 너무 뜨거울 때는 기준을 높여 '찐주도주'만 선별
            return {"strong": 85.0, "alert": 75.0, "interest": 65.0}
        
        elif "위축" in self.market_regime:
            # 시장이 공포에 질렸을 때는 기준을 낮춰 '역발상 수급주' 포착
            return {"strong": 75.0, "alert": 65.0, "interest": 55.0}
        
        return base_thresholds

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

            # 산출된 지표들 묶기
            metrics = {
                "alpha": alpha_rsi,
                "net_buy": self.supply_cache.get(stock_code, 0),
                "price": curr_price,
                "vwap": vwap,
                "trend_rsi": trend_rsi
            }

            # 점수 계산
            conviction_score = self._calculate_conviction_score(metrics)
            
            # 2. 모멘텀 계산
            prev_score = self.score_history.get(stock_code, conviction_score)
            momentum = round(conviction_score - prev_score, 1)
            
            # 히스토리 업데이트 (다음 주기를 위해 현재 점수 저장)
            self.score_history[stock_code] = conviction_score
            
            # 3. 비즈니스 결정: 레짐 기반 동적 임계값 적용
            th = self._get_dynamic_thresholds()
            
            status = "관망"
            if conviction_score >= th['strong']:
                status = "🔥강력추천"
            elif momentum >= self.momentum_threshold:
                status = "🚀수급폭발" # 점수가 낮아도 급상승 중이면 알림
            elif conviction_score >= th['interest']:
                status = "👀관심"
            
            # 로그 기록 시 모멘텀 추가
            self.status_log[stock_code] = {
                "score": conviction_score,
                "momentum": momentum,
                "reason": status
            }

            # 4. 알림 조건 (점수가 기준 이상이거나, 모멘텀이 폭발적일 때)
            if conviction_score >= th['alert'] or momentum >= self.momentum_threshold:
                return {
                    **metrics, 
                    "stock_code": stock_code, 
                    "score": conviction_score, 
                    "momentum": momentum
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
        self.update_target_stocks()
        while True:
            self._update_market_status()
            self._fetch_market_supply()
            
            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 시장 레짐: {self.market_regime}")
            print(f"{'종목명':<10} | {'점수':<5} | {'모멘텀':<6} | {'상태':<10}")
            print("-" * 50)
            
            # 모멘텀이 높은 순서대로 정렬하여 출력 (급등주 우선 포착)
            sorted_stocks = sorted(
                self.stocks, 
                key=lambda x: self.status_log.get(x, {}).get('momentum', 0), 
                reverse=True
            )

            for stock in sorted_stocks:
                res = self.check_conditions(stock)
                log = self.status_log.get(stock, {})
                if log:
                    name = self.stock_names.get(stock, stock)
                    # 모멘텀이 양수면 + 기호 표시
                    m_str = f"+{log['momentum']}" if log['momentum'] > 0 else f"{log['momentum']}"
                    print(f"{name:<10} | {log['score']:>5.1f} | {m_str:>6} | {log['reason']:<10}")
                
                # 급격한 모멘텀 발생 시 즉시 알림
                if res and res['momentum'] >= self.momentum_threshold:
                    self._send_momentum_alert(res)
            
            time.sleep(self.check_interval)

    def _send_momentum_alert(self, res: Dict):
        """점수 급등 알림 전용 메서드"""
        name = self.stock_names.get(res['stock_code'], res['stock_code'])
        print(f"\n🚀 [수급 포착] {name}({res['stock_code']}) 점수 급상승!")
        print(f"- 현재점수: {res['score']} ({res['momentum']:+})")
        print(f"- 가격: {res['price']:,.0f}원 | 수급: {res['net_buy']:,}주")
        print(f"{'-'*50}")
    