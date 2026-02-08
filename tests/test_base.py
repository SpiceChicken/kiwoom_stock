# tests/test_base.py
import pytest
import requests_mock
from kiwoom_stock.api.base import BaseClient
from kiwoom_stock.api.exceptions import KiwoomAPIResponseError

def test_api_error_handling(mocker):
    # Authenticator 가짜로 만들기
    mock_auth = mocker.Mock()
    mock_auth.get_token.return_value = "fake_token"
    
    # [수정] Mock URL과 Base URL을 일치시킴
    base_url = "https://mockapi.kiwoom.com"
    client = BaseClient(mock_auth, base_url)
    
    with requests_mock.Mocker() as m:
        # [수정] 클라이언트가 호출할 URL을 정확히 가로채도록 설정
        m.post(f"{base_url}/test", json={"return_code": -100, "return_message": "에러발생"})
        
        with pytest.raises(KiwoomAPIResponseError) as exc:
            client.request("/test", "api_id", {})
        
        assert "에러발생" in str(exc.value)