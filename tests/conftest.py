from datetime import datetime
from pathlib import Path
import socket
import threading
from unittest.mock import MagicMock

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_ORIGINAL_NETWORK_FUNCTIONS = {
    "create_connection": socket.create_connection,
    "getaddrinfo": socket.getaddrinfo,
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
}


def _deny_external_network(*args, **kwargs):
    raise AssertionError("tests must use a fake transport; external network access was attempted")


def pytest_sessionstart(session):
    """Install the network tripwire before pytest imports any test modules."""
    socket.create_connection = _deny_external_network
    socket.getaddrinfo = _deny_external_network
    socket.socket.connect = _deny_external_network
    socket.socket.connect_ex = _deny_external_network


def pytest_sessionfinish(session, exitstatus):
    socket.create_connection = _ORIGINAL_NETWORK_FUNCTIONS["create_connection"]
    socket.getaddrinfo = _ORIGINAL_NETWORK_FUNCTIONS["getaddrinfo"]
    socket.socket.connect = _ORIGINAL_NETWORK_FUNCTIONS["connect"]
    socket.socket.connect_ex = _ORIGINAL_NETWORK_FUNCTIONS["connect_ex"]


@pytest.fixture(scope="session", autouse=True)
def repository_side_effect_guard():
    """Fail the suite if a test creates the legacy cwd database or leaks a worker."""
    root_database = REPOSITORY_ROOT / "trades.db"
    threads_before = set(threading.enumerate())

    assert not root_database.exists(), f"pre-existing repository database blocks hermetic tests: {root_database}"
    yield

    assert not root_database.exists(), f"test suite created repository database: {root_database}"
    leaked_threads = [
        thread
        for thread in threading.enumerate()
        if thread not in threads_before and thread.is_alive()
    ]
    assert not leaked_threads, f"test suite leaked worker threads: {leaked_threads}"


@pytest.fixture(autouse=True)
def block_external_network(monkeypatch):
    """Block socket creation and DNS resolution for every collected test."""
    monkeypatch.setattr(socket, "create_connection", _deny_external_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_external_network)
    monkeypatch.setattr(socket.socket, "connect", _deny_external_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_external_network)


@pytest.fixture
def mock_strategy_config():
    """테스트용 전략 설정"""
    return {
        "debug_mode": True,
        "score_decay_rate": 0.25,
        "cumulative_trade_return_score_floor": -50.0,
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
    from kiwoom_stock.core.types import MarketRegime
    from kiwoom_stock.monitoring.strategy import TradingStrategy

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
    from kiwoom_stock.core.schema import ForeignData, PgmData, SupplyData

    data = SupplyData(
        stock_code="005930",
        strength=120.0,
        vol_ratio=2.5,
        price=80500.0,
        cur_prc=80500.0,
        vwap=80000.0,
        prev_vwap=79000.0,
        trend_rsi=65.0, 
        vol_factor=1.5,
        atr_percent=0.5,
        ema5=80200.0,
        ema20=79500.0,
        ema60=78000.0,
        prev_ema60=77500.0,
    )
    data.pgm_data = PgmData(netprps_prica=50.0)
    data.foreign_data = ForeignData(netprps_prica=30.0)
    
    # [수정] 가격을 확실한 우상향 패턴으로 변경 (과거 -> 현재)
    data.price_series = [78000, 78500, 79000, 79500, 80000, 80500]
    data.volume_series = [1000, 1200, 1500, 1300, 2000, 2500]

    # 물리 엔진의 Vector Forces 딕셔너리로 교체
    data.forces = {
        "thrust": 1.25,
        "gravity": -0.85,
        "drag": -0.15,
        "magnetic": 0.40,
        "jerk": 0.80,
        "impulse": 0.0,
        "net_force": 1.45,
        "current_velocity": 2.50
    }
    
    return data

@pytest.fixture
def sample_position():
    from kiwoom_stock.monitoring.manager import Position

    return Position(
        id=1,
        stock_code="005930",
        stock_name="삼성전자",
        buy_price=80000.0,
        buy_time="2026-02-08 10:00:00",
        buy_regime="STABLE_BULL",
        status="OPEN",
        atr_percent=0.5,
        down_atr_percent=0.5
    )

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.record_buy.return_value = 1
    db.get_last_sell_time.return_value = None
    db.load_open_positions.return_value = {}
    return db
