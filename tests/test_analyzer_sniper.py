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

def test_analyzer_zero_constant_mapping(analyzer):
    """[파이프라인 타격] 시총(mac)이 market_cap(원)으로 변환되어 엔진에 꽂히는가?"""
    code = "005930"
    analyzer.supply_cache[code] = SupplyData(stock_code=code)
    
    # mac 값이 1000 (단위: 억) -> 1,000억 원
    analyzer.collector.fetch_stock_basic.return_value = {
        'trde_pre': '2.0', 'trde_qty': '5000', 'cur_prc': '80500', 'mac': '1000'
    }
    analyzer.collector.fetch_minute_chart.return_value = [{'cur_prc': '80000', 'high_pric':'80000', 'low_pric':'80000', 'trde_qty':'10'}] * 15
    analyzer.collector.fetch_tick_strength.return_value = [{'cntr_str': '120.0'}] 
    analyzer.collector.fetch_order_book.return_value = {'tot_sel_req': 50000, 'tot_buy_req': 5000}
    
    analyzer.state_tracker.process_tick.return_value = {"forces": {"magnetic": 1.2}}

    # 실행
    analyzer.update_priority_supply([code])
    
    call_kwargs = analyzer.state_tracker.process_tick.call_args.kwargs
    
    # Assert 1: max_amount가 사라지고 total_volume이 넘어갔는지 확인
    assert "max_amount" not in call_kwargs
    assert call_kwargs["total_volume"] == 5000
    
    # Assert 2: mac 1000(억) -> 100_000_000_000(원)으로 정확히 치환되었는지 확인
    assert call_kwargs["market_cap"] == 100_000_000_000.0
    
    # Assert 3: 데이터 반환 시 forces 딕셔너리에 정확히 꽂혔는지 확인 (Key 통일)
    assert analyzer.supply_cache[code].forces == {"magnetic": 1.2}