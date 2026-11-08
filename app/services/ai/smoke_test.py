"""Optional synthetic connectivity check: python -m app.services.ai.smoke_test."""

import time
import json

from app.config import get_settings
from app.services.ai.base import GenerationRequest
from app.services.ai.errors import AIProviderError
from app.services.ai.provider_factory import create_provider


def main() -> int:
    settings = get_settings()
    if not settings.ai_enabled:
        print("AI smoke test unavailable: provider_unavailable")
        return 1
    synthetic_request = GenerationRequest(
        prompt='Return only JSON with one boolean field: {"ok": true}. This is a synthetic connectivity check.',
        response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        schema_name="connectivity_check",
        max_output_tokens=64,
    )
    result = 0
    for name in ("gemini", "groq"):
        model = settings.gemini_model if name == "gemini" else settings.groq_model
        key = settings.gemini_api_key if name == "gemini" else settings.groq_api_key
        if not key or not model:
            print(f"provider={name} model={model or 'unconfigured'} result=provider_unavailable duration_ms=0")
            result = 1
            continue
        started = time.perf_counter()
        try:
            response = create_provider(name, settings=settings).generate(synthetic_request)
            payload = json.loads(response.content)
            category = "success" if isinstance(payload, dict) and set(payload) == {"ok"} and isinstance(payload["ok"], bool) else "invalid_provider_response"
        except AIProviderError as error:
            category = error.code.value
            result = 1
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        print(f"provider={name} model={model} result={category} duration_ms={elapsed_ms}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
