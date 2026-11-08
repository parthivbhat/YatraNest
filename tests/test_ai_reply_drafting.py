"""Isolated PostgreSQL tests for AI-assisted reply draft creation."""

from datetime import UTC, date, datetime
from decimal import Decimal
import json
import re
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.main import app
from app.models import AISuggestion, Enquiry, EnquiryReply, Package, PackagePrice, User
from app.schemas.ai_extraction import EnquiryExtraction, extraction_to_json
from app.services import ai_reply_drafting as drafting
from app.services.ai.base import GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai_reply_drafting import (
    AIReplyDraftError,
    AIDraftSource,
    TrustedReplyNumbers,
    _build_prompt,
    _parse_provider_content,
    _trusted_reply_numbers,
    generate_reply_draft_content,
)
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def _settings() -> Settings:
    return Settings(ai_enabled=True, ai_provider="groq", groq_api_key="test-key-never-logged", groq_model="test-model")


def _extraction() -> EnquiryExtraction:
    return EnquiryExtraction.model_validate({
        "schema_version": "1.0",
        "destination": "Alleppey",
        "travel_start_date": "2027-10-30",
        "travel_end_date": "2027-11-04",
        "travel_dates_flexible": False,
        "original_travel_period_text": "30 October to 4 November 2027",
        "adults": 2,
        "children": 1,
        "infants": 0,
        "budget_amount": "120000.00",
        "budget_currency": "INR",
        "budget_scope": "total",
        "preferences": ["Houseboat"],
        "missing_information": [],
        "ambiguous_information": [],
    }, strict=True)


def _records(db: Session, *, role: str = "admin", assigned: bool = True):
    staff = User(name=f"{role} tester", email=f"{uuid4()}@example.test", role=role)
    db.add(staff)
    db.flush()
    package = Package(
        package_code=f"AID-{uuid4().hex[:8]}", slug=f"ai-draft-{uuid4().hex[:8]}",
        title="Alleppey Houseboat Stay", destinations=["Alleppey"], duration_days=6,
        status="published",
        prices=[PackagePrice(
            season_name="Peak season", start_month=10, end_month=3,
            price_per_person=Decimal("32500.00"), currency="INR", price_basis="per_person_twin_sharing",
            valid_from=date(2027, 10, 1), valid_until=date(2028, 3, 31),
        )],
    )
    enquiry = Enquiry(
        package=package, assigned_to=staff if assigned else None,
        customer_name="Synthetic Traveller", customer_email="private-synthetic@example.invalid",
        customer_phone="+91 98765 43210", original_message=(
            "Synthetic QA enquiry for Alleppey, 2 adults and 1 child, Oct 30 to Nov 4 2027. "
            "Email private-synthetic@example.invalid; phone +91 98765 43210."
        ), status="new", adults=2, children=1, infants=0,
    )
    db.add(enquiry)
    db.flush()
    extraction = _extraction()
    suggestion = AISuggestion(
        enquiry_id=enquiry.id,
        generation_status="completed",
        extracted_data=extraction_to_json(extraction),
        accepted_extraction=extraction_to_json(extraction),
        extraction_review_status="accepted",
        extraction_reviewed_by_id=staff.id,
        extraction_reviewed_at=enquiry.created_at,
        provider="groq", model_name="test-model",
        generated_at=enquiry.created_at,
    )
    db.add(suggestion)
    db.flush()
    return staff, package, enquiry, suggestion


def test_generated_body_uses_accepted_extraction_and_database_owned_price(db_session):
    staff, package, enquiry, suggestion = _records(db_session)
    captured = {}

    def mock_generate(request, *, settings):
        captured["prompt"] = request.prompt
        assert request.output_model is drafting.AIReplyContent
        return GenerationResponse("groq", "test-model", '{"body":"Thank you for sharing your travel plans. We would be happy to review suitable options with you."}')

    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
    )
    assert result.source.extraction_source == "reviewed"
    assert result.source.suggestion_id == suggestion.id
    assert "₹32,500.00" in result.body
    assert "per person" in result.body and "GST treatment requires staff confirmation" in result.body
    assert "not a confirmed quote or booking" in result.body
    assert "child/infant fares" in result.body
    assert "₹32,500" not in captured["prompt"]
    assert "32,500.00" not in captured["prompt"]
    assert "private-synthetic@example.invalid" not in captured["prompt"]
    assert "+91 98765 43210" not in captured["prompt"]
    assert suggestion.accepted_extraction == extraction_to_json(_extraction())
    assert enquiry.original_message.startswith("Synthetic QA enquiry")


