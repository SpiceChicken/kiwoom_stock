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
    """📌 1. 진입 통제 센터 검증 (Kinematic Entry Control)"""

    def test_entry_breakout_override(self, strategy):
        """[Stage 1] 진성 돌파 하이패스 검증 (막대한 자본과 가속으로 저항 분쇄)"""
        data = SupplyData(stock_code="005930")
        # impulse >= 3.0, jerk >= 0.5, thrust >= 1.0 (하드락 무시 조건)
        data.forces = {"impulse": 3.5, "jerk": 0.6, "thrust": 1.2, "net_force": -0.5} 
        
        result = strategy.evaluate(data)
        
        # 합력 역전(net_force < 0)이지만 Stage 1에서 하이패스로 매수되어야 함
        assert result["is_buy_signal"] is True
        assert "진성 돌파" in result["status"] or "Breakout Override" in result["status"]

    @pytest.mark.parametrize("forces, atr, down_atr, expected_keyword", [
        # 0. 고점과열 차단 (Stage 0: thrust >= 1.5 and gravity <= -0.9)
        ({"thrust": 1.5, "gravity": -0.9, "net_force": 1.0, "impulse": 1.0}, 1.0, 0.5, "고점과열"),
        
        # 1. 수급 빈곤 (thrust < 0.8)
        ({"thrust": 0.7, "net_force": 1.0, "impulse": 1.0}, 1.0, 0.5, "수급 빈곤"), 
        
        # 2. 합력 역전 (net_force < 0.0)
        ({"thrust": 1.0, "net_force": -0.1, "impulse": 1.0}, 1.0, 0.5, "합력 역전"),
        
        # 3. 고공 실속 차단 (gravity <= -0.9 and thrust < 1.0)
        ({"thrust": 0.9, "gravity": -0.9, "net_force": 1.0, "impulse": 1.0}, 1.0, 0.5, "고공 실속"),
        
        # 4. 더러운 추세 (up_atr / down_atr_percent < 1.5)
        # atr=2.0, down_atr=1.5 -> up_atr=0.5 -> 비율 0.333 (< 1.5)
        ({"thrust": 1.0, "impulse": 1.0, "net_force": 1.0, "gravity": -0.5}, 2.0, 1.5, "더러운 추세"),
    ])
    def test_entry_hard_locks(self, strategy, forces, atr, down_atr, expected_keyword):
        """[Stage 0 & Stage 2] 절대 진입 금지 및 물리적 하드 록(Hard Locks) 방어막 검증"""
        data = SupplyData(stock_code="005930")
        data.forces = forces
        data.atr_percent = atr
        data.down_atr_percent = down_atr
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False, f"방어막({expected_keyword})이 뚫렸습니다!"
        assert expected_keyword in result["status"]

    def test_entry_standard_triggers(self, strategy):
        """[Stage 3] 정상 궤도 가동 검증 (추세돌파, 바닥반등, 예열중, 가속도 감소)"""
        data = SupplyData(stock_code="005930")
        # 기본 통과 조건: thrust=1.0, net_force=1.0, impulse=1.0, gravity=-0.5
        base_forces = {"thrust": 1.0, "net_force": 1.0, "impulse": 1.0, "gravity": -0.5}
        data.atr_percent = 2.0
        data.down_atr_percent = 0.5 # up_atr = 1.5 -> ratio = 3.0 (Low Quality 패스)
        
        # Case A: 추세돌파 (jerk > 0, current_velocity > 0)
        data.forces = {**base_forces, "jerk": 0.1, "current_velocity": 0.1}
        res_up = strategy.evaluate(data)
        assert res_up["is_buy_signal"] is True and "추세돌파" in res_up["status"]

        # Case B: 바닥반등 (jerk > 0, current_velocity <= 0, impulse > 0)
        data.forces = {**base_forces, "jerk": 0.1, "current_velocity": -0.5, "impulse": 1.0}
        res_rev = strategy.evaluate(data)
        assert res_rev["is_buy_signal"] is True and "바닥반등" in res_rev["status"]

        # Case C: 예열중 (jerk > 0, current_velocity <= 0, impulse=0, magnetic=0)
        data.forces = {**base_forces, "jerk": 0.1, "current_velocity": -0.5, "impulse": 0.0, "magnetic": 0.0}
        res_warm = strategy.evaluate(data)
        assert res_warm["is_buy_signal"] is False and "예열중" in res_warm["status"]

        # Case D: 가속도 감소 (jerk <= 0)
        data.forces = {**base_forces, "jerk": 0.0, "current_velocity": 1.0}
        res_drop = strategy.evaluate(data)
        assert res_drop["is_buy_signal"] is False and "가속도 감소" in res_drop["status"]


