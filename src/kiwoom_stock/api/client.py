from .auth import Authenticator
from .base import BaseClient
from .services.account import AccountService
from .services.market import MarketService

class KiwoomClient:
    def __init__(self, appkey, secretkey, base_url):
        # 1. 인증 및 통신 계층 조립
        self.authenticator = Authenticator(appkey, secretkey, base_url)
        self.base = BaseClient(self.authenticator, base_url)
        
        # 2. 도메인 서비스 네임스페이스 제공
        self.account = AccountService(self.base)
        self.market = MarketService(self.base)