def test_provisional_extraction_is_labelled_and_missing_extraction_is_supported(db_session):
    staff, _, enquiry, suggestion = _records(db_session)
    suggestion.accepted_extraction = None
    suggestion.extraction_review_status = "pending"
    suggestion.extraction_reviewed_by_id = None
    suggestion.extraction_reviewed_at = None
    db_session.flush()
    captured = {}

    def mock_generate(request, *, settings):
        captured["prompt"] = request.prompt
        return GenerationResponse("groq", "test-model", '{"body":"Thank you for getting in touch. Could you share any other preferences?"}')

    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
    )
    assert result.source.extraction_source == "provisional"
    assert "provisional" in result.body.casefold()
    assert "unaccepted provisional extraction" in captured["prompt"].casefold()

    suggestion.generation_status = "failed"
    db_session.flush()
    missing = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
    )
    assert missing.source.extraction is None
    assert missing.source.extraction_source == "none"
    assert "No validated extraction is available" in captured["prompt"]


def test_malformed_accepted_extraction_is_not_silently_replaced_by_raw(db_session):
    staff, _, enquiry, suggestion = _records(db_session)
    suggestion.accepted_extraction = {"unexpected": "payload"}
    db_session.flush()
    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
        generator=lambda request, *, settings: GenerationResponse(
            "groq", "test-model", '{"body":"Thank you for getting in touch."}'
        ),
    )
    assert result.source.extraction is None
    assert result.source.extraction_source == "none"


@pytest.mark.parametrize("content", [
    "not json",
    '{"body":"The price is ₹32,500 per person."}',
    '{"body":"Your package is available and guaranteed."}',
    '{"body":"We can provide a free airport transfer."}',
    '{"body":"Hello", "price":"₹32,500"}',
])
def test_invalid_or_unsafe_provider_content_is_rejected(db_session, content):
    staff, _, enquiry, _ = _records(db_session)

    def mock_generate(request, *, settings):
        return GenerationResponse("groq", "test-model", content)

    with pytest.raises(AIReplyDraftError):
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=staff,
            expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
        )


def test_provider_failure_is_safe_and_does_not_change_enquiry_or_replies(db_session):
    staff, _, enquiry, suggestion = _records(db_session)
    original_message = enquiry.original_message

    def mock_generate(request, *, settings):
        raise AIProviderError(AIErrorCode.PROVIDER_TIMEOUT)

    with pytest.raises(AIReplyDraftError, match="timed out"):
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=staff,
            expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
        )
    assert enquiry.original_message == original_message
    assert db_session.scalar(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id)) is None
    assert suggestion.accepted_extraction == extraction_to_json(_extraction())


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        (AIErrorCode.PROVIDER_AUTHENTICATION_FAILED, 502),
        (AIErrorCode.PROVIDER_RATE_LIMITED, 503),
        (AIErrorCode.PROVIDER_UNAVAILABLE, 503),
    ],
)
def test_provider_failure_categories_are_safe_and_persist_no_reply(db_session, code, expected_status):
    staff, _, enquiry, _ = _records(db_session)

    def mock_generate(request, *, settings):
        raise AIProviderError(code)

    with pytest.raises(AIReplyDraftError) as failure:
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=staff,
            expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(), generator=mock_generate,
        )
    assert failure.value.status_code == expected_status
    assert db_session.scalar(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id)) is None


def test_ai_disabled_or_wrong_provider_fails_before_generation(db_session):
    staff, _, enquiry, _ = _records(db_session)
    for settings in (
        Settings(ai_enabled=False, ai_provider="groq", groq_api_key="x", groq_model="m"),
        Settings(ai_enabled=True, ai_provider="gemini", groq_api_key="x", groq_model="m"),
    ):
        with pytest.raises(AIReplyDraftError, match="unavailable"):
            generate_reply_draft_content(
                db_session, enquiry_id=enquiry.id, actor=staff,
                expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=settings,
                generator=lambda *args, **kwargs: pytest.fail("provider should not be called"),
            )


