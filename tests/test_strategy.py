import pytest
from datetime import datetime
from unittest.mock import patch
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
        buy_price=50000, buy_score=85.0, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=1.5
    )

class TestKineticEntryLogic:
    def test_evaluate_perfect_ignition(self, strategy):
        """Case 1: 완벽한 발진 (속도와 추진력이 모두 양수)"""
        data = SupplyData(stock_code="005930")
        data.total_score = 90.0
        # 💥 [핵심 수정] current_velocity를 양수로 주입하여 '추세 돌파' 조건 충족
        data.forces = {"thrust": 1.5, "net_force": 2.0, "current_velocity": 5.0}
        strategy.history["005930"] = [80.0, 85.0]
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is True
        # 상태 메시지 유연 검증
        assert any(k in result["status"] for k in ["Ignition", "로켓", "발진", "추세돌파", "Uptrend"])

    def test_evaluate_fake_supply(self, strategy):
        """Case 2: 가짜 수급 (속도는 없고 추진력만 있음 -> 매수 보류)"""
        data = SupplyData(stock_code="000660")
        data.total_score = 80.0
        # current_velocity 없음 (0.0) -> 아직 물 밑에 있음
        data.forces = {"thrust": 2.5, "net_force": -0.5} 
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        # 변경된 상태 메시지 "⚠️엔진점화" 검증
        assert "엔진점화" in result["status"] or "Ignition only" in result["status"]

    def test_evaluate_dict_keys_unification(self, strategy):
        data = SupplyData(stock_code="005930")
        data.total_score = 90.0
        data.forces = {"thrust": 1.5}
        strategy.history["005930"] = [80.0, 85.0]
        
        result = strategy.evaluate(data)
        assert "score_detail" not in result
        assert "forces" in result

class TestKineticTrailingStop:
    def test_exit_velocity_drop(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "neg_count": 0, "forces": []}
        
        forces = {"current_velocity": 8.0, "net_force": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        assert reason == "Kinetic Exit (Velocity Drop)"

    def test_exit_staircase_drop_defense(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "neg_count": 0, "forces": []}
        test_forces = [-2.0, -1.5, +0.4] 
        
        reason = None
        for f in test_forces:
            forces = {"current_velocity": 9.5, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
            
        assert reason is not None and "Kinetic Exit" in reason

    def test_exit_v_shape_rebound_holding(self, strategy):
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_v": 10.0, "neg_count": 0, "forces": []}
        test_forces = [-3.0, -2.0, +10.0] 
        
        reason = None
        for f in test_forces:
            forces = {"current_velocity": 9.5, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
            
        assert reason is None