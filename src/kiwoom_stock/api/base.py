import requests
import json
import time
from .exceptions import KiwoomAPIError, KiwoomAPIResponseError

class BaseClient:
    def __init__(self, authenticator, base_url):
        self.auth = authenticator
        self.base_url = base_url

    def request(self, endpoint, api_id, payload, max_retries=3):
        """
        타임아웃 및 네트워크 에러에 대응하는 고도화된 요청 메서드
        """
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": f"Bearer {self.auth.get_token()}"
        }
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        
        for attempt in range(max_retries):
            try:
                # 연결 타임아웃 5초, 읽기 타임아웃 30초 설정
                response = requests.post(url, headers=headers, json=payload, timeout=(5, 30))
                
                if response.status_code != 200:
                    raise KiwoomAPIError(f"HTTP {response.status_code}", status_code=response.status_code)

                data = response.json()

                if data.get("return_code") != 0:
                    raise KiwoomAPIResponseError(
                        data.get("return_message", "알 수 없는 API 오류"),
                        return_code=data.get("return_code"),
                        response_data=data
                    )

                return data

            # --- [에러별 정밀 대응 섹션] ---
            except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectTimeout) as e:
                if attempt < max_retries - 1:
                    wait = (attempt + 1) * 2
                    print(f"⚠️ 타임아웃 발생 ({type(e).__name__}). {wait}초 후 재시도 ({attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
                raise KiwoomAPIError("서버 응답 시간이 초과되었습니다. (Timeout)")

            except requests.exceptions.ConnectionError as e:
                # 래핑된 ReadTimeout 확인
                if "Read timed out" in str(e):
                    if attempt < max_retries - 1:
                        wait = (attempt + 1) * 2
                        print(f"⚠️ 데이터 수신 중 지연 발생. {wait}초 후 재시도 ({attempt + 1}/{max_retries})")
                        time.sleep(wait)
                        continue
                    raise KiwoomAPIError("데이터 수신 중 타임아웃이 발생했습니다. (Read Timeout)")
                
                raise KiwoomAPIError(f"서버 연결 실패: {str(e)}")

            except Exception as e:
                if not isinstance(e, KiwoomAPIError):
                    print(f"DEBUG: 예상치 못한 에러 - {type(e).__name__}: {e}")
                raise e