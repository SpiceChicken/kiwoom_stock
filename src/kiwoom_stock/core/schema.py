from dataclasses import dataclass, field
from typing import List, Dict, Optional

@dataclass
class PgmData:
    """프로그램 매매 데이터"""
    netprps_prica: float = 0.0    # 순매수금액
    all_trde_rt: float = 0.0      # 비중
    buy_cntr_amt: float = 0.0    # 매수금액
    sel_cntr_amt: float = 0.0    # 매도금액

@dataclass
class ForeignData:
    """외국계 창구 데이터"""
    netprps_prica: float = 0.0 # 순매수금액
    trde_prica: float = 1.0    # 거래금액 (DivByZero 방지 1.0)

@dataclass
class SupplyData:
    """
    [Core Data Structure] 종목별 수급 및 지표 데이터
    - 딕셔너리 대신 사용하여 타입 안전성과 가독성을 보장함
    """
    stock_code: str = ""
    strength: float = 100.0      # 체결강도
    vol_ratio: float = 0.0       # 거래량 비율
    price: float = 0.0           # 현재가
    vwap: float = 0.0            # VWAP
    prev_vwap: float = 0.0       # 이전 VWAP
    
    trend_rsi: float = 50.0      # 추세 RSI
    vol_factor: float = 1.0      # 거래량 팩터
    atr_percent: float = 0.5     # 변동성 지표
    
    # 이동평균선 (EMA)
    ema5: float = 0.0
    ema20: float = 0.0
    ema60: float = 0.0
    prev_ema60: float = 0.0
    
    # 시계열 데이터
    price_series: List[float] = field(default_factory=list)
    volume_series: List[float] = field(default_factory=list)
    chart_data: List[Dict] = field(default_factory=list)
    
    # 기타 메타데이터
    trde_qty: int = 0
    cur_prc: float = 0.0
    
    # 하위 데이터 객체
    pgm_data: PgmData = field(default_factory=PgmData)
    foreign_data: ForeignData = field(default_factory=ForeignData)

    # [New] 스무딩 된 세부 점수 저장용 (Alpha, Supply, VWAP, Trend)
    # analyzer.py에서 계산된 smoothed_metrics가 여기에 저장됩니다.
    score_detail: Dict[str, float] = field(default_factory=dict)
    total_score: float = 0.0