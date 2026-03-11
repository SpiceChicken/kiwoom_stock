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
    """범용적인 테스트를 위한 기본 매수 포지션 (매수가 10,000원 고정)"""
    return Position(
        id=1, stock_code="005930", stock_name="삼성전자",
        buy_price=10000, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=atr, 
        down_atr_percent=down_atr
    )

class TestEntryLogic:
    """📌 1. 진입 로직 검증 (모든 쉴드 및 동역학 필터)"""

    @pytest.mark.parametrize("forces, expected_keyword", [
        ({"thrust": 1.6, "gravity": -1.0, "current_velocity": 5.0}, "고점과열"), 
        ({"thrust": 0.5, "gravity": -0.96, "current_velocity": 2.0}, "실속"),
        ({"thrust": 1.6, "gravity": 0.0, "jerk": 1.0}, "수면 아래"),
        ({"thrust": 1.2, "impulse": 0.4, "jerk": 1.0}, "빈 껍데기"),
        ({"thrust": 0.75, "jerk": 0.5, "current_velocity": 1.0, "gravity": -0.5, "impulse": 1.0}, "수급 빈곤"),
    ])
    def test_entry_blocked_by_trap_shields(self, strategy, forces, expected_keyword):
        """[범용 방어막 검증] 다양한 트랩(덫) 상황에서 정확히 매수를 차단하는가?"""
        data = SupplyData(stock_code="005930")
        data.forces = forces
        data.atr_percent = 2.0
        data.down_atr_percent = 0.5
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False, f"방어막({expected_keyword})이 뚫렸습니다!"
        assert expected_keyword in result["status"]

    def test_entry_blocked_by_poor_trend_quality(self, strategy):
        """[체급 검증] 윗꼬리/아랫꼬리 변동성이 심한 '더러운 추세' 차단 검증"""
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 1.2, "jerk": 0.5, "current_velocity": 1.0, "gravity": 0.5, "impulse": 1.0}
        
        # Up-ATR(0.5) / Down-ATR(1.5) = 0.333 (< 1.5 기준 미달)
        data.atr_percent = 2.0
        data.down_atr_percent = 1.5 
        
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is False
        assert "더러운 추세" in result["status"] or "Low Quality" in result["status"]

    def test_entry_successful_breakout(self, strategy):
        """[순수 돌파] 모든 방어막을 통과한 완벽한 역학적 진입 검증"""
        data = SupplyData(stock_code="005930")
        data.forces = {"thrust": 0.85, "jerk": 0.5, "current_velocity": 1.0, "gravity": -0.5, "impulse": 1.0}
        data.atr_percent = 2.0
        data.down_atr_percent = 0.5 
        
        result = strategy.evaluate(data)
        assert result["is_buy_signal"] is True
        assert "추세돌파" in result["status"]


class TestExitLogic:
    """📌 2. 청산 로직 검증 (Zero-Time, 에너지 보존, 스나이퍼)"""

    def test_exit_panic_bailout(self, strategy):
        """[즉사 방어] 시장 폭락(Flash Crash) 또는 일반 역분사(Negative Jerk) 즉각 탈출 검증"""
        pos = _setup_mock_position()
        
        # 1. Flash Crash (수익률 -1.5% 이하, 가속도 -1.0 이하)
        forces_crash = {"jerk": -1.5, "thrust": 0.0, "current_velocity": 0.0}
        reason_crash = strategy.get_exit_reason(pos, current_price=9800, forces=forces_crash)
        assert reason_crash is not None, "Flash Crash 조건이 충족되었으나 None을 반환했습니다."
        assert "Flash Crash Detected" in reason_crash
        
        # 2. 💥 [레거시 청소 완료] Negative Jerk (가속도 -0.5 이하, 추진력 1.0 미만)
        forces_jerk = {"jerk": -0.6, "thrust": 0.5, "current_velocity": 1.0}
        reason_jerk = strategy.get_exit_reason(pos, current_price=9950, forces=forces_jerk) # -0.5% 구간
        assert reason_jerk is not None, "Negative Jerk 조건이 충족되었으나 None을 반환했습니다."
        assert "Negative Jerk" in reason_jerk

    def test_exit_universal_stop_loss(self, strategy):
        """[초기 손절망] 고점 갱신이 없는 초기 상태에서의 범용 손절망 동작 검증"""
        # Up-ATR 0.5 -> 방어선 -1.5%
        pos = _setup_mock_position(atr=1.5, down_atr=1.0) 
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10000}
        
        forces = {"jerk": 0.0, "current_velocity": 1.0}
        
        # 10000 -> 9840 (-1.6% 하락)으로 방어선 붕괴
        reason = strategy.get_exit_reason(pos, current_price=9840, forces=forces) 
        assert reason is not None and "Stop Loss" in reason

    def test_exit_dual_trigger_sniper(self, strategy):
        """[스나이퍼 엑시트] 수익권에서 발생하는 휩쏘(미세 눌림목)와 실제 하락(Sniper Exit)의 구분 검증"""
        # Up-ATR 0.5 -> 1배수 허용치 -0.5%
        pos = _setup_mock_position(atr=1.0, down_atr=0.5) 
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10250} # +2.5% 고점 갱신
        
        # Case 1: 휩쏘 (1분봉 음봉이 떴으나 하락폭이 -0.29%로 미세함 -> 홀드)
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": -0.5}
        reason_hold = strategy.get_exit_reason(pos, current_price=10220, forces=forces) 
        assert reason_hold is None
        
        # Case 2: 스나이퍼 타격 (음봉 & 방어선 이탈 -0.68% 하락 -> 익절)
        reason_exit = strategy.get_exit_reason(pos, current_price=10180, forces=forces) 
        assert reason_exit is not None and "Sniper Exit" in reason_exit

    def test_exit_energy_conservation(self, strategy):
        """[에너지 보존] 수익이 커질수록 방어선이 자동으로 타이트하게 조여지는지 검증"""
        # Up-ATR 1.0 -> 기본 방어선은 -3.0%로 매우 헐렁한 상태
        pos = _setup_mock_position(atr=1.5, down_atr=0.5) 
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10280} # +2.8% 수익 달성
        
        # 에너지 보존 룰 발동: max(-3.0%, -1.4%) = -1.4% 로 방어선이 타이트해짐
        forces = {"current_velocity": 1.0, "thrust": 1.0, "jerk": 0.5} 
        
        # 10280 -> 10140 (-1.36% 하락) -> 아직 버팀
        reason_hold = strategy.get_exit_reason(pos, current_price=10140, forces=forces)
        assert reason_hold is None
        
        # 10280 -> 10135 (-1.41% 하락) -> 타이트해진 방어선 붕괴 -> 수익 보존 익절
        reason_exit = strategy.get_exit_reason(pos, current_price=10135, forces=forces)
        assert reason_exit is not None and "Trailing Stop" in reason_exit