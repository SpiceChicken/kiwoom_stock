import socket
import time
import logging
from urllib.parse import urlparse # URL 파싱을 위해 추가

from .auth import Authenticator
from .base import BaseClient
from .services.account import AccountService
from .services.market import MarketService

logger = logging.getLogger(__name__)

class KiwoomClient:
    def __init__(self, appkey, secretkey, base_url):
        # 1. 인증 및 통신 계층 조립
        self.auth = Authenticator(appkey, secretkey, base_url)

        # 단순 네트워크 대기가 아닌 '인증 완결' 대기로 변경
        self._wait_for_ready(base_url)

        # BaseClient 생성 (Retry 및 공통 요청 관리)
        self.base = BaseClient(self.auth, base_url)
        
        # 도메인 서비스에 BaseClient 주입
        self.account = AccountService(self.base)
        self.market = MarketService(self.base)
 

    def _wait_for_ready(self, base_url, timeout=300):
        """네트워크 연결 및 API 인증이 완료될 때까지 재시도하며 대기합니다."""
        start_time = time.time()
        logger.info("🌐 시스템 준비 상태를 점검합니다 (네트워크 및 인증)...")

        # [수정] base_url에서 호스트 주소만 추출 (https:// 제거)
        # socket.create_connection은 'https://...' 형태를 인식하지 못합니다.
        parsed_url = urlparse(base_url)
        host = parsed_url.hostname
        
        logger.info(f"🌐 시스템 준비 상태 점검 중... (대상: {host})")
        
        while time.time() - start_time < timeout:
            try:
                # 단계 1: 기본 소켓 연결 확인 (DNS 및 물리망 체크)
                socket.create_connection((host, 443), timeout=5)
                
                # 단계 2: 실전 토큰 발급 시도 (인증 성공 여부 체크)
                # Authenticator 내부에 토큰 발급 로직이 수행되도록 호출
                token = self.auth.get_token()
                if token:
                    logger.info("✅ 서버 연결 및 인증에 최종 성공했습니다.")
                    return True
                
            except (socket.timeout, OSError, Exception) as e:
                # NameResolutionError 등 인터넷 미연결 시 발생하는 모든 예외 처리
                logger.warning(f"⏳ 연결 대기 중... (사유: {str(e).split(')')[0]})")
                time.sleep(10) # 부팅 시 안정화를 위해 대기 시간을 조금 더 늘림
                
        raise ConnectionError("🚨 네트워크 인증 시도 시간이 초과되었습니다. 인터넷 연결을 확인하세요.")