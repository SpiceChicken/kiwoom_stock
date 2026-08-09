from copy import deepcopy
from datetime import datetime

import pytest
from kiwoom_stock.monitoring.strategy import TradingStrategy
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.monitoring.manager import Position
from kiwoom_stock.domain.strategy import (
    StrategySemanticsValidationError,
    TargetStopPolicy,
)

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
        # 💥 [데이터 교정] cur_prc=50000 주입하여 VI 방어막 통과
        data = SupplyData(stock_code="005930", cur_prc=50000)
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
        # 💥 [데이터 교정] cur_prc=50000 주입하여 VI 방어막 통과
        data = SupplyData(stock_code="005930", cur_prc=50000)
        data.forces = forces
        data.atr_percent = atr
        data.down_atr_percent = down_atr
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False, f"방어막({expected_keyword})이 뚫렸습니다!"
        assert expected_keyword in result["status"]

    def test_entry_standard_triggers(self, strategy):
        """[Stage 3] 정상 궤도 가동 검증 (추세돌파, 바닥반등, 예열중, 가속도 감소)"""
        # 💥 [데이터 교정] cur_prc=50000 주입하여 VI 방어막 통과
        data = SupplyData(stock_code="005930", cur_prc=50000)
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

    def test_zero_price_shield(self, strategy):
        """🛡️ 1. Zero-Price Shield 검증 (V2.6.3)"""
        # cur_prc가 0.0인 VI 발동 상황 가정
        data = SupplyData(stock_code="005930", cur_prc=0.0)  
        data.forces = {'thrust': 2.0, 'jerk': 1.0} # 가속도가 엄청나도
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "0원 호가 무시" in result["status"]

    def test_net_force_lock(self, strategy):
        """🛡️ 2. Net Force 역전 차단 검증 (V2.6.2)"""
        data = SupplyData(stock_code="005930", cur_prc=50000.0)
        # Thrust는 좋으나 중력/항력에 의해 Net Force가 마이너스인 비행기 딜레마 상황
        data.forces = {'thrust': 1.5, 'net_force': -0.5, 'jerk': 0.1}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "합력 역전" in result["status"]

    def test_fuel_exhaustion_shield(self, strategy):
        """🛡️ 3. 연료 고갈 차단 검증 (V2.6.4)"""
        data = SupplyData(stock_code="005930", cur_prc=50000.0)
        
        # 💥 [데이터 교정] 5순위 '더러운 추세' 방어막을 피하기 위해 건전한 ATR 체급 주입
        data.atr_percent = 2.0
        data.down_atr_percent = 0.5
        
        # 가속도는 붙어있으나 거래량이 직전 대비 30% 수준으로 반토막 난 상황
        data.forces = {'thrust': 1.0, 'jerk': 0.5, 'volume_drop_ratio': 0.3}
        
        result = strategy.evaluate(data)
        
        assert result["is_buy_signal"] is False
        assert "연료 고갈" in result["status"]


