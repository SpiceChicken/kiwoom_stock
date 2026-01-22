import socket
import time
import logging

from .auth import Authenticator
from .base import BaseClient
from .services.account import AccountService
from .services.market import MarketService

logger = logging.getLogger(__name__)

class KiwoomClient:
    def __init__(self, appkey, secretkey, base_url):
        # 1. 인증 및 통신 계층 조립
        self.authenticator = Authenticator(appkey, secretkey, base_url)
        self.base = BaseClient(self.authenticator, base_url)
        
        # 2. 도메인 서비스 네임스페이스 제공
        self.account = AccountService(self.base)
        self.market = MarketService(self.base)

    def _wait_for_connectivity(self, timeout=60):
        """API 서버 도메인이 해석될 때까지 대기합니다."""
        host = self.base_url.replace("https://", "").replace("http://", "").split("/")[0]
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                socket.gethostbyname(host)
                return True
            except socket.gaierror:
                time.sleep(2)
        
        raise ConnectionError(f"네트워크 준비 실패: {host} 주소를 찾을 수 없습니다.")