@pytest.mark.parametrize("role", ["admin", "editor", "consultant"])
def test_service_allows_permitted_roles_with_consultant_assignment(db_session, role):
    staff, _, enquiry, _ = _records(db_session, role=role, assigned=True)
    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
        generator=lambda request, *, settings: GenerationResponse(
            "groq", "test-model", '{"body":"Thank you for sharing your travel plans."}'
        ),
    )
    assert result.body.startswith("Thank you")


def test_ordinary_feel_free_phrase_is_not_misclassified_as_a_price_offer(db_session):
    staff, _, enquiry, _ = _records(db_session)
    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
        generator=lambda request, *, settings: GenerationResponse(
            "groq", "test-model", '{"body":"Feel free to share any other travel preferences."}'
        ),
    )
    assert result.body.startswith("Feel free to share")


def test_reply_validation_logs_safe_stage_without_generated_text(caplog):
    private_marker = "SYNTHETIC-REPLY-CONTENT-MUST-NOT-BE-LOGGED"
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content('{"body":"' + private_marker + ' hotel preferences"}')
    assert '"stage":"reply_safety_validation"' in caplog.text
    assert '"failure_code":"invalid_provider_response"' in caplog.text
    assert '"rule":"restricted_claim_term"' in caplog.text
    assert '"rule_category":"unverified_feature"' in caplog.text
    assert '"matched_term":"hotel"' in caplog.text
    assert private_marker not in caplog.text


def test_restricted_term_diagnostic_is_allowlisted_and_never_logs_content(caplog):
    private_marker = "CUSTOMER-DETAILS-AND-REPLY-MUST-NEVER-APPEAR"
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content('{"body":"' + private_marker + ' availability is not confirmed"}')
    assert '"matched_term":"availability"' in caplog.text
    assert '"rule_category":"commercial_commitment"' in caplog.text
    assert private_marker not in caplog.text
    assert 'availability is not confirmed' not in caplog.text


def test_grounded_travel_dates_and_individual_traveller_counts_are_allowed():
    trusted = TrustedReplyNumbers(
        travel_dates=frozenset({date(2027, 10, 30), date(2027, 11, 4)}),
        adults=2, children=1, infants=0,
    )
    parsed = _parse_provider_content(
        '{"body":"We have your dates as 2027-10-30 to 2027-11-04, for 2 adults and 1 child."}',
        trusted_numbers=trusted,
    )
    assert "2027-10-30" in parsed.body and "2 adults" in parsed.body


def test_grounded_slash_date_format_is_allowed_without_guessing():
    trusted = TrustedReplyNumbers(
        travel_dates=frozenset({date(2027, 10, 30), date(2027, 11, 4)}),
    )
    parsed = _parse_provider_content(
        '{"body":"We have your dates as 30/10/2027 to 04/11/2027."}',
        trusted_numbers=trusted,
    )
    assert "30/10/2027" in parsed.body


def test_customer_budget_is_allowed_only_in_a_grounded_clarification_question():
    trusted = TrustedReplyNumbers(budget_amounts=frozenset({Decimal("70000.00")}), budget_currency="INR")
    parsed = _parse_provider_content(
        '{"body":"Could you clarify whether your ₹70,000 per person budget should be understood as per person or total, since the form lists ₹70,000 total?"}',
        trusted_numbers=trusted,
    )
    assert "clarify" in parsed.body.casefold()
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(
            '{"body":"Could you clarify whether ₹70,000 is the total package amount?"}',
            trusted_numbers=trusted,
        )


