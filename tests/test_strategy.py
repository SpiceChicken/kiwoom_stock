import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    """테스트 간 상태 캐시 오염을 막기 위한 클린 픽스처"""
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position(atr=1.0):
    """테스트 계산을 직관적으로 만들기 위해 매수가 10,000원, ATR 1.0%로 통제"""
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=10000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=atr
    )

class TestKineticEntryLogic:
    """📌 1. 진입 로직 (evaluate 함수) 검증 명세"""

    def test_thrust_hurdle(self, strategy):
        """Test Case 1: 물리적 자연 냉각 허들 검증 (Thrust Hurdle)"""
        data = SupplyData(stock_code="005930")
        # 쉴드에 안 걸리지만 thrust가 0.4로 허들(0.5) 미달
        data.forces = {"thrust": 0.4, "jerk": 0.5, "current_velocity": 1.0, "impulse": 1.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False, "허들을 넘지 못하면 매수 금지"
        assert "엔진점화" in result["status"] or "Ignition only" in result["status"]

    def test_submarine_trap(self, strategy):
        """Test Case 2: 잠수함 트랩 (Submarine Trap) 방어"""
        data = SupplyData(stock_code="005930")
        # 강한 수급이나 수면 아래(gravity <= 0.0)
        data.forces = {"thrust": 1.6, "gravity": 0.0, "jerk": 1.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "수면 아래 폭발" in result["status"] or "Submarine Trap" in result["status"]

    def test_fake_breakout_trap(self, strategy):
        """Test Case 3: 텅 빈 가속도 (Fake Breakout) 방어"""
        data = SupplyData(stock_code="005930")
        # 1.2의 추진력이지만 대포알(Impulse)이 0.5 부족
        data.forces = {"thrust": 1.2, "impulse": 0.5, "jerk": 1.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "빈 껍데기 가속도 차단" in result["status"] or "Fake Breakout" in result["status"]

    def test_perfect_entry(self, strategy):
        """Test Case 4: 완벽한 추세돌파 진입 (The Perfect Entry)"""
        data = SupplyData(stock_code="005930")
        # 쉴드를 모두 회피하고 허들(0.5)을 넘음
        data.forces = {"thrust": 0.8, "jerk": 0.2, "current_velocity": 1.0, "impulse": 2.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is True
        assert "추세돌파" in result["status"]
        assert "score" not in result, "Score 객체가 없어야 함"


class TestHeavyExitLogic:
    """📌 2. 청산 로직 (get_exit_reason 함수) 검증 명세"""

    def test_zero_time_bailout(self, strategy):
        """Test Case 5: 급브레이크 조기 탈출 (Zero-Time Bail-out)"""
        pos = _setup_mock_position() 
        # 현재 손실권(-0.5%), 가속도 처박힘(-0.6), 추진력 꺼짐(0.5)
        forces = {"jerk": -0.6, "thrust": 0.5} 
        
        # 10000 -> 9950 (-0.5% 하락)
        reason = strategy.get_exit_reason(pos, current_price=9950, forces=forces)
        
        assert reason is not None
        assert "Bail-out" in reason
        assert "-0.50%" in reason

    def test_impulse_privilege_hold(self, strategy):
        """Test Case 6: 대포알 우대 정책 (Impulse Privilege Trailing Stop) - 털림 방어"""
        pos = _setup_mock_position(atr=1.0) 
        
        # 진입 시 Impulse = 3.5 주입, 고점 10,150원(+1.5%) 달성 상태 모킹
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10150, 
            "entry_impulse": 3.5
        }
        
        # 고점 대비 -1.6% 하락 (10150 * (1 - 0.016) = 9987.6원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=9987.6, forces=forces)
        
        # 대포알 종목이므로 -2.0x ATR 룸까지 버텨야 함 (None 반환)
        assert reason is None, "대포알 종목이므로 -2.0배 ATR 한계까지 버텨야 합니다."

    def test_impulse_privilege_exit(self, strategy):
        """Test Case 7: 대포알 우대 종목의 최종 익절"""
        pos = _setup_mock_position(atr=1.0)
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10150, 
            "entry_impulse": 3.5
        }
        
        # 고점 대비 -2.1% 하락 (10150 * (1 - 0.021) = 9936.85원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=9936.85, forces=forces)
        
        # 방어망 한계(-2.0x ATR) 초과
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_initial_stop_loss(self, strategy):
        """Test Case 8: 초기 생존망 (Initial Stop Loss)"""
        pos = _setup_mock_position(atr=1.0)
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10050, # 고점 +0.5% (이익 보존망 기준선 1.0% 미달)
            "entry_impulse": 1.0
        }
        
        # 진입가 대비 -3.1% 이탈 (10000 -> 9690원)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=9690, forces=forces)
        
        assert reason is not None
        assert "Stop Loss" in reason
        assert "-3.10%" in reason