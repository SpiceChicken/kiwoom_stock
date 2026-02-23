# [PATCH] tests/test_analyzer_sniper.py 전면 교체

import pytest
from unittest.mock import MagicMock
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core.state_manager import PhysicalStateTracker

@pytest.fixture
def analyzer():
    mock_client = MagicMock()
    mock_state_tracker = MagicMock(spec=PhysicalStateTracker)
    config = {"proxy_code": "005930"}
    
    analyzer = MarketAnalyzer(mock_client, config, mock_state_tracker)
    analyzer.collector = MagicMock()
    return analyzer

class TestSniperProtocol:
    """[Sniper Protocol] API Rate Limit 방어 및 물리 엔진 파이프라인 연결 테스트"""

    def test_skip_deep_analysis_for_low_velocity(self, analyzer):
        """1. 이전 점수가 75점 미만이면 호가/틱 API 호출을 차단하는가?"""
        code = "000660"
        # 이전 틱에서 점수가 70점이었다고 가정
        analyzer.supply_cache[code] = SupplyData(stock_code=code, total_score=70.0)
        
        # 기본 API 반환값 모킹
        analyzer.collector.fetch_minute_chart.return_value = [{'cur_prc': '50000', 'high_pric':'50000', 'low_pric':'50000', 'trde_qty':'10'}] * 15
        analyzer.collector.fetch_stock_basic.return_value = {'trde_pre': '1.0', 'trde_qty': '1000', 'cur_prc': '50000'}
        analyzer.collector.fetch_tick_strength.return_value = [{'cntr_str': '100.0'}]

        analyzer.update_priority_supply([code])
        
        # Assert: 무거운 API는 호출되지 않아야 함 (Rate Limit 방어)
        analyzer.collector.fetch_order_book.assert_not_called()
        analyzer.collector.fetch_recent_ticks.assert_not_called()

    def test_trigger_deep_analysis_for_high_velocity(self, analyzer):
        """2. 이전 점수가 75점 이상이면 심층 API를 호출하고 물리 엔진에 데이터를 넘기는가?"""
        code = "005930"
        # 이전 틱에서 80점(가속 상태)이었다고 가정
        analyzer.supply_cache[code] = SupplyData(stock_code=code, total_score=80.0)
        
        # 기본 API 반환값 모킹
        analyzer.collector.fetch_minute_chart.return_value = [{'cur_prc': '80000', 'high_pric':'80000', 'low_pric':'80000', 'trde_qty':'10'}] * 15
        analyzer.collector.fetch_stock_basic.return_value = {'trde_pre': '2.0', 'trde_qty': '5000', 'cur_prc': '80500'}
        analyzer.collector.fetch_tick_strength.return_value = [{'cntr_str': '120.0'}]
        
        # 심층 API 반환값 모킹
        analyzer.collector.fetch_order_book.return_value = {'tot_sel_req': 50000, 'tot_buy_req': 5000}
        analyzer.collector.fetch_recent_ticks.return_value = [{'cur_prc': '80500', 'cntr_trde_qty': '124223'}] # 약 1억
        
        analyzer.state_tracker.process_tick.return_value = {"total_score": 90.0, "forces": {"magnetic": 1.2}}

        analyzer.update_priority_supply([code])
        
        # Assert 1: 심층 API가 호출되어야 함
        analyzer.collector.fetch_order_book.assert_called_once_with(code)
        
        # Assert 2: 물리 엔진에 호가 데이터가 정확히 넘어갔는지 확인
        call_kwargs = analyzer.state_tracker.process_tick.call_args.kwargs
        assert call_kwargs["tot_sel_req"] == 50000
        assert call_kwargs["tot_buy_req"] == 5000
        assert call_kwargs["max_instant_amt_100m"] > 0