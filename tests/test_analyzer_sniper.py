import pytest
from unittest.mock import MagicMock, patch
from kiwoom_stock.monitoring.analyzer import MarketAnalyzer
from kiwoom_stock.core.schema import SupplyData
from kiwoom_stock.core import scoring

# Fixture: Mocked Analyzer
@pytest.fixture
def analyzer():
    mock_client = MagicMock()
    config = {"proxy_code": "005930"}
    
    # Analyzer 인스턴스 생성
    analyzer = MarketAnalyzer(mock_client, config)
    
    # Collector Mocking (API 호출 차단)
    analyzer.collector = MagicMock()
    
    return analyzer

# Fixture: High Score Data (Inherit from conftest.py)
@pytest.fixture
def high_score_data(sample_supply_data):
    """
    conftest.py의 sample_supply_data를 기반으로
    점수를 80점(합격권)으로 설정한 데이터
    """
    data = sample_supply_data
    data.total_score = 80.0  # Sniper Trigger (>= 75.0)
    
    # Sniper 관련 필드 초기화 (안전장치)
    data.total_ask_remains = 0
    data.total_buy_remains = 0
    data.whale_activity = False
    data.whale_volume = 0.0
    
    return data

# Fixture: Low Score Data (Inherit from conftest.py)
@pytest.fixture
def low_score_data(sample_supply_data):
    """
    conftest.py의 sample_supply_data를 기반으로
    점수를 70점(불합격권)으로 설정한 데이터
    """
    # 객체 복사가 필요하다면 copy 모듈 사용, 여기서는 단순 할당 후 수정
    data = sample_supply_data 
    data.stock_code = "000660" # 구분용
    data.total_score = 70.0  # Sniper Skip (< 75.0)
    return data