def test_demo_traveller_budget_without_currency_marker_must_still_be_grounded_and_clarified():
    enquiry = SimpleNamespace(
        travel_start_date=None,
        travel_end_date=None,
        adults=2,
        children=None,
        infants=None,
        budget_amount_inr=Decimal("70000.00"),
        original_message="My budget is ₹70,000 per person, but the form lists ₹70,000 total.",
    )
    trusted = _trusted_reply_numbers(enquiry, AIDraftSource(None, None, None, "none"))
    parsed = _parse_provider_content(
        '{"body":"Could you clarify whether your submitted budget of 70,000 is per person or total, since the form lists 70,000 total?"}',
        trusted_numbers=trusted,
    )
    assert "clarify" in parsed.body.casefold()
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(
            '{"body":"Your budget of 70,000 is suitable for the package."}',
            trusted_numbers=trusted,
        )


def test_numeric_allowlist_uses_trip_fields_and_customer_budget_not_contact_digits():
    enquiry = SimpleNamespace(
        travel_start_date=date(2027, 10, 30), travel_end_date=date(2027, 11, 4),
        adults=2, children=1, infants=0, budget_amount_inr=Decimal("70000.00"),
        original_message=(
            "My budget is ₹70,000 per person; dates 30 October to 4 November 2027. "
            "Contact qa@example.invalid at +91 98765 43210."
        ),
    )
    trusted = _trusted_reply_numbers(enquiry, AIDraftSource(None, None, None, "none"))
    assert trusted.travel_dates == frozenset({date(2027, 10, 30), date(2027, 11, 4)})
    assert trusted.budget_amounts == frozenset({Decimal("70000.00")})
    assert trusted.adults == 2 and trusted.children == 1 and trusted.infants == 0
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(
            '{"body":"Call +91 98765 43210 about your trip."}', trusted_numbers=trusted
        )


@pytest.mark.parametrize("body", [
    "The package costs ₹32,500.",
    "A 10% discount is available.",
    "We can provide a free airport transfer.",
    "GST would be ₹4,200.",
    "Our driver will arrive in 24 hours.",
])
def test_invented_prices_offers_taxes_and_unrelated_numbers_are_rejected(body):
    trusted = TrustedReplyNumbers(
        travel_dates=frozenset({date(2027, 10, 30), date(2027, 11, 4)}),
        adults=2, children=1, infants=0,
        budget_amounts=frozenset({Decimal("70000.00")}), budget_currency="INR",
    )
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(json.dumps({"body": body}), trusted_numbers=trusted)


def test_numeric_failure_diagnostics_explain_category_and_sources_without_values(caplog):
    caplog.set_level("WARNING")
    private_marker = "PRIVATE_NUMBER_987"
    enquiry = SimpleNamespace(
        travel_start_date=date(2027, 10, 30), travel_end_date=date(2027, 11, 4),
        adults=2, children=1, infants=0, budget_amount_inr=Decimal("70000.00"),
        original_message="My budget is ₹70,000 total.",
    )
    trusted = _trusted_reply_numbers(enquiry, AIDraftSource(None, None, None, "none"))
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(
            json.dumps({"body": f"We will plan for {private_marker} adults."}),
            trusted_numbers=trusted,
        )
    assert '"numeric_claim_category":"traveller_count"' in caplog.text
    assert '"trusted_value_found":false' in caplog.text
    assert '"numeric_validation_condition":"traveller_count_mismatch"' in caplog.text
    assert '"structured_data_match":false' in caplog.text
    assert '"accepted_extraction_match":false' in caplog.text
    assert private_marker not in caplog.text
    assert '"matched_term":"numeric_or_currency_marker"' in caplog.text


def test_budget_diagnostic_reports_trusted_origin_without_logging_amount(caplog):
    caplog.set_level("WARNING")
    enquiry = SimpleNamespace(
        travel_start_date=None, travel_end_date=None, adults=2, children=None, infants=None,
        budget_amount_inr=Decimal("70000.00"), original_message="My budget is ₹70,000 total.",
    )
    trusted = _trusted_reply_numbers(enquiry, AIDraftSource(None, None, None, "none"))
    with pytest.raises(AIReplyDraftError):
        _parse_provider_content(
            '{"body":"Your budget of ₹70,000 is suitable."}', trusted_numbers=trusted,
        )
    assert '"numeric_claim_category":"inr_amount"' in caplog.text
    assert '"trusted_value_found":true' in caplog.text
    assert '"numeric_validation_condition":"budget_clarification_required"' in caplog.text
    assert '"structured_data_match":true' in caplog.text
    assert '"accepted_extraction_match":false' in caplog.text
    assert '70000' not in caplog.text


