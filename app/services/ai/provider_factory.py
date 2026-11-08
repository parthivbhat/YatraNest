"""Create a configured provider without requiring AI credentials at startup."""

from typing import Callable

import httpx

from app.config import Settings, get_settings
from app.services.ai.base import GenerationRequest, GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.gemini_provider import GeminiProvider
from app.services.ai.groq_provider import GroqProvider


class ProviderAdapter:
    def __init__(self, name: str, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        self.name = name
        self.provider = (GeminiProvider if name == "gemini" else GroqProvider)(settings, transport=transport)

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        return self.provider.generate(request)


def create_provider(
    name: str | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.BaseTransport | None = None,
) -> ProviderAdapter:
    configured = settings or get_settings()
    selected = name or configured.ai_provider
    if selected not in {"gemini", "groq"}:
        raise AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE)
    return ProviderAdapter(selected, configured, transport=transport)
