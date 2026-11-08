"""Deterministic tests for provider adapters, orchestration and validation."""

import json
import logging

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.schemas.ai_extraction import EnquiryExtraction
from app.schemas.ai_reply import AIReplyContent
from app.services.ai.base import GenerationRequest, validate_structured_output
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.gemini_provider import GeminiProvider
from app.services.ai.groq_provider import GroqProvider
from app.services.ai.orchestrator import generate
from app.services.ai.provider_factory import create_provider


GEMINI_JSON = json.dumps({
    "schema_version": "1.0", "destination": "Kerala", "travel_start_date": None,
    "travel_end_date": None, "original_travel_period_text": "December", "adults": 2,
    "children": 1, "infants": 0, "budget_amount": "120000", "budget_currency": "INR",
    "budget_scope": "total", "preferences": ["Houseboat"],
    "missing_information": ["travel_start_date", "travel_end_date"],
    "ambiguous_information": ["travel_year"],
})


def settings(**overrides):
    values = {
        "ai_enabled": True, "ai_provider": "gemini", "gemini_api_key": "test-gemini-secret",
        "gemini_model": "configured-gemini-model", "groq_api_key": "test-groq-secret",
        "groq_model": "configured-groq-model", "ai_timeout_seconds": 5,
        "ai_max_retries": 0, "ai_fallback_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def transport_with(status=200, payload=None, *, handler=None):
    def respond(request):
        if handler:
            return handler(request)
        return httpx.Response(status, json=payload or {})
    return httpx.MockTransport(respond)


def test_configuration_optional_bounded_and_secret_safe():
    config = Settings(_env_file=None)
    assert config.ai_enabled is False
    assert config.ai_provider == "gemini"
    assert config.gemini_api_key is None
    assert config.groq_api_key is None
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_provider="other")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_timeout_seconds=61)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_max_retries=3)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ai_max_retries=-1)
    assert "test-gemini-secret" not in repr(settings())


def test_provider_factory_missing_credentials_is_safe():
    with pytest.raises(AIProviderError) as error:
        create_provider("gemini", settings=settings(gemini_api_key=None))
    assert error.value.code == AIErrorCode.PROVIDER_UNAVAILABLE
    assert "test-gemini-secret" not in str(error.value)
    with pytest.raises(AIProviderError) as error:
        create_provider("groq", settings=settings(groq_model=None))
    assert error.value.code == AIErrorCode.PROVIDER_UNAVAILABLE
    with pytest.raises(AIProviderError) as error:
        create_provider("gemini", settings=settings(gemini_model=None))
    assert error.value.code == AIErrorCode.PROVIDER_UNAVAILABLE


def test_gemini_success_uses_configured_endpoint_header_and_json_mode():
    def handler(request):
        assert request.url.path.endswith("/configured-gemini-model:generateContent")
        assert request.headers["x-goog-api-key"] == "test-gemini-secret"
        body = json.loads(request.content)
        response_format = body["generationConfig"]["responseFormat"]
        assert response_format["text"]["mimeType"] == "APPLICATION_JSON"
        assert response_format["text"]["schema"] == {
            "type": "object", "properties": {"ok": {"type": "boolean"}}
        }
        assert "responseSchema" not in body["generationConfig"]
        assert "responseMimeType" not in body["generationConfig"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": GEMINI_JSON}]}, "finishReason": "STOP"}]})
    response = GeminiProvider(settings(), transport=transport_with(handler=handler)).generate(
        GenerationRequest("synthetic", response_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}})
    )
    assert response.provider == "gemini"
    assert response.model == "configured-gemini-model"
    assert response.content == GEMINI_JSON


@pytest.mark.parametrize("status,code,retryable", [
    (401, AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, False),
    (403, AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, False),
    (429, AIErrorCode.PROVIDER_RATE_LIMITED, True),
    (503, AIErrorCode.GENERATION_FAILED, True),
    (400, AIErrorCode.PROVIDER_INVALID_REQUEST, False),
])
def test_gemini_http_error_classification(status, code, retryable):
    with pytest.raises(AIProviderError) as error:
        GeminiProvider(settings(), transport=transport_with(status, {"error": {"message": "secret payload"}})).generate(GenerationRequest("synthetic"))
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert "secret payload" not in str(error.value)


