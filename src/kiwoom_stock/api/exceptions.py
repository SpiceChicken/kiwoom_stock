"""
키움 API 커스텀 예외 클래스
"""


class KiwoomAPIError(Exception):
    """키움 API 기본 예외 클래스"""
    
    def __init__(self, message: str, status_code: int = None, response_data: dict = None):
        """
        Args:
            message: 에러 메시지
            status_code: HTTP 상태 코드 (있는 경우)
            response_data: API 응답 데이터 (있는 경우)
        """
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_data = response_data
    
    def __str__(self):
        if self.status_code:
            return f"[{self.status_code}] {self.message}"
        return self.message


class KiwoomAuthError(KiwoomAPIError):
    """키움 API 인증 관련 예외"""
    pass


class KiwoomAPIResponseError(KiwoomAPIError):
    """키움 API 응답 오류 예외"""
    
    def __init__(self, message: str, return_code: int = None, return_message: str = None, 
                 status_code: int = None, response_data: dict = None):
        """
        Args:
            message: 에러 메시지
            return_code: API 반환 코드
            return_message: API 반환 메시지
            status_code: HTTP 상태 코드
            response_data: 전체 응답 데이터
        """
        super().__init__(message, status_code, response_data)
        self.return_code = return_code
        self.return_message = return_message
    
    def __str__(self):
        base_msg = super().__str__()
        if self.return_code is not None:
            return f"{base_msg} (API Return Code: {self.return_code}, Message: {self.return_message})"
        return base_msg

