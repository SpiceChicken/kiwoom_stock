# [PATCH] tests/test_strategy.py 전면 재작성

import pytest
from datetime import datetime
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
        buy_price=50000, buy_score=85.0, buy_time="2026-02-10 10:00:00",
        buy_regime="STABLE_BULL", status="OPEN",
        atr_percent=1.5
    )

class TestKineticEntryLogic:
    """[진입 타격] Trap Shield 및 Two-Track 동역학 진입 검증"""

    def test_evaluate_trap_shield(self, strategy):
        """Case 1: 세력의 덫(고점 과열, 가속도 둔화)을 완벽히 차단하는가?"""
        data = SupplyData(stock_code="005930")
        data.total_score = 90.0
        strategy.history["005930"] = [80.0, 85.0] # 모멘텀은 양수 상태로 통제
        
        # 1. 고점 과열 차단 (Climax Shield) - Thrust가 높지만 Gravity 저항이 극심함
        data.forces = {"thrust": 1.6, "gravity": -1.0, "current_velocity": 5.0}
        res1 = strategy.evaluate(data)
        assert res1["is_buy_signal"] is False
        assert "고점과열 차단" in res1["status"]

        # 2. 가속도 둔화 차단 (Jerk Filter) - 가속도가 죽어가는 상태
        data.forces = {"thrust": 1.0, "gravity": 0.0, "jerk": -0.2, "current_velocity": 5.0}
        res2 = strategy.evaluate(data)
        assert res2["is_buy_signal"] is False
        assert "가속도 둔화 차단" in res2["status"]

    def test_evaluate_two_track_entry(self, strategy):
        """Case 2: 투트랙(추세 돌파 vs 바닥 반등) 진입 로직 검증"""
        data = SupplyData(stock_code="005930")
        data.total_score = 90.0
        strategy.history["005930"] = [80.0, 85.0] # 모멘텀 양수
        
        # 1. 추세 돌파 (Uptrend) - 이미 물 위로 올라온(velocity > 0) 상태
        data.forces = {"thrust": 1.0, "gravity": 0.0, "jerk": 0.5, "current_velocity": 2.0}
        res1 = strategy.evaluate(data)
        assert res1["is_buy_signal"] is True
        assert "추세돌파" in res1["status"]

        # 2. 바닥 반등 (Reversal Boost) - 물 속(velocity < 0)이지만 대포알(Impulse) 터짐
        data.forces = {"thrust": 1.0, "gravity": 0.0, "jerk": 0.5, "current_velocity": -1.0, "impulse": 1.5}
        res2 = strategy.evaluate(data)
        assert res2["is_buy_signal"] is True
        assert "바닥반등" in res2["status"]

    def test_evaluate_warming_up(self, strategy):
        """Case 3: 추세도 역배열이고 센서도 안 터졌으면 진입을 유보(Warming Up)하는가?"""
        data = SupplyData(stock_code="005930")
        data.total_score = 90.0
        strategy.history["005930"] = [80.0, 85.0]
        # 물 속(-1.0)이고 impulse/magnetic 둘 다 0
        data.forces = {"thrust": 1.0, "current_velocity": -1.0, "impulse": 0.0, "magnetic": 0.0, "jerk": 0.5}
        
        res = strategy.evaluate(data)
        assert res["is_buy_signal"] is False
        assert "예열중" in res["status"]


class TestHeavyExitLogic:
    """[청산 타격] Heavy Exit (Flash Dump, Bleeding, Trailing Stop) 검증"""

    def test_exit_engine_dead(self, strategy):
        """Rule 1: 엔진 완전 소진 (Velocity < 0) 시 즉각 컷오프하는가?"""
        pos = _setup_mock_position()
        forces = {"current_velocity": -0.5, "net_force": -1.0} # 속도가 마이너스로 꼬라박음
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        assert reason is not None and "Engine Dead" in reason

    def test_exit_flash_dump(self, strategy):
        """Rule 2-A: 플래시 덤프 (30초 내 폭포수 하락) 방어 검증"""
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000, "forces": []}
        
        # 3연속 틱 모두 음수이고 합계가 -3.0 이하 (-1.0, -1.0, -1.5 = -3.5)
        forces_seq = [-1.0, -1.0, -1.5] 
        reason = None
        for f in forces_seq:
            forces = {"current_velocity": 1.0, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=50500, forces=forces)
            
        assert reason is not None and "Flash Dump" in reason

    def test_exit_bleeding(self, strategy):
        """Rule 2-B: 블리딩 (60초 내 서서히 가라앉는 하락) 방어 검증"""
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000, "forces": []}
        
        # 💥 [데이터 교정] Flash Dump(최근 3틱 합 -3.0 이하)를 피하면서 6틱 합이 -4.5 이하가 되도록 조절
        # 전체 합계: -4.6 / 최근 3틱 합계: -1.0 (Flash Dump 발동 안 함)
        forces_seq = [-1.5, -1.5, -0.6, -1.0, 0.2, -0.2] 
        reason = None
        for f in forces_seq:
            forces = {"current_velocity": 1.0, "net_force": f}
            reason = strategy.get_exit_reason(pos, current_price=50500, forces=forces)
            
        assert reason is not None and "Bleeding" in reason

    def test_exit_price_trailing_stop(self, strategy):
        """Rule 3: 고점(max_price) 대비 1.5% 하락 시 가격 기반 트레일링 스탑이 작동하는가?"""
        pos = _setup_mock_position()
        # 내부 상태의 최고점을 52,000원으로 강제 세팅
        strategy._kinetic_state[pos.stock_code] = {"max_price": 52000, "forces": []}
        
        # 현재가가 51,000원으로 하락 (51,000 / 52,000 - 1 = -1.92% 하락 -> 1.5% 이탈)
        forces = {"current_velocity": 1.0, "net_force": 0.0}
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        
        assert reason is not None and "Trailing Stop" in reason

    @patch('kiwoom_stock.monitoring.strategy.datetime')
    def test_selective_swing_overnight(self, mock_datetime, strategy):
        """Rule 4: 15:20 이후 조건 만족 시 오버나잇 스윙으로 전환되는가?"""
        mock_datetime.now.return_value = datetime(2026, 2, 10, 15, 25, 0)
        mock_datetime.combine.side_effect = datetime.combine
        
        pos = _setup_mock_position()
        strategy._kinetic_state[pos.stock_code] = {"max_price": 50000, "forces": []}
        
        # 오버나잇 통과 조건: 속도 >= 3.0, 추세 >= 2.0, 자기력 > 0
        forces = {"current_velocity": 3.5, "thrust": 2.5, "magnetic": 1.5, "net_force": 1.0}
        reason = strategy.get_exit_reason(pos, current_price=51000, forces=forces)
        
        assert reason is None
        assert pos.status == "OVERNIGHT"