class TestSniperProtocol:
    """
    [Sniper Protocol] 심층 분석 로직 테스트
    - ka10004 (호가 잔량), ka10003 (체결 정보) API 호출 조건 및 점수 반영 검증
    """

    def test_skip_deep_analysis_for_low_score(self, analyzer, low_score_data):
        """1. 점수가 75점 미만이면 심층 분석(API 호출)을 수행하지 않아야 한다."""
        # Setup
        code = low_score_data.stock_code
        
        # Execute
        analyzer._perform_deep_analysis(low_score_data, code)
        
        # Assert
        # API가 호출되지 않았는지 확인
        analyzer.collector.fetch_order_book.assert_not_called()
        analyzer.collector.fetch_recent_ticks.assert_not_called()
        
        # 점수가 변하지 않았는지 확인
        assert low_score_data.total_score == 70.0

    def test_trigger_deep_analysis_for_high_score(self, analyzer, high_score_data):
        """2. 점수가 75점 이상이면 심층 분석 API를 호출해야 한다."""
        # Setup
        code = high_score_data.stock_code
        
        # Mock API Responses (중립적인 데이터)
        analyzer.collector.fetch_order_book.return_value = {'ask_total': 1000, 'buy_total': 1000}
        analyzer.collector.fetch_recent_ticks.return_value = {'whale_found': False, 'whale_vol': 0.0}
        
        # Execute
        analyzer._perform_deep_analysis(high_score_data, code)
        
        # Assert
        # API가 호출되었는지 확인
        analyzer.collector.fetch_order_book.assert_called_once_with(code)
        analyzer.collector.fetch_recent_ticks.assert_called_once_with(code)

    def test_whale_boost_application(self, analyzer, high_score_data):
        """3. 고래(Whale)가 발견되면 점수가 가산되어야 한다."""
        # Setup
        code = high_score_data.stock_code
        initial_score = high_score_data.total_score # 80.0
        
        # Mock API: 고래 발견 (50억 원)
        analyzer.collector.fetch_order_book.return_value = {'sell_total': 1000, 'buy_total': 1000}
        analyzer.collector.fetch_recent_ticks.return_value = {
            'whale_found': True, 
            'whale_vol': 50.0 # 50억
        }
        
        # Execute
        analyzer._perform_deep_analysis(high_score_data, code)
        
        # Assert       
        # 점수 상승 확인 (scoring.apply_deep_analysis_bonus 로직 의존)
        # 예상: 80.0 * (1.0 + 50*0.01) -> 80 * 1.5 -> 120 (Max 100)
        assert high_score_data.total_score > initial_score
        assert high_score_data.total_score <= 100.0 # 상한선 체크

    def test_order_book_penalty(self, analyzer, high_score_data):
        """4. 매수 잔량이 압도적으로 많으면(허매수 의심) 감점되어야 한다."""
        # Setup
        code = high_score_data.stock_code
        # 테스트 간섭 방지를 위해 점수 리셋 (Fixtures are function-scoped by default, but safe to reset)
        high_score_data.total_score = 80.0 
        initial_score = high_score_data.total_score
        
        # Mock API: 매수 잔량이 매도 잔량의 10배 (Ratio 0.1) -> 허매수
        analyzer.collector.fetch_order_book.return_value = {
            'sell_total': 1000, 
            'buy_total': 10000 
        }
        analyzer.collector.fetch_recent_ticks.return_value = {'whale_found': False, 'whale_vol': 0.0}
        
        # Execute
        analyzer._perform_deep_analysis(high_score_data, code)
        
        # Assert
        # 점수 하락 확인
        assert high_score_data.total_score < initial_score

    @patch('kiwoom_stock.monitoring.analyzer.scoring.calculate_total_score')
    def test_integration_in_update_priority_supply(self, mock_calc_score, analyzer):
        """5. 전체 파이프라인(update_priority_supply)에서 심층 분석이 올바르게 호출되는지 통합 테스트"""
        # Setup
        stock_codes = ["005930"]
        
        # Mocking Collector Bulk Data
        analyzer.collector.fetch_program_trade.return_value = {}
        analyzer.collector.fetch_foreign_window_trade.return_value = {}
        analyzer.collector.fetch_minute_chart.return_value = [] 
        analyzer.collector.fetch_stock_basic.return_value = {'trde_pre': '0', 'trde_qty': '0', 'cur_prc': '0'}
        analyzer.collector.fetch_tick_strength.return_value = []
        
        # Mocking Score Calculation -> 80점 리턴 (심층 분석 트리거)
        mock_calc_score.return_value = {'total_score': 80.0, 'weights': {}}
        
        # Mocking Deep Analysis API
        analyzer.collector.fetch_order_book.return_value = {'sell_total': 2000, 'buy_total': 1000}
        analyzer.collector.fetch_recent_ticks.return_value = {'whale_found': True, 'whale_vol': 10.0}
        
        # Execute
        analyzer.update_priority_supply(stock_codes)
        
        # Assert
        # 1. 1차 점수가 80점으로 계산됨
        # 2. _perform_deep_analysis 내부에서 API 호출 확인
        analyzer.collector.fetch_order_book.assert_called_with("005930")
        analyzer.collector.fetch_recent_ticks.assert_called_with("005930")
        
        # 3. 데이터 객체에 최종 점수가 반영되었는지 확인
        data = analyzer.supply_cache["005930"]
        # 80점보다 높아야 함 (Whale Bonus + OrderBook Bonus)
        assert data.total_score > 80.0

    def test_api_error_handling(self, analyzer, high_score_data):
        """6. API 호출 중 에러가 발생해도 프로그램이 죽지 않고 기존 점수를 유지해야 한다."""
        # Setup
        code = high_score_data.stock_code
        high_score_data.total_score = 80.0
        initial_score = high_score_data.total_score
        
        # Mock API: 예외 발생
        analyzer.collector.fetch_order_book.side_effect = Exception("API Timeout")
        
        # Execute
        # 여기서 예외가 전파되지 않고 로그만 찍혀야 함
        analyzer._perform_deep_analysis(high_score_data, code)
        
        # Assert
        # 점수가 변하지 않았는지 확인
        assert high_score_data.total_score == initial_score