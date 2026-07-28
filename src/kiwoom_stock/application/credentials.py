"""Provider-neutral Kiwoom credential values and application port."""

from dataclasses import dataclass
import re
from typing import Final, Protocol


_REDACTED: Final = "[REDACTED]"
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9\-._~+/]+=*$")


class CredentialProviderError(ValueError):
    """A credential source failed validation without exposing its contents."""


class SensitiveText:
    """A secret string that redacts implicit stringification."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise CredentialProviderError("credential value must be text")
        if (
            not value
            or value != value.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise CredentialProviderError("credential value must be non-empty")
        self._value = value

    def reveal_for_auth(self) -> str:
        """Reveal the value only at the authentication adapter boundary."""

        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({_REDACTED})"

    def __str__(self) -> str:
        return _REDACTED


class BearerToken:
    """An in-memory access token that cannot be printed accidentally.

    This wrapper limits implicit copies and serialization; it does not claim to
    encrypt or zeroize Python process memory.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or _BEARER_TOKEN.fullmatch(value) is None
        ):
            raise CredentialProviderError("access token has an invalid bearer form")
        self._value = value

    def reveal_for_authorization(self) -> str:
        """Reveal only while assembling the outbound Authorization header."""

        return self._value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({_REDACTED})"

    def __str__(self) -> str:
        return _REDACTED


@dataclass(frozen=True, repr=False)
class KiwoomClientCredentials:
    """The two OAuth client credentials; neither field is printable."""

    app_key: SensitiveText
    secret_key: SensitiveText

    def __post_init__(self) -> None:
        if not isinstance(self.app_key, SensitiveText):
            raise TypeError("app_key must be SensitiveText")
        if not isinstance(self.secret_key, SensitiveText):
            raise TypeError("secret_key must be SensitiveText")


class CredentialProvider(Protocol):
    """Provider-neutral startup credential boundary."""

    def load(self) -> KiwoomClientCredentials:
        """Resolve one immutable credential bundle."""
