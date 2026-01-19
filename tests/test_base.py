import pytest
import requests_mock
from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.exceptions import KiwoomAPIResponseError

def test_api_error_handling(mocker):
    # Authenticator 가짜로 만들기
    mock_auth = mocker.Mock()
    mock_auth.get_token.return_value = "fake_token"
    
    client = BaseClient(mock_auth, "https://mockapi.kiwoom.com")
    
    with requests_mock.Mocker() as m:
        # 서버가 에러 코드를 주는 상황 시뮬레이션
        m.post("https://api.com/test", json={"return_code": -100, "return_message": "에러발생"})
        
        with pytest.raises(KiwoomAPIResponseError) as exc:
            client.request("/test", "api_id", {})
        
        assert "에러발생" in str(exc.value)