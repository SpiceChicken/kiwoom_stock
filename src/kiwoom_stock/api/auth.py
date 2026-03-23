from datetime import datetime, timedelta
import requests
import json
import logging
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type

logger = logging.getLogger(__name__)

class RateLimitExceededError(Exception):
    pass

class Authenticator:
    def __init__(self, appkey, secretkey, base_url):
        self.appkey = appkey
        self.secretkey = secretkey
        self.base_url = base_url
        self._token = None
        self._token_expires_at = None

    @retry(
        wait=wait_exponential(multiplier=2, min=2, max=10),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(RateLimitExceededError),
        reraise=True
    )
    def get_token(self):
        if self._token and datetime.now() < self._token_expires_at:
            return self._token
        
        # 1. URL 설정 (끝에 슬래시가 없는지 확인)
        url = f"{self.base_url.rstrip('/')}/oauth2/token"
        
        # 2. 키움 문서에 명시된 필수 헤더 추가
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": "au10001"  # 필수 항목 추가
        }
        
        # 3. 바디 데이터
        payload = {
            "grant_type": "client_credentials", 
            "appkey": self.appkey, 
            "secretkey": self.secretkey
        }
        try:
            # 헤더(headers=headers)를 반드시 포함해야 합니다.
            resp = requests.post(url, headers=headers, data=json.dumps(payload), timeout=10)
            
            # 💡 [핵심 추가 1] 429 에러 발생 시 커스텀 예외를 던져 @retry 를 발동시킵니다.
            if resp.status_code == 429:
                logger.warning("⚠️ [429 Error] 키움 서버 인증 한도 초과. 지수 백오프 재시도를 발동합니다.")
                raise RateLimitExceededError("429 Too Many Requests")
            
            # 4. 상태 코드 확인 (JSON 파싱 전 필수)
            if resp.status_code != 200:
                logger.info(f"인증 요청 실패: 상태 코드 {resp.status_code}")
                logger.info(f"서버 응답 내용: {resp.text}") # 여기서 실제 에러 원인 확인 가능
                return None
            
            data = resp.json()
            self._token = data.get("token")
            
            # 5. 만료 시간 설정
            expires_in = data.get("expires_in", 82800)
            self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)
            
            return self._token
            
        # 💡 [핵심 추가 2] RateLimitExceededError는 먹히지 않고 밖으로 빠져나가게 예외 처리 분리
        except RateLimitExceededError:
            raise
        except Exception as e:
            logger.info(f"토큰 발급 중 예외 발생: {e}")
            return None