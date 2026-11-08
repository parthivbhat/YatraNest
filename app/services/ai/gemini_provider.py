"""Google Gemini Generate Content HTTP adapter."""

from urllib.parse import quote

import httpx

from app.config import Settings
from app.services.ai.base import GenerationRequest, GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.http_common import classify_http_error, classify_transport_error

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


class GeminiProvider:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not settings.gemini_api_key or not settings.gemini_model:
            raise AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE)
        self.model = settings.gemini_model
        self.api_key = settings.gemini_api_key.get_secret_value()
        self.timeout = httpx.Timeout(settings.ai_timeout_seconds)
        self.transport = transport

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        url = f"{GEMINI_API_BASE}/{quote(self.model, safe='')}:generateContent"
        generation_config: dict[str, object] = {
            "maxOutputTokens": request.max_output_tokens,
        }
        response_format: dict[str, object] = {
            "text": {"mimeType": "APPLICATION_JSON"},
        }
        if request.response_schema is not None:
            response_format["text"]["schema"] = request.response_schema
        generation_config["responseFormat"] = response_format
        body = {
            "contents": [{"parts": [{"text": request.prompt}]}],
            "generationConfig": generation_config,
        }
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(url, headers={"x-goog-api-key": self.api_key}, json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise classify_transport_error(exc) from None
        if response.status_code >= 400:
            raise classify_http_error(response.status_code)
        try:
            payload = response.json()
            if payload.get("promptFeedback", {}).get("blockReason"):
                raise AIProviderError(AIErrorCode.CONTENT_REFUSED)
            candidate = payload["candidates"][0]
            finish_reason = candidate.get("finishReason")
            if finish_reason in {"SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"}:
                raise AIProviderError(AIErrorCode.CONTENT_REFUSED)
            parts = candidate["content"]["parts"]
            content = "".join(part["text"] for part in parts if isinstance(part.get("text"), str))
            if not content.strip():
                raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
            return GenerationResponse("gemini", self.model, content)
        except AIProviderError:
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE) from None
