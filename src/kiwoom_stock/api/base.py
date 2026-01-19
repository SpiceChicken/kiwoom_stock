import requests
import json
from .exceptions import KiwoomAPIError, KiwoomAPIResponseError

class BaseClient:
    def __init__(self, authenticator, base_url):
        self.auth = authenticator
        self.base_url = base_url

    def request(self, endpoint, api_id, payload):
        """
        모든 API 요청을 처리하며 예외를 단계별로 검증합니다.
        """
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
            "authorization": f"Bearer {self.auth.get_token()}"
        }
        url = f"{self.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        
        try:
            # 1. 네트워크 요청 수행
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            
            # 2. HTTP 상태 코드 검증 (404, 500 등 방지)
            if response.status_code != 200:
                print(f"DEBUG: HTTP 오류 발생 ({response.status_code}) - {response.text}")
                raise KiwoomAPIError(
                    f"서버가 에러를 반환했습니다. HTTP {response.status_code}",
                    status_code=response.status_code
                )

            # 3. JSON 파싱 검증 (HTML 응답 등으로 인한 JSONDecodeError 방지)
            try:
                data = response.json()
            except requests.exceptions.JSONDecodeError:
                print(f"DEBUG: JSON 파싱 실패 - 응답 내용: {response.text}")
                raise KiwoomAPIError("서버 응답이 JSON 형식이 아닙니다.")

            # 4. 키움 API 비즈니스 로직 에러 검증 (return_code)
            if data.get("return_code") != 0:
                # API ID가 au10001인 경우 등 예외 케이스 처리 가능
                raise KiwoomAPIResponseError(
                    data.get("return_message", "알 수 없는 API 오류"),
                    return_code=data.get("return_code"),
                    response_data=data
                )

            return data

        except requests.exceptions.Timeout:
            raise KiwoomAPIError("API 요청 시간이 초과되었습니다. (Timeout)")
        
        except requests.exceptions.ConnectionError:
            raise KiwoomAPIError("서버와의 연결에 실패했습니다. (Network Error)")

        except requests.exceptions.RequestException as e:
            # 기타 requests 관련 모든 예외 처리
            raise KiwoomAPIError(f"API 요청 중 예상치 못한 오류 발생: {e}")

        except Exception as e:
            # 시스템 레벨의 예외 처리
            if not isinstance(e, KiwoomAPIError):
                print(f"DEBUG: 정의되지 않은 에러 발생 - {e}")
            raise e