"""Sanitized Kiwoom API exception contracts."""

from typing import Optional


class KiwoomAPIError(Exception):
    """A transport/API failure containing allowlisted metadata only."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        *,
        category: str = "api_error",
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.category = category

    def __str__(self) -> str:
        if self.status_code is not None:
            return f"[{self.status_code}] {self.message}"
        return self.message


class KiwoomAuthError(KiwoomAPIError):
    """A fail-closed token issuance or revocation error."""


class KiwoomAPIResponseError(KiwoomAPIError):
    """A valid Kiwoom response that reports a non-success return code."""

    def __init__(
        self,
        message: str,
        return_code: Optional[int] = None,
        status_code: Optional[int] = None,
        *,
        category: str = "api_rejected",
    ):
        super().__init__(message, status_code, category=category)
        self.return_code = return_code

    def __str__(self) -> str:
        base_msg = super().__str__()
        if self.return_code is not None:
            return f"{base_msg} (API Return Code: {self.return_code})"
        return base_msg

