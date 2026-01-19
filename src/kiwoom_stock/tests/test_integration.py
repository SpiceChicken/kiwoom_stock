import pytest
from kiwoom_stock.api.client import KiwoomClient
from kiwoom_stock.utils.config import load_config, get_base_url

@pytest.fixture
def client():
    config = load_config() # 실제 appkey, secretkey 로드
    return KiwoomClient(
        appkey=config['appkey'],
        secretkey=config['secretkey'],
        base_url=get_base_url())

def test_real_portfolio_call(client):
    """실제 계좌 정보를 한 번 가져와보는지 테스트"""
    try:
        data = client.account.get_portfolio()
        assert "acnt_nm" in data  # 응답에 계좌명이 포함되어 있는지 확인
    except Exception as e:
        pytest.fail(f"실제 API 호출 중 오류 발생: {e}")

def test_real_chart_call(client):
    """삼성전자(005930) 차트 데이터를 실제로 가져오는지 테스트"""
    prices = client.market.get_minute_chart("005930")
    assert len(prices) > 0
    assert "close" in prices[0]