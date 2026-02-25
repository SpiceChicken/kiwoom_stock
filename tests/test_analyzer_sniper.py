# [PATCH] tests/test_analyzer_sniper.py

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
    """[Sniper Protocol] API Rate Limit 방어 및 시총 기반 로그 스케일 충격량 검증"""

    def test_skip_deep_analysis_for_low_momentum(self, analyzer):
        """1. 강도 100 이하 & 속도 0 이하일 때 무거운 호가/틱 API를 완벽히 차단하는가?"""
        code = "000660"
        analyzer.supply_cache[code] = SupplyData(stock_code=code)
        
        # 기본 API 반환값 모킹 (strength = 100.0)
        analyzer.collector.fetch_minute_chart.return_value = [{'cur_prc': '50000', 'high_pric':'50000', 'low_pric':'50000', 'trde_qty':'10'}] * 15
        analyzer.collector.fetch_stock_basic.return_value = {'trde_pre': '1.0', 'trde_qty': '1000', 'cur_prc': '50000', 'mac': '1000'}
        analyzer.collector.fetch_tick_strength.return_value = [{'cntr_str': '100.0'}] # 돌파 실패

        analyzer.update_priority_supply([code])
        
        # Assert: 심층 API(호가, 틱)는 단 한 번도 호출되어선 안 됨 (Rate Limit 방어 성공)
        analyzer.collector.fetch_order_book.assert_not_called()
        analyzer.collector.fetch_recent_ticks.assert_not_called()

    def test_trigger_deep_analysis_and_physics_mapping(self, analyzer):
        """2. 강도 100 초과 시 심층 API를 호출하고 변경된 인자(max_amount)로 맵핑하는가?"""
        code = "005930"
        analyzer.supply_cache[code] = SupplyData(stock_code=code)
        
        # 강도가 120으로 치솟은 상황 부여
        analyzer.collector.fetch_minute_chart.return_value = [{'cur_prc': '80000', 'high_pric':'80000', 'low_pric':'80000', 'trde_qty':'10'}] * 15
        analyzer.collector.fetch_stock_basic.return_value = {'trde_pre': '2.0', 'trde_qty': '5000', 'cur_prc': '80500', 'mac': '1000'} # 시총 1000억
        analyzer.collector.fetch_tick_strength.return_value = [{'cntr_str': '120.0'}] 
        
        # 심층 API 모킹
        analyzer.collector.fetch_order_book.return_value = {'tot_sel_req': 50000, 'tot_buy_req': 5000}
        
        # 단일 틱 2천만 원 체결 (80000 * 250)
        analyzer.collector.fetch_recent_ticks.return_value = [{'cur_prc': '80000', 'cntr_trde_qty': '250'}]
        
        analyzer.state_tracker.process_tick.return_value = {"total_score": 90.0, "forces": {"magnetic": 1.2}}

        analyzer.update_priority_supply([code])
        
        analyzer.collector.fetch_order_book.assert_called_once_with(code)
        
        # Assert: Engine 호출 시 파라미터가 정확히 넘어가는지 검증
        call_kwargs = analyzer.state_tracker.process_tick.call_args.kwargs
        assert call_kwargs["tot_sel_req"] == 50000
        
        # 시총 1000억 기준 Cutoff는 1천만 원. 2천만 원이 터졌으므로 스케일링된 값은 2.0배
        assert call_kwargs["max_amount"] == 2.0 

    def test_logarithmic_dynamic_impulse_scaling(self, analyzer):
        """3. 베버-페히너 법칙 기반: 시가총액별 로그 스케일링 허들 방어 검증"""
        code = "005930"
        
        # Case A: 소형주 (시총 500억) -> 컷오프 고정 1천만 원
        # 1,500만 원 체결 -> 돌파 성공 (1.5 반환 기대)
        analyzer.collector.fetch_recent_ticks.return_value = [{'cur_prc': '50000', 'cntr_trde_qty': '300'}]
        impulse_small = analyzer._fetch_safe_instant_volume(code, market_cap=50_000_000_000)
        assert impulse_small == 1.5
        
        # Case B: 중견주 (시총 1조, 10^12) -> log_scale = 1.0 -> 컷오프 3,500만 원
        # 7,000만 원 체결 -> 돌파 성공 (2.0 반환 기대)
        analyzer.collector.fetch_recent_ticks.return_value = [{'cur_prc': '70000', 'cntr_trde_qty': '1000'}]
        impulse_mid = analyzer._fetch_safe_instant_volume(code, market_cap=1_000_000_000_000)
        assert round(impulse_mid, 2) == 2.0
        
        # Case C: 초대형주 (시총 10조, 10^13) -> log_scale = 2.0 -> 컷오프 1억 2,250만 원
        # 동일한 7,000만 원 체결 -> 삼성전자 급에서는 노이즈에 불과하므로 돌파 실패 (0.0 반환 기대)
        impulse_large = analyzer._fetch_safe_instant_volume(code, market_cap=10_000_000_000_000)
        assert impulse_large == 0.0