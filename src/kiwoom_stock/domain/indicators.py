"""
[Core] 보조지표 및 수학적 계산 모듈 (Refactored)
- 전략(Strategy) 및 분석에서 사용하는 모든 기술적 지표 계산 로직을 포함합니다.
- 순수 함수(Pure Function) 형태로 구현되어 클래스 인스턴스 없이 사용 가능합니다.
- 데이터 입력 기준: 리스트의 마지막 요소([-1])가 '가장 최신' 데이터입니다.
"""

from typing import Dict, List
import statistics

# ==========================================
# 1. 기본 수학/통계 연산 (Basic Calculations)
# ==========================================


def calculate_roc(current: float, previous: float) -> float:
    """
    [Rate of Change] 변화율 계산 (%)
    :param current: 현재 값
    :param previous: 이전 값
    :return: 변화율 (previous가 0이면 0.0 반환)
    """
    if previous == 0:
        return 0.0
    return (current - previous) / previous * 100


def calculate_disparity(target: float, base: float) -> float:
    """
    [Disparity] 이격도 계산 (%)
    :param target: 비교 대상 (예: 주가, 단기 이평선)
    :param base: 기준 값 (예: VWAP, 장기 이평선)
    :return: 기준 대비 차이 비율 (base가 0이면 0.0 반환)
    """
    if base == 0:
        return 0.0
    return (target - base) / base * 100


def calculate_slope(current: float, previous: float, interval: int = 1) -> float:
    """
    [Slope] 기울기(강도) 계산
    :param current: 현재 값
    :param previous: 이전 값
    :param interval: 시간 간격 (기본 1)
    :return: 기울기 강도 (천분율 단위 등으로 변환 전 raw data)
    """
    if previous == 0:
        return 0.0
    # 정규화된 기울기 계산 (변화율과 유사하나 용도 구분을 위해 분리)
    return (current - previous) / previous * 1000 / interval


def calculate_volume_ratio(current_vol: float, past_vols: List[float]) -> float:
    """
    [Volume Ratio] 거래량 파워 계산
    :param current_vol: 현재 캔들 거래량
    :param past_vols: 과거 N개 캔들 거래량 리스트
    :return: 평균 대비 현재 거래량 비율 (1.0 = 평이, 2.0 = 2배 폭발)
    """
    if not past_vols:
        return 1.0

    avg_vol = sum(past_vols) / len(past_vols)
    if avg_vol == 0:
        return 1.0

    return current_vol / avg_vol


def calculate_volatility_ratio(range_val: float, atr: float) -> float:
    """
    [Volatility Ratio] ATR 대비 변동폭 비율
    :param range_val: 현재 변동폭 (예: 이격도 절대값)
    :param atr: ATR (평균 진폭)
    :return: ATR 대비 몇 배 확장되었는지 (과열 판단용)
    """
    if atr <= 0:
        return 0.0
    return range_val / atr

# ==========================================
# 2. 이동평균 및 추세 지표 (Moving Averages & Trend)
# ==========================================


def calculate_sma(series: List[float], period: int) -> float:
    """
    [SMA] 단순이동평균 계산
    :param series: 데이터 리스트 (시간순: [과거, ..., 최신])
    :param period: 기간
    :return: SMA 값 (데이터 부족 시 0.0)
    """
    if len(series) < period:
        return 0.0
    return sum(series[-period:]) / period


def calculate_ema(series: List[float], period: int) -> float:
    """
    [EMA] 지수이동평균 계산
    공식: (현재가 * 가중치) + (이전 EMA * (1 - 가중치))
    가중치: 2 / (period + 1)

    :param series: 데이터 리스트 (시간순: [과거, ..., 최신])
    :param period: 기간
    :return: 최신 EMA 값
    """
    if len(series) < period:
        return series[-1] if series else 0.0

    alpha = 2 / (period + 1)

    # 초기값: 앞부분 period개의 SMA
    ema = sum(series[:period]) / period

    # 이후 값들에 대해 EMA 누적 계산
    for i in range(period, len(series)):
        ema = (series[i] * alpha) + (ema * (1 - alpha))

    return round(ema, 2)

# ==========================================
# 3. 오실레이터 및 변동성 지표 (Oscillators & Volatility)
# ==========================================


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """
    [RSI] 상대강도지수 계산 (Wilder's Smoothing 적용)
    :param prices: 종가 리스트 (시간순: [과거, ..., 최신])
    :param period: 기간 (기본 14)
    :return: RSI 값 (0~100)
    """
    if len(prices) < period + 1:
        return 50.0

    # 전일 대비 변화량 계산
    deltas = []
    for i in range(1, len(prices)):
        deltas.append(prices[i] - prices[i-1])

    if not deltas:
        return 50.0

    # 초기 평균 상승/하락 (Wilder's 방식 초기값은 보통 SMA)
    gains = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # 이후 데이터에 대해 Smoothing 적용
    for i in range(period, len(deltas)):
        delta = deltas[i]
        gain = max(delta, 0)
        loss = max(-delta, 0)

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    return round(rsi, 2)


def calculate_bollinger_bands(
    prices: List[float],
    period: int = 20,
    std_dev: float = 2.0,
) -> Dict[str, float]:
    """
    [Bollinger Bands] 볼린저 밴드 계산
    :param prices: 종가 리스트 (시간순: [과거, ..., 최신])
    :param period: 기간
    :param std_dev: 표준편차 승수
    :return: {'upper': 상단, 'mid': 중단, 'lower': 하단}
    """
    if len(prices) < period:
        return {"upper": 0.0, "mid": 0.0, "lower": 0.0}

    # 가장 최근 period개의 데이터 사용
    target_prices = prices[-period:]

    sma = statistics.mean(target_prices)
    if len(target_prices) > 1:
        stdev = statistics.stdev(target_prices)
    else:
        stdev = 0.0

    return {
        "upper": round(sma + (std_dev * stdev), 2),
        "mid": round(sma, 2),
        "lower": round(sma - (std_dev * stdev), 2)
    }


def calculate_atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """
    [ATR] 평균 진폭 (Average True Range)
    :param highs: 고가 리스트 (시간순)
    :param lows: 저가 리스트 (시간순)
    :param closes: 종가 리스트 (시간순)
    :param period: 기간
    :return: ATR 값
    """
    if len(closes) < period + 1:
        return 0.0

    tr_list = []
    # TR 계산 (i=1부터 시작)
    for i in range(1, len(closes)):
        # TR = Max(H-L, |H-Cp|, |L-Cp|)
        h, l, cp = highs[i], lows[i], closes[i-1]
        tr = max(h - l, abs(h - cp), abs(l - cp))
        tr_list.append(tr)

    if not tr_list:
        return 0.0

    # Wilder's Smoothing 방식으로 ATR 계산
    # 초기 ATR은 SMA
    atr = sum(tr_list[:period]) / period

    # 이후 Smoothing
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period

    return round(atr, 2)


def calculate_atr_percent(
    highs: List[float],
    lows: List[float],
    closes: List[float],
    current_price: float,
    period: int = 14,
) -> float:
    """
    [ATR %] 주가 대비 변동성 비율 (Volatility Percentage)
    :param current_price: 명시적 현재가
    :return: (ATR / 현재가) * 100
    """
    if not closes or current_price == 0:
        return 0.0

    atr = calculate_atr(highs, lows, closes, period)

    return round((atr / current_price) * 100, 2)
