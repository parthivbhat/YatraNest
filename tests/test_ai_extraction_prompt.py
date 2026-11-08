"""Database-free contract tests for the AI extraction prompt and Groq boundary."""

import json

import httpx

from app.config import Settings
from app.schemas.ai_extraction import EnquiryExtraction
from app.services.ai.orchestrator import generate
from app.services.ai_extraction_lifecycle import _generation_request


SYNTHETIC_MUNNAR_MESSAGE = (
    "Hi, I'm planning a 6-day trip to Munnar, Thekkady and Alleppey in February 2027 "
    "for two adults. We prefer a comfortable 4-star accommodation and would like to "
    "explore tea plantations, nature and the Alleppey backwaters. Our budget is around "
    "₹70,000 per person. Please suggest a suitable package and share the details."
)

SYNTHETIC_MUNNAR_EXTRACTION = {
    "schema_version": "1.0",
    "destination": "Munnar, Thekkady and Alleppey",
    "travel_start_date": None,
    "travel_end_date": None,
    "travel_dates_flexible": None,
    "original_travel_period_text": "February 2027",
    "adults": 2,
    "children": None,
    "infants": None,
    "budget_amount": "70000.00",
    "budget_currency": "INR",
    "budget_scope": "per_person",
    "preferences": [
        "4-star accommodation",
        "tea plantations",
        "nature",
        "Alleppey backwaters",
    ],
    "missing_information": [
        "travel_start_date",
        "travel_end_date",
        "travel_dates_flexible",
        "children",
        "infants",
    ],
    "ambiguous_information": [],
}


def test_extraction_prompt_states_the_complete_schema_contract():
    prompt = _generation_request(SYNTHETIC_MUNNAR_MESSAGE).prompt

    for field in EnquiryExtraction.model_fields:
        assert f'"{field}"' in prompt
    for allowed_field in (
        "destination",
        "travel_start_date",
        "travel_end_date",
        "travel_year",
        "travel_dates_flexible",
        "adults",
        "children",
        "infants",
        "budget_amount",
        "budget_currency",
        "budget_scope",
    ):
        assert f'"{allowed_field}"' in prompt

    assert '"schema_version":"1.0"' in prompt
    assert '"YYYY-MM-DD"' in prompt
    assert '"total"|"per_person"' in prompt
    assert "decimal string" in prompt
    assert "never a JSON number" in prompt
    assert "Do not infer zero for an unmentioned" in prompt
    assert '"travel_year" in ambiguous_information' in prompt
    assert '"budget_amount":"70000.00"' in prompt
    assert '"children":null' in prompt
    assert '"infants":null' in prompt
    assert '"missing_information":["travel_start_date","travel_end_date",' in prompt
    assert SYNTHETIC_MUNNAR_MESSAGE in prompt


def test_mocked_groq_munnar_response_validates_against_extraction_schema():
    settings = Settings(
        _env_file=None,
        ai_enabled=True,
        ai_provider="groq",
        groq_api_key="synthetic-test-key",
        groq_model="openai/gpt-oss-120b",
        ai_max_retries=0,
        ai_fallback_enabled=False,
    )
    request = _generation_request(SYNTHETIC_MUNNAR_MESSAGE)
    observed = {}

    def handler(http_request: httpx.Request) -> httpx.Response:
        body = json.loads(http_request.content)
        observed["prompt"] = body["messages"][0]["content"]
        observed["response_format"] = body["response_format"]
        return httpx.Response(
            200,
            json={
                "id": "synthetic-request",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(SYNTHETIC_MUNNAR_EXTRACTION, ensure_ascii=False)
                        }
                    }
                ],
            },
        )

    result = generate(
        request,
        settings=settings,
        transport_factory=lambda provider: httpx.MockTransport(handler),
    )

    assert result.provider == "groq"
    assert result.model == "openai/gpt-oss-120b"
    assert isinstance(result.structured_data, EnquiryExtraction)
    assert result.structured_data.destination == "Munnar, Thekkady and Alleppey"
    assert result.structured_data.travel_start_date is None
    assert result.structured_data.travel_end_date is None
    assert result.structured_data.adults == 2
    assert result.structured_data.children is None
    assert result.structured_data.infants is None
    assert str(result.structured_data.budget_amount) == "70000.00"
    assert result.structured_data.budget_currency == "INR"
    assert result.structured_data.budget_scope == "per_person"
    assert observed["response_format"] == {"type": "json_object"}
    assert SYNTHETIC_MUNNAR_MESSAGE in observed["prompt"]
