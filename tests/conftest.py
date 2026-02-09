# tests/conftest.py
import pytest
from unittest.mock import MagicMock
from datetime import datetime
from kiwoom_stock.core.schema import SupplyData, PgmData, ForeignData
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.monitoring.manager import StockManager
from kiwoom_stock.core.types import MarketRegime

@pytest.fixture
def mock_strategy_config():
    """테스트용 전략 설정"""
    return {
        "debug_mode": True,
        "score_decay_rate": 0.25,
        "target_profit_rate": 0.03,
        "stop_loss_rate": -0.03,
        "total_loss_limit": -50.0,
        "entry_deadline": "15:00",
        "day_trade_exit_time": "15:30",
        "momentum_threshold": 10.0,
        "regimes": {
            "default": {
                "thresholds": { "strong": 87.0, "strong_supply": 82.0, "alert": 75.0, "interest": 65.0 }
            }
        }
    }

# [수정] strategy 픽스처를 이곳으로 옮겨 모든 테스트 파일이 공유하도록 함
@pytest.fixture
def strategy(mock_strategy_config, mocker):
    """
    [해결책] MagicMock 대신 실제 datetime을 상속받아 now()만 조작합니다.
    이렇게 하면 combine, strptime 등 다른 메서드는 실제처럼 동작하여 TypeError가 사라집니다.
    """
    real_datetime = datetime

    class MockDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            # 테스트 중에는 무조건 '2026-02-10 10:00:00' (장 중)으로 고정
            return real_datetime(2026, 2, 10, 10, 0, 0)

    # kiwoom_stock.monitoring.strategy 모듈 안의 'datetime' 클래스를 
    # 우리가 만든 MockDatetime 클래스로 바꿔치기 합니다.
    mocker.patch("kiwoom_stock.monitoring.strategy.datetime", MockDatetime)
    
    st = TradingStrategy(mock_strategy_config)
    st.update_context(MarketRegime.STABLE_BULL)
    return st

@pytest.fixture
def sample_supply_data():
    """v2.6 SupplyData 기반 가짜 데이터 (상승 추세)"""
    data = SupplyData(
        stock_code="005930",
        strength=120.0,
        vol_ratio=2.5,
        price=80500.0,
        cur_prc=80500.0,
        vwap=80000.0,
        prev_vwap=79000.0,
        alpha_score=0.0,
        trend_rsi=65.0, 
        vol_factor=1.5,
        atr_percent=2.0,
        ema5=80200.0,
        ema20=79500.0,
        ema60=78000.0,
        prev_ema60=77500.0,
        market_total_amount=10000000000.0
    )
    data.pgm_data = PgmData(net_amt=50.0)
    data.foreign_data = ForeignData(netprps_prica=30.0)
    
    # [수정] 가격을 확실한 우상향 패턴으로 변경 (과거 -> 현재)
    data.price_series = [78000, 78500, 79000, 79500, 80000, 80500]
    data.volume_series = [1000, 1200, 1500, 1300, 2000, 2500]

    # Strategy 테스트용으로 쓸 때 (이미 분석이 끝난 상태를 가정)
    data.score_detail = {'alpha': 90.0, 'supply': 80.0, 'vwap': 95.0, 'trend': 85.0}
    data.total_score = 87.5  # 가중 기하평균 대략 계산값
    
    return data

@pytest.fixture
def sample_position():
    return Position(
        id=1,
        stock_code="005930",
        stock_name="삼성전자",
        buy_price=80000.0,
        buy_score=90.0,
        alpha_score=85.0,
        supply_score=80.0,
        vwap_score=95.0,
        trend_score=90.0,
        buy_time="2026-02-08 10:00:00",
        buy_regime="STABLE_BULL",
        status="OPEN",
        atr_percent=1.5
    )

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.record_buy.return_value = 1
    db.get_last_sell_time.return_value = None
    db.load_open_positions.return_value = {}
    return db