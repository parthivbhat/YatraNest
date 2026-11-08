"""Shared provider request/response types and strict structured-output boundary."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from typing import Any

from pydantic import BaseModel, ValidationError

from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.diagnostics import log_ai_diagnostic


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationRequest:
    prompt: str
    response_schema: dict[str, Any] | None = None
    schema_name: str = "structured_response"
    max_output_tokens: int = 1024
    output_model: type[BaseModel] | None = None


@dataclass(frozen=True)
class GenerationResponse:
    provider: str
    model: str
    content: str
    request_id: str | None = None
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    structured_data: BaseModel | None = None


def validate_structured_output(content: str, schema: type[BaseModel]) -> BaseModel:
    """Validate provider JSON without coercion, repair, or unknown-field loss."""
    try:
        json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_structured_output_rejected",
            stage="structured_output_json_parse",
            failure_code="invalid_provider_response",
            exception_type=type(exc).__name__,
        )
        raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE) from None
    try:
        return schema.model_validate_json(content, strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_structured_output_rejected",
            stage="structured_output_schema_validation",
            failure_code="validation_failed",
            exception_type=type(exc).__name__,
        )
        raise AIProviderError(AIErrorCode.VALIDATION_FAILED) from None
