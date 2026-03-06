import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position

@pytest.fixture
def strategy():
    """상태 캐시 오염을 막기 위한 클린 픽스처"""
    st = TradingStrategy({"debug_mode": True})
    st._kinetic_state = {} 
    return st

def _setup_mock_position(atr=1.0, down_atr=0.5):
    """테스트 계산을 직관적으로 만들기 위해 매수가 10,000원 고정"""
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=10000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=atr, 
        down_atr_percent=down_atr
    )

class TestKineticEntryLogic:
    """📌 1. 진입 로직 (evaluate 함수) 마이크로 레짐 및 실속 검증"""

    def test_stall_shield(self, strategy):
        """Test Case 1: 고공 실속막 (Stall Shield) 검증"""
        data = SupplyData(stock_code="005930")
        # 수면에서 멀어짐(-0.95) & 추진력 상실(0.5)
        data.forces = {"gravity": -0.95, "thrust": 0.5, "jerk": 1.0, "current_velocity": 2.0}
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "고공 실속" in result["status"] or "Stall" in result["status"]

    def test_trend_quality_filter(self, strategy):
        """Test Case 2: 가짜 돌파 거부 검증 (Trend Quality Filter)"""
        data = SupplyData(stock_code="005930")
        
        # 💥 [데이터 교정] Submarine Trap과 Fake Breakout 쉴드를 피하기 위해 안전한 값 주입
        data.forces = {
            "thrust": 1.2, 
            "jerk": 0.5, 
            "current_velocity": 1.0, 
            "gravity": 0.5, 
            "impulse": 1.0
        }
        
        # Up-ATR(0.5) / Down-ATR(1.5) = 0.333 (< 1.5) -> 더러운 추세
        data.atr_percent = 2.0
        data.down_atr_percent = 1.5
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "더러운 추세" in result["status"] or "Low Quality" in result["status"]

    def test_perfect_entry(self, strategy):
        """Test Case 3: V2.4 순수 추세돌파 진입 성공"""
        data = SupplyData(stock_code="005930")
        
        # 💥 [데이터 교정] 빈 껍데기 가속도 쉴드(impulse <= 0.5)를 뚫기 위해 impulse: 1.0 주입
        data.forces = {
            "thrust": 1.0, 
            "jerk": 0.5, 
            "current_velocity": 1.0, 
            "gravity": -0.5, 
            "impulse": 1.0
        }
        
        # Up-ATR(1.5) / Down-ATR(0.5) = 3.0 (>= 1.5 양호한 체급)
        data.atr_percent = 2.0
        data.down_atr_percent = 0.5
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is True
        assert "추세돌파" in result["status"]
        assert "down_atr_percent" in result

class TestHeavyExitLogic:
    """📌 2. 청산 로직 (get_exit_reason 함수) Zero-Time 및 통합 방어망 검증"""

    def test_flash_crash_bailout(self, strategy):
        """Test Case 4: Zero-Time 폭포수 붕괴 즉사 (Flash Crash Bail-out)"""
        pos = _setup_mock_position()
        # 시간 변수 개입 없음. 수익률 -1.6% 달성 & 가속도 -1.2 역분사
        forces = {"jerk": -1.2, "thrust": 0.0, "current_velocity": 0.0}
        
        # 10000 -> 9840 (-1.6% 하락)
        reason = strategy.get_exit_reason(pos, current_price=9840, forces=forces)
        
        assert reason is not None
        assert "Bail-out" in reason
        assert "Flash Crash Detected" in reason
        assert "-1.60%" in reason

    def test_universal_stop_loss(self, strategy):
        """Test Case 5: 비대칭 통합 방어망 - 초기 손절 (Universal Stop Loss)"""
        pos = _setup_mock_position(atr=1.5, down_atr=1.0) 
        # Up-ATR = 0.5 -> 방어선 Limit = -1.5%
        
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10000 # 고점 갱신이 없는 상태 보장
        }
        
        # 10000 -> 9840 (-1.6% 하락)으로 방어선(-1.5%) 붕괴
        forces = {"jerk": 0.0, "current_velocity": 1.0}
        reason = strategy.get_exit_reason(pos, current_price=9840, forces=forces)
        
        assert reason is not None
        assert "Stop Loss (Universal: -1.60%)" in reason

    def test_universal_trailing_stop(self, strategy):
        """Test Case 6: 비대칭 통합 방어망 - 이익 보존 (Universal Trailing Stop)"""
        pos = _setup_mock_position(atr=1.5, down_atr=1.0)
        # Up-ATR = 0.5 -> 방어선 Limit = -1.5%
        
        # 진행 1: 고점을 10,300원(+3.0%)으로 갱신
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10000,
            "max_price": 10300 
        }
        
        # 진행 2: 고점 대비 -1.6% 하락 방어선 붕괴 (10300 * 0.984 = 10135.2원)
        forces = {"jerk": 0.0, "current_velocity": 1.0}
        reason = strategy.get_exit_reason(pos, current_price=10135.2, forces=forces)
        
        assert reason is not None
        assert "Trailing Stop" in reason