@pytest.mark.parametrize("payload", [
    {}, {"candidates": []}, {"candidates": [{"content": {"parts": []}}]},
    {"candidates": [{"content": {"parts": [{"text": ""}]}}]},
])
def test_gemini_malformed_or_empty_response(payload):
    with pytest.raises(AIProviderError) as error:
        GeminiProvider(settings(), transport=transport_with(payload=payload)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.INVALID_PROVIDER_RESPONSE


def test_gemini_blocked_response_is_not_retryable():
    body = {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}
    with pytest.raises(AIProviderError) as error:
        GeminiProvider(settings(), transport=transport_with(payload=body)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.CONTENT_REFUSED
    assert not error.value.retryable


def test_gemini_timeout_is_transient():
    def handler(_request):
        raise httpx.ReadTimeout("sensitive timeout details")
    with pytest.raises(AIProviderError) as error:
        GeminiProvider(settings(), transport=transport_with(handler=handler)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.PROVIDER_TIMEOUT
    assert error.value.retryable
    assert "sensitive" not in str(error.value)


def test_groq_timeout_is_transient():
    def handler(_request):
        raise httpx.ConnectTimeout("sensitive timeout details")
    with pytest.raises(AIProviderError) as error:
        GroqProvider(settings(), transport=transport_with(handler=handler)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.PROVIDER_TIMEOUT
    assert error.value.retryable


def test_groq_success_uses_bearer_header_and_strict_reply_json_schema():
    def handler(request):
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-groq-secret"
        body = json.loads(request.content)
        assert body["model"] == "configured-groq-model"
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {
                "name": "ai_reply_content",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {"body": {"type": "string"}},
                    "required": ["body"],
                    "additionalProperties": False,
                },
            },
        }
        return httpx.Response(200, json={"id": "safe-request-id", "choices": [{"message": {"content": '{"body":"A concise synthetic reply."}'}, "finish_reason": "stop"}]})
    response = GroqProvider(settings(), transport=transport_with(handler=handler)).generate(
        GenerationRequest("synthetic", schema_name="ai_reply_content")
    )
    assert response.provider == "groq"
    assert response.model == "configured-groq-model"
    assert response.request_id == "safe-request-id"


@pytest.mark.parametrize("content,expected", [
    ('{"body":"A concise synthetic reply."}', "A concise synthetic reply."),
    ('{"body":"A concise synthetic reply.","extra":"rejected"}', None),
    ('{"extra":"rejected"}', None),
    ('{"body":""}', None),
])
def test_groq_reply_json_schema_still_passes_application_validation(content, expected):
    config = settings(ai_provider="groq", ai_max_retries=0)
    result = None
    error = None
    try:
        result = generate(
            GenerationRequest("synthetic prompt", schema_name="ai_reply_content", output_model=AIReplyContent),
            settings=config,
            transport_factory=lambda _provider: transport_with(
                200, {"choices": [{"message": {"content": content}}]}
            ),
            sleeper=lambda _: None,
        )
    except AIProviderError as caught:
        error = caught

    if expected is None:
        assert error is not None
        assert error.code == AIErrorCode.VALIDATION_FAILED
        assert result is None
    else:
        assert error is None
        assert isinstance(result.structured_data, AIReplyContent)
        assert result.structured_data.body == expected


@pytest.mark.parametrize("status,code,retryable", [
    (401, AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, False),
    (429, AIErrorCode.PROVIDER_RATE_LIMITED, True),
    (500, AIErrorCode.GENERATION_FAILED, True),
    (400, AIErrorCode.PROVIDER_INVALID_REQUEST, False),
])
def test_groq_http_error_classification(status, code, retryable):
    with pytest.raises(AIProviderError) as error:
        GroqProvider(settings(), transport=transport_with(status, {"error": {"message": "do not leak"}})).generate(GenerationRequest("synthetic"))
    assert (error.value.code, error.value.retryable) == (code, retryable)
    assert "do not leak" not in str(error.value)


def test_groq_http_diagnostic_logs_status_and_safe_code_only(caplog):
    caplog.set_level("WARNING")
    private_marker = "SYNTHETIC-PROVIDER-BODY-MUST-NOT-BE-LOGGED"
    with pytest.raises(AIProviderError):
        GroqProvider(
            settings(),
            transport=transport_with(401, {"error": {"message": private_marker}}),
        ).generate(GenerationRequest("synthetic prompt that must not be logged"))
    assert '"stage":"provider_api_error"' in caplog.text
    assert '"provider_status_code":401' in caplog.text
    assert '"failure_code":"provider_authentication_failed"' in caplog.text
    assert private_marker not in caplog.text
    assert "synthetic prompt that must not be logged" not in caplog.text


def test_groq_400_logs_safe_error_code_param_and_sanitized_message(caplog):
    caplog.set_level("WARNING")
    private_marker = "PRIVATE-PROMPT-AND-CUSTOMER-CONTENT-MARKER"
    error_body = {
        "error": {
            "message": f"reasoning_format is incompatible with JSON mode; echoed content {private_marker}",
            "type": "invalid_request_error",
            "param": "reasoning_format",
            "code": "invalid_parameter",
        }
    }
    with pytest.raises(AIProviderError) as error:
        GroqProvider(
            settings(), transport=transport_with(400, error_body)
        ).generate(GenerationRequest("synthetic private prompt marker"))
    assert error.value.code == AIErrorCode.PROVIDER_INVALID_REQUEST
    assert '"provider_error_code":"invalid_parameter"' in caplog.text
    assert '"provider_error_param":"reasoning_format"' in caplog.text
    assert '"sanitized_provider_message":"The provider rejected the reasoning format configuration."' in caplog.text
    assert private_marker not in caplog.text
    assert "synthetic private prompt marker" not in caplog.text
    assert "incompatible with JSON mode" not in caplog.text


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"message": {"content": None}}]}])
def test_groq_malformed_or_empty_response(payload):
    with pytest.raises(AIProviderError) as error:
        GroqProvider(settings(), transport=transport_with(payload=payload)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.INVALID_PROVIDER_RESPONSE


def test_groq_refusal_and_unsupported_format_are_safe_failures():
    refusal = {"choices": [{"message": {"content": None, "refusal": "private"}}]}
    with pytest.raises(AIProviderError) as error:
        GroqProvider(settings(), transport=transport_with(payload=refusal)).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.CONTENT_REFUSED
    with pytest.raises(AIProviderError) as error:
        GroqProvider(settings(), transport=transport_with(400, {"error": {"message": "response format unsupported"}})).generate(GenerationRequest("synthetic"))
    assert error.value.code == AIErrorCode.PROVIDER_INVALID_REQUEST
    assert "unsupported" not in str(error.value)


def test_transient_gemini_failure_falls_back_and_attributes_groq():
    calls = {"gemini": 0, "groq": 0}
    def factory(name):
        def handler(_request):
            calls[name] += 1
            if name == "gemini":
                return httpx.Response(503, json={})
            return httpx.Response(200, json={"choices": [{"message": {"content": GEMINI_JSON}}]})
        return httpx.MockTransport(handler)
    config = settings(ai_max_retries=1)
    result = generate(GenerationRequest("synthetic", output_model=EnquiryExtraction), settings=config, transport_factory=factory, sleeper=lambda _: None)
    assert (calls["gemini"], calls["groq"]) == (2, 1)
    assert result.provider == "groq"
    assert result.structured_data.destination == "Kerala"


def test_fallback_failure_returns_safe_groq_category():
    def factory(name):
        status = 503 if name == "gemini" else 429
        return transport_with(status, {"error": {"message": "private provider payload"}})
    with pytest.raises(AIProviderError) as error:
        generate(
            GenerationRequest("synthetic"), settings=settings(ai_max_retries=0),
            transport_factory=factory,
        )
    assert error.value.code == AIErrorCode.PROVIDER_RATE_LIMITED
    assert "private provider payload" not in str(error.value)


@pytest.mark.parametrize("bad_payload,expected", [
    ("not-json", AIErrorCode.INVALID_PROVIDER_RESPONSE),
    ('{"unknown": true}', AIErrorCode.VALIDATION_FAILED),
])
def test_invalid_structured_output_does_not_fallback(bad_payload, expected):
    calls = []
    def factory(name):
        calls.append(name)
        def handler(_request):
            if name == "gemini":
                return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": bad_payload}]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": GEMINI_JSON}}]})
        return httpx.MockTransport(handler)
    with pytest.raises(AIProviderError) as error:
        generate(GenerationRequest("synthetic", output_model=EnquiryExtraction), settings=settings(), transport_factory=factory)
    assert error.value.code == expected
    assert calls == ["gemini"]


