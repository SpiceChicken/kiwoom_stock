import pytest
from datetime import datetime, time
from unittest.mock import patch
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    """테스트 간 상태 캐시 오염을 막기 위한 클린 픽스처"""
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position():
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=50000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=1.5
    )

class TestKineticEntryLogic:
    """📌 1. 진입 로직 (evaluate 함수) 검증 명세"""

    def test_climax_shield(self, strategy):
        """Test Case 1: 고점 피날레 빔 차단 (Climax Shield)"""
        data = SupplyData(stock_code="005930")
        # thrust >= 1.5 AND gravity <= -0.9 조건을 만족하며, jerk와 current_velocity가 매우 높은 상황
        data.forces = {"thrust": 2.0, "gravity": -1.0, "jerk": 5.0, "current_velocity": 10.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False, "고점과열 차단 조건에서는 절대 매수 신호가 True가 될 수 없습니다."
        assert "고점과열 차단" in result["status"]
        
        # 💥 [중요 검증] score, momentum 키 완전 배제 확인
        assert "score" not in result, "Score 변수가 완전히 제거되어야 합니다."
        assert "momentum" not in result, "Momentum 변수가 완전히 제거되어야 합니다."

    def test_pure_physics_breakout(self, strategy):
        """Test Case 2: 순수 물리 기반 추세돌파 진입 (Score 완전 배제)"""
        data = SupplyData(stock_code="000660")
        # 쉴드 조건 미해당, thrust > 0.0 AND jerk > 0.0 AND current_velocity > 0.0
        data.forces = {"thrust": 1.0, "gravity": -0.5, "jerk": 0.5, "current_velocity": 2.0}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is True, "물리적 조건이 충족되면 매수 신호가 켜져야 합니다."
        assert "추세돌파" in result["status"]
        
        # 💥 [중요 검증] score, momentum 키 완전 배제 확인
        assert "score" not in result, "Score 변수가 완전히 제거되어야 합니다."
        assert "momentum" not in result, "Momentum 변수가 완전히 제거되어야 합니다."


class TestHeavyExitLogic:
    """📌 2. 청산 로직 (get_exit_reason 함수) 검증 명세"""

    def test_whipsaw_tolerance(self, strategy):
        """Test Case 3: 횡보장 휩쏘(Whipsaw) 내성 검증"""
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000}
        
        # current_velocity가 음수지만 -2.0보다는 큰 상황 (-0.5), 가격은 50000원 변동 없음
        forces = {"current_velocity": -0.5} 
        reason = strategy.get_exit_reason(pos, current_price=50000, forces=forces)
        
        assert reason is None, "과거의 누적 합력 배열(Net Force)에 의해 기계적 손절이 나가지 않고 휩쏘를 버텨내야 합니다."

    def test_engine_dead(self, strategy):
        """Test Case 4: 엔진 완전 소진 (Engine Dead)"""
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000}
        
        # current_velocity <= -2.0으로 완전 심해 진입
        forces = {"current_velocity": -2.1} 
        reason = strategy.get_exit_reason(pos, current_price=50000, forces=forces)
        
        assert reason is not None
        assert "Kinetic Exit (Engine Dead: V <= -2.0)" in reason

    def test_trailing_stop(self, strategy):
        """Test Case 5: 가격 기반 트레일링 스탑 (Trailing Stop)"""
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000}
        
        # 진행 1: 고점 갱신 (52000원 돌파)
        forces = {"current_velocity": 1.5} # 엔진은 살아있음
        strategy.get_exit_reason(pos, current_price=52000, forces=forces)
        assert strategy._kinetic_state[pos.stock_code]["max_price"] == 52000
        
        # 진행 2: 갱신된 고점(52000) 대비 -1.5% 이탈 하락 (52000 -> 51000은 약 -1.92% 하락)
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        
        assert reason is not None
        assert "Trailing Stop" in reason

    def test_atr_stop_loss(self, strategy):
        """Test Case 6: 동적 스탑로스 (ATR Stop Loss)"""
        pos = _setup_mock_position() # 매수가 50000원
        
        # 💥 [데이터 교정] ATR을 0.3으로 좁혀서 동적 손절선을 -0.9%로 타이트하게 만듭니다.
        pos.atr_percent = 0.3 
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000}
        
        # 50000 -> 49500은 "-1.0% 하락"입니다.
        # 고점 대비 -1.5%인 Trailing Stop에는 걸리지 않지만, 
        # 매수가 대비 -0.9%인 ATR Stop Loss에는 걸려야 합니다!
        forces = {"current_velocity": 1.0} 
        reason = strategy.get_exit_reason(pos, current_price=49500, forces=forces)
        
        assert reason is not None
        assert "Stop Loss" in reason