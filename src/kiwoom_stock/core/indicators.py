"""
RSI(Relative Strength Index) 및 보조지표 계산 모듈
"""

from typing import List, Dict, Tuple
import statistics
import math


class Indicators:
    """RSI 및 볼린저 밴드를 계산하는 클래스"""
    
    def __init__(self, period: int = 14):
        """
        RSI 계산기 초기화
        
        Args:
            period: RSI 계산 기간 (기본값: 14)
        """
        if period < 2:
            raise ValueError("RSI 기간은 최소 2 이상이어야 합니다.")
        self.period = period
    
    def calculate(self, prices: List[float]) -> float:
        """
        주가 데이터로부터 RSI를 계산합니다 (Wilder's Smoothing).
        
        Args:
            prices: 종가 리스트 (최신 가격이 앞에 오는 순서, 최소 period+1개 필요)
        
        Returns:
            RSI 값 (0-100)
        """
        if len(prices) < self.period + 1:
            return 50.0
        
        price_changes = []
        for i in range(len(prices) - 1):
            change = prices[i] - prices[i + 1]
            price_changes.append(change)
        
        price_changes.reverse()
        
        gains = [max(change, 0) for change in price_changes[:self.period]]
        losses = [max(-change, 0) for change in price_changes[:self.period]]
        
        avg_gain = statistics.mean(gains)
        avg_loss = statistics.mean(losses)
        
        for i in range(self.period, len(price_changes)):
            change = price_changes[i]
            gain = max(change, 0)
            loss = max(-change, 0)
            
            avg_gain = (avg_gain * (self.period - 1) + gain) / self.period
            avg_loss = (avg_loss * (self.period - 1) + loss) / self.period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return round(rsi, 2)

    def calculate_bollinger_bands(self, prices: List[float], period: int = 20, std_dev: float = 2.0) -> Dict[str, float]:
        """
        주가 데이터로부터 볼린저 밴드를 계산합니다.
        
        Args:
            prices: 종가 리스트 (최신 가격이 앞)
            period: 이동평균 기간 (기본값: 20)
            std_dev: 표준편차 승수 (기본값: 2.0)
            
        Returns:
            {'upper': 상단선, 'mid': 중간선, 'lower': 하단선}
        """
        if len(prices) < period:
            return {"upper": 0.0, "mid": 0.0, "lower": 0.0}
        
        target_prices = prices[:period]
        sma = statistics.mean(target_prices)
        stdev = statistics.stdev(target_prices) if len(target_prices) > 1 else 0
            
        return {
            "upper": round(sma + (std_dev * stdev), 2),
            "mid": round(sma, 2),
            "lower": round(sma - (std_dev * stdev), 2)
        }

    @staticmethod
    def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
        """
        ATR(Average True Range) 계산
        시장의 변동성 강도를 측정합니다.
        """
        if len(closes) < period + 1:
            return 0.0
        
        tr_list = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            tr_list.append(tr)
            
        # 와일더의 이동평균 방식 적용
        atr = statistics.mean(tr_list[-period:])
        return round(atr, 2)

    def calculate_atr_percent(self, high_series: List[float], low_series: List[float], close_series: List[float]) -> float:
        """ATR % (변동성 비율) 계산"""
        if len(close_series) < self.period + 1:
            return 3.0  # 데이터 부족 시 기본 과열 기준값(3%) 반환

        tr_list = []
        for i in range(1, len(close_series)):
            tr = max(
                high_series[i] - low_series[i],
                abs(high_series[i] - close_series[i-1]),
                abs(low_series[i] - close_series[i-1])
            )
            tr_list.append(tr)

        # TR의 이동평균 (ATR)
        atr = sum(tr_list[-self.period:]) / self.period
        curr_price = close_series[-1]
        
        return round((atr / curr_price) * 100, 2)

    def calculate_ema(self, series: List[float], period: int) -> float:
        """
        지수이동평균(EMA) 계산
        공식: (현재가 * 가중치) + (이전 EMA * (1 - 가중치))
        가중치: 2 / (period + 1)
        """
        if len(series) < period:
            return series[-1] if series else 0.0

        alpha = 2 / (period + 1)
        
        # 첫 번째 값은 단순 이동평균(SMA)으로 시작하거나 첫 종가로 시작
        ema = sum(series[:period]) / period 
        
        # 이후 값들에 대해 지수 가중치 적용
        for i in range(period, len(series)):
            ema = (series[i] * alpha) + (ema * (1 - alpha))
            
        return round(ema, 2)