@pytest.mark.parametrize("failure_code", [AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, AIErrorCode.INVALID_PROVIDER_RESPONSE])
def test_nontransient_gemini_failure_does_not_fallback(failure_code):
    calls = []
    def factory(name):
        calls.append(name)
        status = 401 if failure_code == AIErrorCode.PROVIDER_AUTHENTICATION_FAILED else 200
        payload = {} if status == 401 else {"candidates": []}
        return transport_with(status, payload)
    with pytest.raises(AIProviderError) as error:
        generate(GenerationRequest("synthetic"), settings=settings(), transport_factory=factory)
    assert error.value.code == failure_code
    assert calls == ["gemini"]


def test_disabled_ai_and_missing_fallback_are_safe():
    with pytest.raises(AIProviderError) as error:
        generate(GenerationRequest("synthetic"), settings=settings(ai_enabled=False))
    assert error.value.code == AIErrorCode.PROVIDER_UNAVAILABLE
    with pytest.raises(AIProviderError) as error:
        generate(
            GenerationRequest("synthetic"),
            settings=settings(groq_api_key=None, ai_max_retries=0),
            transport_factory=lambda _name: transport_with(503, {}),
            sleeper=lambda _: None,
        )
    assert error.value.code == AIErrorCode.PROVIDER_UNAVAILABLE


