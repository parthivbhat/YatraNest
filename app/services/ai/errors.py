"""Safe normalized provider errors; messages never contain provider payloads."""

from enum import StrEnum


class AIErrorCode(StrEnum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_AUTHENTICATION_FAILED = "provider_authentication_failed"
    PROVIDER_INVALID_REQUEST = "provider_invalid_request"
    INVALID_PROVIDER_RESPONSE = "invalid_provider_response"
    VALIDATION_FAILED = "validation_failed"
    GENERATION_FAILED = "generation_failed"
    CONTENT_REFUSED = "content_refused"


class AIProviderError(Exception):
    """Provider failure with a fixed code and retry classification only."""

    def __init__(
        self,
        code: AIErrorCode,
        *,
        retryable: bool = False,
        provider_status_code: int | None = None,
    ):
        self.code = code
        self.retryable = retryable
        self.provider_status_code = provider_status_code
        super().__init__(code.value)
