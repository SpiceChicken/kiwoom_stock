import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position(atr=1.0, down_atr=0.5):
    """테스트 계산을 직관적으로 만들기 위해 매수가 10,000원 통제 및 Up/Down ATR 분리"""
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=10000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=atr, 
        down_atr_percent=down_atr
    )

class TestKineticEntryLogic:
    """📌 1. 진입 로직 (evaluate 함수) 3대 쉴드 및 동역학 진입 검증"""

    def test_thrust_hurdle(self, strategy):
        """Test Case 1: 물리적 자연 냉각 허들 검증 (Thrust Hurdle)"""
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 0.4, "jerk": 0.5, "current_velocity": 1.0, "impulse": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "엔진점화" in result["status"] or "Ignition only" in result["status"]

    def test_submarine_trap(self, strategy):
        """Test Case 2: 잠수함 트랩 (Submarine Trap) 방어"""
        data = SupplyData(stock_code="005930")
        # 수면 아래(gravity == 0.0) 조건
        data.forces = {"thrust": 1.6, "gravity": 0.0, "jerk": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "수면 아래 폭발" in result["status"] or "Submarine Trap" in result["status"]

    def test_fake_breakout_trap(self, strategy):
        """Test Case 3: 텅 빈 가속도 (Fake Breakout) 방어"""
        data = SupplyData(stock_code="005930")
        # 추진력은 좋으나(1.2), 거래대금(impulse)이 빈약함(0.5)
        data.forces = {"thrust": 1.2, "impulse": 0.5, "jerk": 1.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "빈 껍데기 가속도 차단" in result["status"] or "Fake Breakout" in result["status"]

    def test_perfect_entry(self, strategy):
        """Test Case 4: 완벽한 추세돌파 진입 (The Perfect Entry)"""
        data = SupplyData(stock_code="005930")
        # 모든 쉴드를 통과하고 허들(thrust > 0.5)을 넘음
        data.forces = {"thrust": 0.8, "jerk": 0.2, "current_velocity": 1.0, "impulse": 2.0}
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is True
        assert "추세돌파" in result["status"]
        assert "score" not in result

class TestHeavyExitLogic:
    """📌 2. 청산 로직 (get_exit_reason 함수) 하이브리드 듀얼 방어망 검증"""

    def test_zero_time_bailout(self, strategy):
        """Test Case 5: 급브레이크 조기 탈출 (Zero-Time Bail-out)"""
        pos = _setup_mock_position() 
        # 조건 B: jerk <= -0.5 and thrust < 1.0
        forces = {"jerk": -0.6, "thrust": 0.5} 
        # 10000 -> 9950 (-0.5% 하락)
        reason = strategy.get_exit_reason(pos, current_price=9950, forces=forces)
        assert reason is not None
        assert "Bail-out" in reason

    def test_up_atr_trailing_stop_hold(self, strategy):
        """Test Case 6: Up-ATR 동적 이익보존망 (Trailing Stop) - 털림 방어"""
        # atr=1.0, down_atr=0.5 -> Up-ATR = 0.5
        # 트레일링 스탑 발동: +1.75% 이상, 방어선: 고점 대비 -1.5%
        pos = _setup_mock_position(atr=1.0, down_atr=0.5) 
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10200 # 고점 10,200원 (+2.0% 돌파)
        }
        
        # 고점 대비 -1.4% 하락 (10200 * 0.986 = 10057.2원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=10057.2, forces=forces)
        
        assert reason is None, "Up-ATR 방어선(-1.5%) 이내이므로 휩쏘를 견뎌야 합니다."

    def test_up_atr_trailing_stop_exit(self, strategy):
        """Test Case 7: Up-ATR 동적 이익보존망 - 최종 익절"""
        pos = _setup_mock_position(atr=1.0, down_atr=0.5)
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10200 # 고점 10,200원
        }
        
        # 고점 대비 -1.6% 하락 (10200 * 0.984 = 10036.8원) -> 방어선(-1.5%) 붕괴
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=10036.8, forces=forces)
        
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_initial_stop_loss(self, strategy):
        """Test Case 8: 초기 생존망 (Initial Stop Loss)"""
        # Down-ATR = 0.5 -> 스탑로스 리미트: -(0.5 * 5.0) = -2.5%
        pos = _setup_mock_position(atr=1.0, down_atr=0.5)
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10000 
        }
        
        # 진입가 대비 -2.6% 이탈 (10000 -> 9740원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=9740, forces=forces)
        
        assert reason is not None
        assert "Stop Loss" in reason
        assert "-2.60%" in reason