def test_structured_extraction_validation_is_strict():
    parsed = validate_structured_output(GEMINI_JSON, EnquiryExtraction)
    assert parsed.destination == "Kerala"
    with pytest.raises(AIProviderError) as error:
        validate_structured_output('{"unexpected": 2}', EnquiryExtraction)
    assert error.value.code == AIErrorCode.VALIDATION_FAILED
    with pytest.raises(AIProviderError) as error:
        validate_structured_output("{bad", EnquiryExtraction)
    assert error.value.code == AIErrorCode.INVALID_PROVIDER_RESPONSE


@pytest.mark.parametrize("field,value", [
    ("travel_start_date", "2026-02-30"),
    ("adults", -1),
    ("budget_currency", "NOT-A-CURRENCY"),
    ("budget_amount", "-0.01"),
])
def test_invalid_extraction_values_are_rejected_without_repair(field, value):
    payload = {
        "schema_version": "1.0", "destination": "Kerala",
        "travel_start_date": "2026-12-01", "travel_end_date": "2026-12-07",
        "original_travel_period_text": None, "adults": 2, "children": 0, "infants": 0,
        "budget_amount": "10000.00", "budget_currency": "INR", "budget_scope": "total",
        "preferences": [], "missing_information": [], "ambiguous_information": [],
    }
    payload[field] = value
    with pytest.raises(AIProviderError) as error:
        validate_structured_output(json.dumps(payload), EnquiryExtraction)
    assert error.value.code == AIErrorCode.VALIDATION_FAILED


def test_no_provider_logs_or_sensitive_error_text(caplog):
    secret_text = "test-gemini-secret"
    caplog.set_level(logging.DEBUG)
    with pytest.raises(AIProviderError) as error:
        GeminiProvider(settings(), transport=transport_with(401, {"error": {"message": secret_text}})).generate(
            GenerationRequest("synthetic prompt containing private data")
        )
    assert secret_text not in str(error.value)
    assert secret_text not in caplog.text
    assert "synthetic prompt" not in caplog.text
    with pytest.raises(AIProviderError):
        GroqProvider(settings(), transport=transport_with(401, {"error": {"message": "private response"}})).generate(
            GenerationRequest("another synthetic prompt")
        )
    assert "test-groq-secret" not in caplog.text
    assert "Bearer" not in caplog.text
    assert "private response" not in caplog.text
