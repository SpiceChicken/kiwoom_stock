from datetime import datetime, timedelta
import requests
import json

class Authenticator:
    def __init__(self, appkey, secretkey, base_url):
        self.appkey = appkey
        self.secretkey = secretkey
        self.base_url = base_url
        self._token = None
        self._token_expires_at = None

    def get_token(self):
        if self._token and datetime.now() < self._token_expires_at:
            return self._token
        
        # 1. URL 설정 (끝에 슬래시가 없는지 확인)
        url = f"{self.base_url.rstrip('/')}/oauth2/token"
        
        # 2. 키움 문서에 명시된 필수 헤더 추가
        headers = {
            "Content-Type": "application/json;charset=UTF-8"
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
            
            # 4. 상태 코드 확인 (JSON 파싱 전 필수)
            if resp.status_code != 200:
                print(f"인증 요청 실패: 상태 코드 {resp.status_code}")
                print(f"서버 응답 내용: {resp.text}") # 여기서 실제 에러 원인 확인 가능
                return None
            
            data = resp.json()
            self._token = data.get("token")
            
            # 5. 만료 시간 설정
            expires_in = data.get("expires_in", 82800)
            self._token_expires_at = datetime.now() + timedelta(seconds=expires_in)
            
            return self._token
            
        except Exception as e:
            print(f"토큰 발급 중 예외 발생: {e}")
            return None