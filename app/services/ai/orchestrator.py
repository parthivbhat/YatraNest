"""Bounded retry and controlled Gemini-to-Groq fallback orchestration."""

import time
from typing import Callable

import httpx

from app.config import Settings, get_settings
from app.services.ai.base import GenerationRequest, GenerationResponse, validate_structured_output
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.provider_factory import create_provider


def _retry_provider(
    provider_name: str,
    request: GenerationRequest,
    settings: Settings,
    *,
    transport_factory: Callable[[str], httpx.BaseTransport | None] | None,
    sleeper: Callable[[float], None],
) -> GenerationResponse:
    provider = create_provider(
        provider_name,
        settings=settings,
        transport=transport_factory(provider_name) if transport_factory else None,
    )
    attempts = settings.ai_max_retries + 1
    for attempt in range(attempts):
        try:
            response = provider.generate(request)
            if request.output_model is not None:
                parsed = validate_structured_output(response.content, request.output_model)
                response = GenerationResponse(
                    provider=response.provider,
                    model=response.model,
                    content=response.content,
                    request_id=response.request_id,
                    generated_at=response.generated_at,
                    structured_data=parsed,
                )
            return response
        except AIProviderError as error:
            if not error.retryable or attempt + 1 >= attempts:
                raise
            sleeper(min(0.25 * (2**attempt), 1.0))
    raise AIProviderError(AIErrorCode.GENERATION_FAILED)


def generate(
    request: GenerationRequest,
    *,
    settings: Settings | None = None,
    transport_factory: Callable[[str], httpx.BaseTransport | None] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> GenerationResponse:
    """Generate once logically; retry transient failures and optionally use Groq fallback."""
    configured = settings or get_settings()
    if not configured.ai_enabled:
        raise AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE)
    primary = configured.ai_provider
    try:
        return _retry_provider(
            primary, request, configured, transport_factory=transport_factory, sleeper=sleeper
        )
    except AIProviderError as error:
        # Only a transient primary Gemini failure can trigger the configured Groq fallback.
        if primary != "gemini" or not configured.ai_fallback_enabled or not error.retryable:
            raise
    return _retry_provider(
        "groq", request, configured, transport_factory=transport_factory, sleeper=sleeper
    )