def test_reply_prompt_grounding_and_budget_conflict_instructions():
    prompt = _build_prompt(
        "My budget is ₹70,000 per person.", None, "none", None,
        {"budget_amount_inr": "70000.00", "budget_scope_in_form": "total"},
    )
    assert "may repeat only travel dates and individual traveller counts" in prompt
    assert "ask which scope the traveller intended" in prompt
    assert "never as a package rate, quote or confirmed price" in prompt
    assert "Do not invent or quote package prices, totals, taxes/GST" in prompt


def test_service_rejects_unassigned_consultant_and_inactive_user(db_session):
    consultant, _, enquiry, _ = _records(db_session, role="consultant", assigned=False)
    with pytest.raises(AIReplyDraftError) as denied:
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=consultant,
            expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
            generator=lambda *args, **kwargs: pytest.fail("provider must not be called"),
        )
    assert denied.value.status_code == 404

    consultant.is_active = False
    db_session.flush()
    with pytest.raises(AIReplyDraftError) as inactive:
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=consultant,
            expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
            generator=lambda *args, **kwargs: pytest.fail("provider must not be called"),
        )
    assert inactive.value.status_code == 403


def test_stale_enquiry_and_ambiguous_price_are_handled_without_guessing(db_session):
    staff, _, enquiry, suggestion = _records(db_session)
    stale = (enquiry.updated_at.replace(year=enquiry.updated_at.year - 1)).isoformat()
    with pytest.raises(AIReplyDraftError) as conflict:
        generate_reply_draft_content(
            db_session, enquiry_id=enquiry.id, actor=staff,
            expected_enquiry_updated_at=stale, settings=_settings(),
            generator=lambda *args, **kwargs: pytest.fail("provider must not be called for stale data"),
        )
    assert conflict.value.status_code == 409

    no_dates = EnquiryExtraction.model_validate({
        "schema_version": "1.0", "destination": "Alleppey",
        "travel_start_date": None, "travel_end_date": None,
        "travel_dates_flexible": None, "original_travel_period_text": None,
        "adults": None, "children": None, "infants": None,
        "budget_amount": None, "budget_currency": None, "budget_scope": None,
        "preferences": [], "missing_information": ["travel_start_date", "travel_end_date", "adults", "children", "infants", "budget_amount"],
        "ambiguous_information": [],
    }, strict=True)
    suggestion.accepted_extraction = extraction_to_json(no_dates)
    db_session.flush()
    result = generate_reply_draft_content(
        db_session, enquiry_id=enquiry.id, actor=staff,
        expected_enquiry_updated_at=enquiry.updated_at.isoformat(), settings=_settings(),
        generator=lambda request, *, settings: GenerationResponse(
            "groq", "test-model", '{"body":"Thank you for getting in touch."}'
        ),
    )
    assert "Pricing requires staff confirmation" in result.body
    assert "₹32,500.00" not in result.body


