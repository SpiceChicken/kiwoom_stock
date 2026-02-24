import pytest
from datetime import datetime
from unittest.mock import patch
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    """테스트 간 _kinetic_state 오염 방지용 클린 픽스처"""
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position():
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=50000, buy_score=85.0, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN"
    )

class TestKineticEntryLogic:
    def test_evaluate_perfect_ignition(self, strategy):
        data = SupplyData(stock_code="005930", total_score=90.0)
        data.forces = {"thrust": 1.5, "net_force": 2.0}
        strategy.history["005930"] = [80.0, 85.0]
        
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is True
        assert result["status"] == "🔥로켓발진 (Ignition)"

    def test_evaluate_fake_supply(self, strategy):
        data = SupplyData(stock_code="000660", total_score=80.0)
        data.forces = {"thrust": 2.5, "net_force": -0.5} 
        
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert result["status"] == "👀수급유입 (Engine On)"

class TestKineticTrailingStop:
    def test_exit_velocity_drop(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "neg_count": 0, "forces": []}
        
        forces = {"current_velocity": 8.0, "net_force": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        assert reason == "Kinetic Exit (Velocity Drop)"

    def test_exit_staircase_drop_defense(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "forces": []}
        test_forces = [-2.0, -1.5, +0.4] 
        
        reason = None
        for f in test_forces:
            forces = {"current_velocity": 9.5, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
            
        assert "Kinetic Exit" in reason

    def test_exit_v_shape_rebound_holding(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "forces": []}
        test_forces = [-3.0, -2.0, +10.0] 
        
        reason = None
        for f in test_forces:
            forces = {"current_velocity": 9.5, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
            
        assert reason is None

    @patch('kiwoom_stock.monitoring.strategy.datetime')
    def test_selective_swing_overnight(self, mock_datetime, strategy):
        mock_datetime.now.return_value = datetime(2026, 2, 10, 15, 25, 0)
        mock_datetime.combine.side_effect = datetime.combine
        
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 3.5, "neg_count": 0, "forces": []}
        
        forces = {"current_velocity": 3.5, "thrust": 2.5, "magnetic": 1.5, "net_force": 1.0}
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        
        assert reason is None
        assert pos.status == "OVERNIGHT"