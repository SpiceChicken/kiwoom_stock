# [PATCH] tests/test_analyzer_sniper.py

import pytest
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.state_manager import PhysicalStateTracker
from kiwoom_stock.domain.models import PhysicalContinuityEvidence
from kiwoom_stock.domain.state import PhysicalStateValidationError
from kiwoom_stock.core.database import PhysicalStatePersistenceError


def _continuity():
    return PhysicalContinuityEvidence(1, "initial", None, 0)

@pytest.fixture
def analyzer():
    mock_client = MagicMock()
    mock_state_tracker = MagicMock(spec=PhysicalStateTracker)
    config = {"proxy_code": "005930"}
    
    analyzer = MarketAnalyzer(mock_client, config, mock_state_tracker)
    analyzer.collector = MagicMock()
    analyzer.collector.fetch_indicator_chart.side_effect = (
        lambda *args, **kwargs: analyzer.collector.fetch_minute_chart(
            *args, **kwargs
        )
    )
    return analyzer

class TestSniperProtocol:
    def test_analyzer_zero_constant_mapping(self, analyzer):
        """[파이프라인 타격] 시총(mac)이 market_cap(원)으로 변환되어 엔진에 꽂히는가?"""
        code = "005930"
        analyzer.supply_cache[code] = SupplyData(stock_code=code)
        
        # mac 값이 1000 (단위: 억) -> 1,000억 원
        analyzer.collector.fetch_stock_basic.return_value = {
            'trde_pre': '2.0', 'trde_qty': '5000', 'cur_prc': '80500', 'mac': '1000'
        }
        
        # 💥 [핵심 방어] ATR 계산을 위한 'open_pric' 키값 추가!
        analyzer.collector.fetch_minute_chart.return_value = [
            {'cur_prc': '80000', 'open_pric': '80000', 'high_pric':'80000', 'low_pric':'80000', 'trde_qty':'10'}
        ] * 15
        
        analyzer.collector.fetch_tick_strength.return_value = [
            {'cntr_str': '120.0'}
        ] * 5
        analyzer.collector.fetch_order_book.return_value = {'tot_sel_req': 50000, 'tot_buy_req': 5000}
        
        analyzer.state_tracker.process_observations.return_value = {
            code: {
                "total_score": 90.0,
                "forces": {"magnetic": 1.2},
                "continuity": _continuity(),
            }
        }

        # 실행
        analyzer.update_priority_supply([code])
        
        observation = analyzer.state_tracker.process_observations.call_args.args[0][0]
        
        # Assert: mac 1000(억) -> 100_000_000_000(원)으로 정확히 치환되었는지 확인
        assert observation.cumulative_volume == 5000
        assert observation.market_cap == 100_000_000_000.0
        assert observation.strength == 120.0
        assert observation.prev_strength_5m == 120.0
        assert observation.observed_at.tzinfo is not None
        assert analyzer.supply_cache[code].forces == {"magnetic": 1.2}

    def test_fixed_cadence_row_four_is_canonical_observation_baseline(self, analyzer):
        code = "005930"
        observed_at = datetime(2026, 8, 8, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        analyzer._clock = lambda: observed_at
        analyzer.supply_cache[code] = SupplyData(stock_code=code)
        analyzer.collector.fetch_stock_basic.return_value = {
            "trde_pre": "2.0", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000"
        }
        analyzer.collector.fetch_minute_chart.return_value = [
            {
                "cur_prc": "80000", "open_pric": "80000", "high_pric": "80000",
                "low_pric": "80000", "trde_qty": "10",
            }
        ] * 15
        analyzer.collector.fetch_tick_strength.return_value = [
            {"cntr_str": value} for value in ("120", "115", "110", "105", "90")
        ]
        analyzer.collector.fetch_order_book.return_value = {
            "tot_sel_req": 1000, "tot_buy_req": 1000
        }
        analyzer.state_tracker.process_observations.return_value = {
            code: {
                "forces": {"jerk": 0.5},
                "continuity": _continuity(),
            }
        }

        analyzer.update_priority_supply([code])

        observation = analyzer.state_tracker.process_observations.call_args.args[0][0]
        assert observation.strength == 120.0
        assert observation.prev_strength_5m == 90.0
        assert observation.observed_at == observed_at
        assert observation.baseline_source == "row_4_fixed_cadence"
        assert observation.baseline_sample_index == 4
        assert observation.baseline_time_estimated is True

    def test_persistence_failure_clears_stale_forces_and_propagates(self, analyzer):
        code = "005930"
        data = SupplyData(stock_code=code, forces={"current_velocity": 99.0})
        analyzer.supply_cache[code] = data
        analyzer.collector.fetch_stock_basic.return_value = {
            "trde_pre": "2", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000"
        }
        analyzer.collector.fetch_minute_chart.return_value = [
            {
                "cur_prc": "80000", "open_pric": "80000", "high_pric": "80000",
                "low_pric": "80000", "trde_qty": "10",
            }
        ] * 15
        analyzer.collector.fetch_tick_strength.return_value = [
            {"cntr_str": "100"}
        ] * 5
        analyzer.collector.fetch_order_book.return_value = {
            "tot_sel_req": 1, "tot_buy_req": 1
        }
        analyzer.state_tracker.process_observations.side_effect = (
            PhysicalStatePersistenceError("commit failed")
        )

        with pytest.raises(PhysicalStatePersistenceError, match="commit failed"):
            analyzer.update_priority_supply([code])

        assert data.forces == {}
        assert data.continuity is None

    def test_invalid_observation_after_success_clears_stale_state(self, analyzer):
        code = "005930"
        data = SupplyData(stock_code=code)
        analyzer.supply_cache[code] = data
        analyzer.collector.fetch_minute_chart.return_value = [
            {
                "cur_prc": "80000", "open_pric": "80000",
                "high_pric": "80000", "low_pric": "80000", "trde_qty": "10",
            }
        ] * 15
        analyzer.collector.fetch_tick_strength.return_value = [
            {"cntr_str": "100"}
        ] * 5
        analyzer.collector.fetch_order_book.return_value = {
            "tot_sel_req": 1, "tot_buy_req": 1,
        }
        analyzer.collector.fetch_stock_basic.return_value = {
            "trde_pre": "2", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000",
        }
        analyzer.state_tracker.process_observations.return_value = {
            code: {
                "forces": {"current_velocity": 1.0},
                "continuity": _continuity(),
            }
        }

        analyzer.update_priority_supply([code])
        data = analyzer.supply_cache[code]
        assert data.forces == {"current_velocity": 1.0}
        assert data.continuity == _continuity()

        analyzer.collector.fetch_stock_basic.return_value = {
            "trde_pre": "2", "trde_qty": "5001", "cur_prc": "0", "mac": "1000",
        }
        with pytest.raises(PhysicalStateValidationError, match="current_price"):
            analyzer.update_priority_supply([code])

        assert data.forces == {}
        assert data.continuity is None
        assert analyzer.state_tracker.process_observations.call_count == 1

    @pytest.mark.parametrize("unexpected_parser_error", [False, True])
    def test_chart_parser_failure_after_success_is_typed_and_clears_stale_state(
        self,
        analyzer,
        unexpected_parser_error,
    ):
        code = "005930"
        data = SupplyData(stock_code=code)
        analyzer.supply_cache[code] = data
        valid_row = {
            "cur_prc": "80000", "open_pric": "80000",
            "high_pric": "80000", "low_pric": "80000", "trde_qty": "10",
        }
        analyzer.collector.fetch_minute_chart.return_value = [valid_row.copy() for _ in range(15)]
        analyzer.collector.fetch_stock_basic.return_value = {
            "trde_pre": "2", "trde_qty": "5000", "cur_prc": "80500", "mac": "1000",
        }
        analyzer.collector.fetch_tick_strength.return_value = [
            {"cntr_str": "100"}
        ] * 5
        analyzer.collector.fetch_order_book.return_value = {
            "tot_sel_req": 1, "tot_buy_req": 1,
        }
        analyzer.state_tracker.process_observations.return_value = {
            code: {
                "forces": {"current_velocity": 1.0},
                "continuity": _continuity(),
            }
        }
        analyzer.update_priority_supply([code])
        data = analyzer.supply_cache[code]

        if unexpected_parser_error:
            class ExplodingRow(dict):
                def __getitem__(self, key):
                    if key == "cur_prc":
                        raise RuntimeError("unexpected parser failure")
                    return super().__getitem__(key)

            bad_rows = [ExplodingRow(valid_row) for _ in range(15)]
            expected_type = "RuntimeError"
        else:
            bad_rows = [
                {key: value for key, value in valid_row.items() if key != "cur_prc"}
                for _ in range(15)
            ]
            expected_type = "KeyError"
        analyzer.collector.fetch_minute_chart.return_value = bad_rows

        with pytest.raises(PhysicalStateValidationError, match=expected_type):
            analyzer.update_priority_supply([code])

        assert data.forces == {}
        assert data.continuity is None
        assert analyzer.state_tracker.process_observations.call_count == 1