class TestExitLogic:
    """📌 2. 청산 로직 검증 (Bail-out, 고고도 잠금장치, 통합 쉴드)"""

    def test_exit_panic_bailout(self, strategy):
        """[조기 탈출] 폭포수 붕괴 및 일반 역분사 감지 검증"""
        pos = _setup_mock_position()
        
        # 1. Flash Crash (profit_rate <= -1.5, jerk <= -1.0)
        forces_crash = {"jerk": -1.2, "thrust": 0.0}
        reason_crash = strategy.get_exit_reason(pos, current_price=9800, forces=forces_crash) # -2.0% 구간
        assert reason_crash is not None and "Flash Crash Detected" in reason_crash
        
        # 2. Negative Jerk (jerk <= -0.5, thrust < 1.0)
        forces_jerk = {"jerk": -0.6, "thrust": 0.9}
        reason_jerk = strategy.get_exit_reason(pos, current_price=9900, forces=forces_jerk) # -1.0% 구간
        assert reason_jerk is not None and "Negative Jerk" in reason_jerk

    def test_exit_high_altitude_sniper(self, strategy):
        """[고고도 룰 1] 이중 잠금 스나이퍼 (초고도 예민 반응) 검증"""
        pos = _setup_mock_position(atr=1.0, down_atr=0.5) # Up-ATR = 0.5 -> 방어선 -0.5%
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10250} # +2.5% 고점 갱신
        
        # Case 1: 휩쏘 (jerk < 0 이지만 하락폭 -0.29%로 방어선 이내 -> 홀드)
        forces = {"jerk": -0.1, "thrust": 1.0}
        reason_hold = strategy.get_exit_reason(pos, current_price=10220, forces=forces) 
        assert reason_hold is None
        
        # Case 2: 스나이퍼 타격 (음봉 & 방어선 이탈 -0.68% 하락 -> 익절)
        reason_exit = strategy.get_exit_reason(pos, current_price=10180, forces=forces) 
        assert reason_exit is not None and "Sniper Exit" in reason_exit

    def test_exit_high_altitude_energy_conservation(self, strategy):
        """[고고도 룰 2] 에너지 보존 법칙 (수익 절반 방어) 검증"""
        pos = _setup_mock_position(atr=1.5, down_atr=0.5) # Up-ATR = 1.0 -> 기본 방어선 -3.0%
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10300} # +3.0% 수익 달성
        
        # 에너지 보존 룰 발동: max(-3.0%, -(3.0 * 0.5)%) = -1.5% 로 방어선이 타이트해짐
        forces = {"jerk": 0.1, "thrust": 1.0} # 음봉 아님 (스나이퍼 회피)
        
        # 10300 -> 10150 (-1.45% 하락) -> 타이트해진 방어선(-1.5%) 이내이므로 버팀
        reason_hold = strategy.get_exit_reason(pos, current_price=10150, forces=forces)
        assert reason_hold is None
        
        # 10300 -> 10140 (-1.55% 하락) -> 타이트해진 방어선(-1.5%) 붕괴 -> 익절
        reason_exit = strategy.get_exit_reason(pos, current_price=10140, forces=forces)
        assert reason_exit is not None and "Profit Retention" in reason_exit

    def test_exit_universal_shield(self, strategy):
        """[통합 쉴드] 초기 손절 및 일반 구간 이익 보존 검증"""
        pos = _setup_mock_position(atr=1.5, down_atr=1.0) # Up-ATR = 0.5 -> 방어선 -1.5%
        forces = {"jerk": 0.0, "current_velocity": 1.0}
        
        # 1. 초기 손절 (고점 갱신 없음)
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10000}
        reason_sl = strategy.get_exit_reason(pos, current_price=9840, forces=forces) # -1.6%
        assert reason_sl is not None and "Stop Loss (Universal" in reason_sl

        # 2. 일반 이익 보존 (고점 갱신 +1.8% -> 2.0% 미만이므로 고고도 룰 미발동)
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10180}
        reason_ts = strategy.get_exit_reason(pos, current_price=10020, forces=forces) # 고점 대비 -1.57%
        assert reason_ts is not None and "Trailing Stop (Peak:" in reason_ts