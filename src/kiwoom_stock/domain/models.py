"""Shared domain data models with legacy import compatibility.

These classes keep the current field names, defaults, and calculation behavior.
Legacy modules re-export them during the migration window.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


@dataclass
class PgmData:
    """Program trading data."""

    netprps_prica: float = 0.0
    all_trde_rt: float = 0.0
    buy_cntr_amt: float = 0.0
    sel_cntr_amt: float = 0.0


@dataclass
class ForeignData:
    """Foreign broker flow data."""

    netprps_prica: float = 0.0
    trde_prica: float = 1.0


@dataclass
class SupplyData:
    """Per-symbol supply, price, and indicator snapshot."""

    stock_code: str = ""
    strength: float = 100.0
    prev_strength_5m: float = 100.0
    vol_ratio: float = 0.0
    price: float = 0.0
    vwap: float = 0.0
    prev_vwap: float = 0.0

    trend_rsi: float = 50.0
    vol_factor: float = 1.0
    atr_percent: float = 0.5
    down_atr_percent: float = 0.5

    ema5: float = 0.0
    ema20: float = 0.0
    ema60: float = 0.0
    prev_ema60: float = 0.0

    price_series: List[float] = field(default_factory=list)
    volume_series: List[float] = field(default_factory=list)
    chart_data: List[Dict] = field(default_factory=list)

    trde_qty: int = 0
    cur_prc: float = 0.0
    mac: float = 100000.0

    pgm_data: PgmData = field(default_factory=PgmData)
    foreign_data: ForeignData = field(default_factory=ForeignData)
    forces: Dict[str, float] = field(default_factory=dict)


class MarketRegime(Enum):
    STABLE_BULL = "안정적 강세장"
    VOLATILE_BULL = "변동성 강세장"
    QUIET_BEAR = "조용한 하락장"
    PANIC_BEAR = "패닉 하락장"
    NEUTRAL = "평온 구간"
    UNKNOWN = "Unknown"


@dataclass
class Position:
    id: int
    stock_code: str
    stock_name: str
    buy_price: float
    buy_time: str
    buy_regime: str
    status: str = "OPEN"
    thrust: float = 0.0
    gravity: float = 0.0
    drag: float = 0.0
    magnetic: float = 0.0
    jerk: float = 0.0
    impulse: float = 0.0
    net_force: float = 0.0
    sell_price: Optional[float] = None
    profit_rate: Optional[float] = None
    sell_time: Optional[str] = None
    sell_reason: Optional[str] = None
    atr_percent: float = 0.5
    down_atr_percent: float = 0.5

    @property
    def calc_profit_rate(self) -> float:
        """Return the percentage PnL against the buy price."""

        if not self.buy_price or not self.sell_price:
            return 0.0
        return round((self.sell_price / self.buy_price - 1) * 100, 2)
