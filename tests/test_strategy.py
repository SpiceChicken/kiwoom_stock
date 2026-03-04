# [PATCH] tests/test_strategy.py 전면 덮어쓰기

import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position():
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=10000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=0.5, 
        down_atr_percent=0.5,
    )

class TestKineticEntryLogic:
    def test_thrust_hurdle(self, strategy):
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 0.4, "jerk": 0.5, "current_velocity": 1.0, "impulse": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "엔진점화" in result["status"] or "Ignition only" in result["status"]

    def test_submarine_trap(self, strategy):
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 1.6, "gravity": 0.0, "jerk": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "수면 아래 폭발" in result["status"] or "Submarine Trap" in result["status"]

    def test_fake_breakout_trap(self, strategy):
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 1.2, "impulse": 0.5, "jerk": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "빈 껍데기 가속도 차단" in result["status"] or "Fake Breakout" in result["status"]

    def test_perfect_entry(self, strategy):
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 0.8, "jerk": 0.2, "current_velocity": 1.0, "impulse": 2.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is True
        assert "추세돌파" in result["status"]
        assert "score" not in result

class TestHeavyExitLogic:
    def test_zero_time_bailout(self, strategy):
        pos = _setup_mock_position() 
        forces = {"jerk": -0.6, "thrust": 0.5} 
        reason = strategy.get_exit_reason(pos, current_price=9950, forces=forces)
        assert reason is not None
        assert "Bail-out" in reason

    def test_impulse_privilege_hold(self, strategy):
        pos = _setup_mock_position() 
        
        # 💥 [경계값 갭 확대] 10,500원(+5.0%)으로 명확하게 이익보존망 달성시킴
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10500, 
            "entry_impulse": 3.5
        }
        
        # 고점 대비 -1.5% 하락 (10500 * 0.985 = 10342.5원)
        # down_atr_percent * 3.0 이내이므로 버텨야 함
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=10350.0, forces=forces)
        
        assert reason is None, "대포알 종목이므로 down_atr_percent * 3.0 한계까지 버텨야 합니다."

    def test_impulse_privilege_exit(self, strategy):
        pos = _setup_mock_position()
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10500, 
            "entry_impulse": 3.5
        }
        
        # 💥 [경계값 갭 확대] 고점 대비 확실하게 -3.0% 찢어서 하락시킴 (10500 * 0.97 = 10185.0원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=10185.0, forces=forces)
        
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_initial_stop_loss(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10050, 
            "entry_impulse": 1.0
        }
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=9690, forces=forces)
        assert reason is not None
        assert "Stop Loss" in reason