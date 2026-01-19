from datetime import datetime, timedelta
from kiwoom_stock.api.auth import Authenticator

def test_token_caching(mocker):
    auth = Authenticator("appkey", "secret", "https://mockapi.kiwoom.com")
    
    # 가짜 토큰 세팅
    auth._token = "old_token"
    auth._token_expires_at = datetime.now() + timedelta(hours=1)
    
    # get_token 호출 시 새 요청을 보내지 않고 기존 토큰을 반환해야 함
    token = auth.get_token()
    assert token == "old_token"