"""Common safe HTTP status and transport classification."""

import httpx

from app.services.ai.errors import AIErrorCode, AIProviderError


def classify_http_error(status_code: int) -> AIProviderError:
    status = {"provider_status_code": status_code}
    if status_code in {401, 403}:
        return AIProviderError(AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, **status)
    if status_code == 429:
        return AIProviderError(AIErrorCode.PROVIDER_RATE_LIMITED, retryable=True, **status)
    if status_code in {408, 500, 502, 503, 504}:
        return AIProviderError(AIErrorCode.GENERATION_FAILED, retryable=True, **status)
    if status_code in {400, 404, 405, 413, 415, 422}:
        return AIProviderError(AIErrorCode.PROVIDER_INVALID_REQUEST, **status)
    return AIProviderError(AIErrorCode.GENERATION_FAILED, **status)


def classify_transport_error(error: Exception) -> AIProviderError:
    if isinstance(error, httpx.TimeoutException):
        return AIProviderError(AIErrorCode.PROVIDER_TIMEOUT, retryable=True)
    if isinstance(error, httpx.TransportError):
        return AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE, retryable=True)
    return AIProviderError(AIErrorCode.GENERATION_FAILED)