@pytest.fixture
def ai_reply_client(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield db_session
    finally:
        app.dependency_overrides.pop(get_db, None)


def _client(db: Session, user: User | None) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    if user:
        session_record, token = issue_session(db, user_id=user.id)
        db.flush()
        client.cookies.set(SESSION_COOKIE_NAME, token)
        client._test_csrf_token = session_record.csrf_token
    return client


def _tokens(client: TestClient, enquiry: Enquiry) -> tuple[str, str, str]:
    response = client.get(f"/admin/enquiries/{enquiry.id}")
    assert response.status_code == 200
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    updated = re.search(r'name="expected_enquiry_updated_at" value="([^"]+)"', response.text)
    request_id = re.search(r'name="ai_draft_request_id" value="([^"]+)"', response.text)
    assert csrf and updated and request_id
    return csrf.group(1), updated.group(1), request_id.group(1)


def test_route_saves_ai_output_as_authored_editable_draft_with_provenance(ai_reply_client, monkeypatch, caplog):
    caplog.set_level("INFO")
    db = ai_reply_client
    staff, _, enquiry, suggestion = _records(db)
    approved = EnquiryReply(
        enquiry_id=enquiry.id, author_id=staff.id, body="Approved manual reply stays unchanged.",
        status="approved", approved_by_id=staff.id, approved_at=datetime.now(UTC),
    )
    db.add(approved)
    db.flush()
    client = _client(db, staff)
    csrf, updated, request_id = _tokens(client, enquiry)
    monkeypatch.setattr(drafting, "get_settings", _settings)
    monkeypatch.setattr(drafting, "generate", lambda request, *, settings: GenerationResponse(
        "groq", "test-model", '{"body":"Thank you for your enquiry. We will review your preferences."}'
    ))
    response = client.post(f"/admin/enquiries/{enquiry.id}/replies/ai-draft", data={
        "csrf_token": csrf, "expected_enquiry_updated_at": updated, "ai_draft_request_id": request_id,
    })
    assert response.status_code == 303 and "updated=ai_reply_draft" in response.headers["location"]
    replies = db.scalars(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id).order_by(EnquiryReply.created_at)).all()
    reply = next(item for item in replies if item.id != approved.id)
    db.refresh(approved)
    assert approved.status == "approved" and approved.body == "Approved manual reply stays unchanged."
    assert reply is not None
    assert (reply.author_id, reply.status, reply.source_ai_suggestion_id) == (staff.id, "draft", suggestion.id)
    assert "₹32,500.00" in reply.body
    assert reply.approved_at is None and reply.manually_sent_at is None
    assert "not a confirmed quote or booking" in reply.body
    detail = client.get(f"/admin/enquiries/{enquiry.id}")
    assert detail.headers["cache-control"] == "no-store"
    assert detail.headers["x-robots-tag"] == "noindex, nofollow"
    assert "AI-assisted draft" in detail.text and "staff-reviewed extraction" in detail.text
    assert '"event":"ai_reply_draft_persisted"' in caplog.text
    assert enquiry.customer_email not in caplog.text


def test_replayed_ai_draft_form_returns_existing_draft_without_second_provider_call(ai_reply_client, monkeypatch):
    db = ai_reply_client
    staff, _, enquiry, suggestion = _records(db)
    client = _client(db, staff)
    csrf, updated, request_id = _tokens(client, enquiry)
    calls = {"count": 0}
    monkeypatch.setattr(drafting, "get_settings", _settings)

    def mock_generate(request, *, settings):
        calls["count"] += 1
        return GenerationResponse("groq", "test-model", '{"body":"Thank you for your enquiry."}')

    monkeypatch.setattr(drafting, "generate", mock_generate)
    post_data = {
        "csrf_token": csrf,
        "expected_enquiry_updated_at": updated,
        "ai_draft_request_id": request_id,
    }
    path = f"/admin/enquiries/{enquiry.id}/replies/ai-draft"
    first = client.post(path, data=post_data)
    replay = client.post(path, data=post_data)
    drafts = db.scalars(select(EnquiryReply).where(
        EnquiryReply.enquiry_id == enquiry.id,
        EnquiryReply.source_ai_suggestion_id == suggestion.id,
    )).all()
    assert first.status_code == replay.status_code == 303
    assert calls["count"] == 1
    assert len(drafts) == 1
    assert drafts[0].status == "draft"
    assert drafts[0].approved_at is None and drafts[0].manually_sent_at is None


def test_ai_reply_route_requires_csrf_and_consultant_assignment_scope(ai_reply_client):
    db = ai_reply_client
    admin, _, enquiry, _ = _records(db)
    csrfless = _client(db, admin).post(f"/admin/enquiries/{enquiry.id}/replies/ai-draft", data={
        "expected_enquiry_updated_at": enquiry.updated_at.isoformat(),
    })
    assert csrfless.status_code == 403

    consultant, _, other_enquiry, _ = _records(db, role="consultant", assigned=False)
    consultant_client = _client(db, consultant)
    scoped = consultant_client.post(f"/admin/enquiries/{other_enquiry.id}/replies/ai-draft", data={
        "csrf_token": consultant_client._test_csrf_token,
        "expected_enquiry_updated_at": other_enquiry.updated_at.isoformat(),
        "ai_draft_request_id": str(uuid4()),
    })
    assert scoped.status_code == 404