class TestExitLogic:
    """📌 2. 청산 로직 검증 (VI 방어망, Bail-out, 고고도 잠금장치, 통합 쉴드)"""

    def test_exit_vi_defense(self, strategy):
        """[Sign-off] Exit 방어 검증: 0원 틱 유입 시 Stop Loss(-100%) 로깅 없이 조용히 패스"""
        pos = _setup_mock_position(atr=1.5, down_atr=1.0)
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10000}
        
        forces = {"jerk": -2.0, "current_velocity": -5.0} # 물리 엔진 최악의 악조건 주입
        
        # 0원 틱 주입
        reason = strategy.get_exit_reason(pos, current_price=0.0, forces=forces)
        
        # -100.0% 손절이나 에러가 뜨지 않고 완벽히 무시되어야 함
        assert reason is None, "0원 틱 방어막이 뚫려 잘못된 청산 사유가 발생했습니다."

    @pytest.mark.parametrize(
        "invalid_buy_price",
        [None, True, float("nan"), float("inf"), 0.0, -1.0],
        ids=["none", "bool", "nan", "inf", "zero", "negative"],
    )
    def test_invalid_buy_price_fails_before_exit_or_kinetic_state_mutation(
        self,
        strategy,
        invalid_buy_price,
    ):
        pos = _setup_mock_position()
        pos.buy_price = invalid_buy_price
        strategy._kinetic_state[pos.stock_code] = {
            "buy_price": 10_000.0,
            "max_price": 10_500.0,
        }
        before = deepcopy(strategy._kinetic_state)

        with pytest.raises(StrategySemanticsValidationError, match="buy_price"):
            strategy.get_exit_reason(
                pos,
                current_price=10_300.0,
                forces={
                    "current_velocity": 4.0,
                    "thrust": 3.0,
                    "magnetic": 1.0,
                    "jerk": -2.0,
                },
                now=datetime(2026, 8, 3, 15, 27),
            )

        assert strategy._kinetic_state == before
        assert pos.status == "OPEN"

    def test_missing_buy_price_fails_before_overnight_or_kinetic_state_creation(
        self,
        strategy,
    ):
        pos = _setup_mock_position()
        del pos.buy_price
        pos.status = "OVERNIGHT"

        with pytest.raises(StrategySemanticsValidationError, match="buy_price"):
            strategy.get_exit_reason(
                pos,
                current_price=10_300.0,
                forces={
                    "current_velocity": 4.0,
                    "thrust": 3.0,
                    "magnetic": 1.0,
                },
                now=datetime(2026, 8, 3, 15, 27),
            )

        assert pos.stock_code not in strategy._kinetic_state
        assert pos.status == "OVERNIGHT"

    def test_direct_strategy_defaults_policy_and_rejects_wrong_policy_adapter(self):
        strategy = TradingStrategy({"debug_mode": True})

        assert strategy.target_stop_policy == TargetStopPolicy()
        with pytest.raises(TypeError):
            TradingStrategy({}, target_stop_policy=object())

    @pytest.mark.parametrize(
        "config",
        [
            {"target_profit_rate": 0.03, "stop_loss_rate": -0.03},
            {"target_profit_rate": 0.03},
            {"target_stop_unit_version": "percentage-points-v1"},
            {
                "target_stop_unit_version": "ratio-v0",
                "target_profit_percentage_points": 3.0,
                "stop_loss_percentage_points": 3.0,
            },
            {
                "target_stop_unit_version": "percentage-points-v1",
                "target_profit_percentage_points": "3.0",
                "stop_loss_percentage_points": 3.0,
            },
            {
                "target_stop_unit_version": "percentage-points-v1",
                "target_profit_percentage_points": True,
                "stop_loss_percentage_points": 3.0,
            },
            {
                "target_stop_unit_version": "percentage-points-v1",
                "target_profit_percentage_points": float("nan"),
                "stop_loss_percentage_points": 3.0,
            },
            {
                "target_stop_unit_version": "percentage-points-v1",
                "target_profit_percentage_points": 3.0,
                "stop_loss_percentage_points": 0.0,
            },
        ],
    )
    def test_direct_strategy_rejects_ambiguous_or_invalid_target_stop(self, config):
        with pytest.raises((TypeError, ValueError)):
            TradingStrategy(config)

    @pytest.mark.parametrize(
        ("score", "expected"),
        [(-4.9999, False), (-5.0, True), (-5.0001, True)],
    )
    def test_cumulative_trade_return_score_floor_is_inclusive(
        self,
        score,
        expected,
    ):
        strategy = TradingStrategy(
            {"cumulative_trade_return_score_floor": -5.0}
        )

        assert strategy.is_kill_switch_activated(score) is expected

    @pytest.mark.parametrize(
        "value",
        [True, float("nan"), float("inf"), 0.1],
    )
    def test_cumulative_trade_return_score_floor_rejects_invalid_values(
        self,
        value,
    ):
        with pytest.raises(StrategySemanticsValidationError):
            TradingStrategy(
                {"cumulative_trade_return_score_floor": value}
            )

    def test_direct_strategy_rejects_deprecated_score_floor_dict_key(self):
        with pytest.raises(StrategySemanticsValidationError, match="unsupported"):
            TradingStrategy({"total_loss_limit": -5.0})

    @pytest.mark.parametrize(
        ("current_price", "expected"),
        [
            (10255.499, None),
            (10255.5, "Fixed Target"),
            (10255.501, "Fixed Target"),
            (9744.501, None),
            (9744.5, "Fixed Stop"),
            (9744.499, "Fixed Stop"),
        ],
    )
    def test_fixed_target_stop_use_inclusive_full_precision_boundaries(
        self,
        current_price,
        expected,
    ):
        strategy = TradingStrategy(
            {"debug_mode": True},
            target_stop_policy=TargetStopPolicy(
                target_profit_percentage_points=2.555,
                stop_loss_percentage_points=2.555,
            ),
        )
        pos = _setup_mock_position(atr=20.0, down_atr=1.0)

        reason = strategy.get_exit_reason(
            pos,
            current_price=current_price,
            forces={"jerk": 0.0, "thrust": 1.0},
            now=datetime(2026, 8, 3, 10, 0),
        )

        if expected is None:
            assert reason is None
        else:
            assert reason is not None and expected in reason
            assert "2.555 %p" in reason
            assert "percentage-points-v1" in reason
            assert all(term not in reason.lower() for term in ("pnl", "portfolio", "fill"))

    def test_fixed_target_precedes_late_overnight_candidate(self):
        strategy = TradingStrategy({"debug_mode": False})
        pos = _setup_mock_position()
        forces = {
            "current_velocity": 3.0,
            "thrust": 2.1,
            "magnetic": 0.1,
            "jerk": 0.0,
        }

        reason = strategy.get_exit_reason(
            pos,
            current_price=10300.0,
            forces=forces,
            now=datetime(2026, 8, 3, 15, 27),
        )

        assert reason is not None and "Fixed Target" in reason
        assert pos.status == "OPEN"

    def test_persisted_overnight_still_blocks_fixed_target(self):
        strategy = TradingStrategy({"debug_mode": False})
        pos = _setup_mock_position()
        pos.status = "OVERNIGHT"

        assert strategy.get_exit_reason(
            pos,
            current_price=10300.0,
            forces={"jerk": 0.0},
            now=datetime(2026, 8, 3, 15, 27),
        ) is None

    def test_exit_panic_bailout(self, strategy):
        pos = _setup_mock_position()
        
        forces_crash = {"jerk": -1.2, "thrust": 0.0}
        reason_crash = strategy.get_exit_reason(pos, current_price=9800, forces=forces_crash)
        assert reason_crash is not None and "Flash Crash Detected" in reason_crash
        
        forces_jerk = {"jerk": -0.6, "thrust": 0.9}
        reason_jerk = strategy.get_exit_reason(pos, current_price=9900, forces=forces_jerk)
        assert reason_jerk is not None and "Negative Jerk" in reason_jerk

    def test_exit_high_altitude_sniper(self, strategy):
        pos = _setup_mock_position(atr=1.0, down_atr=0.5) 
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10250} 
        
        forces = {"jerk": -0.1, "thrust": 1.0}
        assert strategy.get_exit_reason(pos, current_price=10220, forces=forces) is None
        
        reason_exit = strategy.get_exit_reason(pos, current_price=10180, forces=forces) 
        assert reason_exit is not None and "Sniper Exit" in reason_exit

    def test_exit_high_altitude_energy_conservation(self, strategy):
        pos = _setup_mock_position(atr=1.5, down_atr=0.5) 
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10300} 
        forces = {"jerk": 0.1, "thrust": 1.0} 
        
        assert strategy.get_exit_reason(pos, current_price=10150, forces=forces) is None
        
        reason_exit = strategy.get_exit_reason(pos, current_price=10140, forces=forces)
        assert reason_exit is not None and "Profit Retention" in reason_exit

    def test_exit_universal_shield(self, strategy):
        pos = _setup_mock_position(atr=1.5, down_atr=1.0) 
        forces = {"jerk": 0.0, "current_velocity": 1.0}
        
        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10000}
        reason_sl = strategy.get_exit_reason(pos, current_price=9840, forces=forces) 
        assert reason_sl is not None and "Stop Loss (Universal" in reason_sl

        strategy._kinetic_state[pos.stock_code] = {"buy_price": 10000, "max_price": 10180}
        reason_ts = strategy.get_exit_reason(pos, current_price=10020, forces=forces) 
        assert reason_ts is not None and "Trailing Stop (Peak:" in reason_ts
