import requests
from .exceptions import KiwoomAPIResponseError

class BaseClient:
    def __init__(self, authenticator, base_url):
        self.auth = authenticator
        self.base_url = base_url

    def request(self, endpoint, api_id, payload):
        headers = {
            "api-id": api_id,
            "authorization": f"Bearer {self.auth.get_token()}"
        }
        url = f"{self.base_url}{endpoint}"
        
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        
        if data.get("return_code") != 0:
            raise KiwoomAPIResponseError(data.get("return_message"), response_data=data)
        return data