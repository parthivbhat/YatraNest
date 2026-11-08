"""Groq Chat Completions HTTP adapter."""

import logging
import re
import time

import httpx

from app.config import Settings
from app.services.ai.base import GenerationRequest, GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.http_common import classify_http_error, classify_transport_error
from app.services.ai.diagnostics import log_ai_diagnostic

GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
AI_REPLY_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "ai_reply_content",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "body": {"type": "string"},
            },
            "required": ["body"],
            "additionalProperties": False,
        },
    },
}
logger = logging.getLogger(__name__)


def _safe_error_details(response: httpx.Response) -> tuple[str | None, str | None, str]:
    """Extract safe identifiers from Groq's error JSON; never return raw text."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None, None, "The provider rejected the request."

    code = error.get("code")
    code = code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", code) else None
    param = error.get("param")
    param = param if isinstance(param, str) and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,100}", param) else None
    message = error.get("message")
    normalized = message.casefold() if isinstance(message, str) else ""

    if "reasoning_format" in normalized or param == "reasoning_format":
        safe_message = "The provider rejected the reasoning format configuration."
    elif "response_format" in normalized or param == "response_format":
        safe_message = "The provider rejected the JSON response configuration."
    elif "max_completion_tokens" in normalized or param == "max_completion_tokens":
        safe_message = "The provider rejected the completion token limit."
    elif "context length" in normalized or "context window" in normalized or "too many tokens" in normalized:
        safe_message = "The request exceeds the model context limit."
    elif "model" in normalized and any(term in normalized for term in ("not found", "unsupported", "unavailable")):
        safe_message = "The configured model was rejected."
    elif any(term in normalized for term in ("unsupported parameter", "unknown parameter", "unrecognized parameter")):
        safe_message = "The provider rejected an unsupported request parameter."
    elif param:
        safe_message = "The provider rejected a request field."
    else:
        safe_message = "The provider rejected the request."
    return code, param, safe_message


class GroqProvider:
    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None):
        if not settings.groq_api_key or not settings.groq_model:
            raise AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE)
        self.model = settings.groq_model
        self.api_key = settings.groq_api_key.get_secret_value()
        self.timeout = httpx.Timeout(settings.ai_timeout_seconds)
        self.transport = transport

    def generate(self, request: GenerationRequest) -> GenerationResponse:
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": request.prompt}],
            "max_completion_tokens": request.max_output_tokens,
            # Groq's strict reply schema is specific to the reply contract.
            # Extraction has a richer Pydantic schema and keeps JSON Object
            # Mode; the canonical application validator checks that payload.
            "response_format": (
                AI_REPLY_RESPONSE_FORMAT
                if request.schema_name == "ai_reply_content"
                else {"type": "json_object"}
            ),
        }
        started = time.monotonic()
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(
                    GROQ_CHAT_COMPLETIONS_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json=body,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            error = classify_transport_error(exc)
            log_ai_diagnostic(
                logger,
                logging.WARNING,
                event="ai_provider_failure",
                stage="provider_transport",
                provider="groq",
                model=self.model,
                failure_code=error.code.value,
                exception_type=type(exc).__name__,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise error from None
        response_duration_ms = round((time.monotonic() - started) * 1000)
        log_ai_diagnostic(
            logger,
            logging.INFO if response.status_code < 400 else logging.WARNING,
            event="ai_provider_http_response",
            stage="provider_http",
            provider="groq",
            model=self.model,
            status_code=response.status_code,
            duration_ms=response_duration_ms,
        )
        if response.status_code >= 400:
            error = classify_http_error(response.status_code)
            provider_error_code, provider_error_param, safe_message = _safe_error_details(response)
            log_ai_diagnostic(
                logger,
                logging.WARNING,
                event="ai_provider_api_error",
                stage="provider_api_error",
                provider="groq",
                model=self.model,
                status_code=response.status_code,
                failure_code=error.code.value,
                provider_error_code=provider_error_code,
                provider_error_param=provider_error_param,
                sanitized_provider_message=safe_message,
            )
            raise error
        try:
            payload = response.json()
            choice = payload["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise AIProviderError(AIErrorCode.CONTENT_REFUSED)
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
            request_id = payload.get("id")
            return GenerationResponse("groq", self.model, content, request_id if isinstance(request_id, str) else None)
        except AIProviderError as error:
            log_ai_diagnostic(
                logger,
                logging.WARNING,
                event="ai_provider_response_rejected",
                stage="response_extraction",
                provider="groq",
                model=self.model,
                failure_code=error.code.value,
                exception_type=type(error).__name__,
            )
            raise
        except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            log_ai_diagnostic(
                logger,
                logging.WARNING,
                event="ai_provider_response_rejected",
                stage="response_extraction",
                provider="groq",
                model=self.model,
                failure_code="invalid_provider_response",
                exception_type=type(exc).__name__,
            )
            raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE) from None
