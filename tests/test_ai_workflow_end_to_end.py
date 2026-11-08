"""End-to-end staff-assisted enquiry workflow using only synthetic/mock data."""

from datetime import UTC, datetime
import json
import re
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.main import app
from app.models import AIExtractionReviewEvent, AISuggestion, Enquiry, EnquiryReply, User
from app.routes import admin as admin_routes
from app.routes import public as public_routes
from app.schemas.ai_extraction import EnquiryExtraction, extraction_to_json
from app.services import ai_reply_drafting
from app.services.ai.base import GenerationResponse
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def _settings() -> Settings:
    return Settings(ai_enabled=True, ai_provider="groq", groq_api_key="mock-only", groq_model="mock-model")


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


@pytest.fixture
def workflow_client(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield db_session
    finally:
        app.dependency_overrides.pop(get_db, None)


def _staff_client(db: Session, staff: User) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    record, token = issue_session(db, user_id=staff.id)
    db.flush()
    client.cookies.set(SESSION_COOKIE_NAME, token)
    client._qa_csrf = record.csrf_token
    return client


def _field(text: str, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}" value="([^"]*)"', text)
    assert match, f"Missing hidden field {name}"
    return match.group(1)


def test_public_submission_through_manual_sent_record(workflow_client, supplied_packages, monkeypatch):
    db = workflow_client
    admin = User(name="Synthetic QA Admin", email=f"{uuid4()}@example.test", role="admin")
    db.add(admin)
    db.flush()

    # Exercise post-commit task scheduling and state visibility without an AI
    # provider or separate worker connection; extraction lifecycle internals
    # have their own concurrency/fencing tests.
    extracted = _extraction()
    scheduled: list[str] = []

    def ensure_pending(session, enquiry):
        from app.services.ai_extraction_lifecycle import ensure_initial_suggestion
        return ensure_initial_suggestion(session, enquiry, settings=_settings())

    def complete_mocked_background(suggestion_id):
        scheduled.append(str(suggestion_id))
        suggestion = db.get(AISuggestion, suggestion_id)
        assert suggestion is not None
        suggestion.generation_status = "completed"
        suggestion.provider = "groq"
        suggestion.model_name = "mock-model"
        suggestion.extracted_data = extraction_to_json(extracted)
        suggestion.generated_at = datetime.now(UTC)
        db.flush()

    monkeypatch.setattr(public_routes, "ensure_initial_suggestion", ensure_pending)
    monkeypatch.setattr(public_routes, "_run_extraction_background", complete_mocked_background)
    client = TestClient(app, follow_redirects=False)

    package = supplied_packages["kerala"]
    package_page = client.get(f"/packages/{package.slug}")
    assert package_page.status_code == 200
    nonce = _field(package_page.text, "form_nonce")
    original_message = "  Hello, Kerala houseboat is essential for 2 adults and one child in November 2027.  "
    confirmation = client.post(
        f"/packages/{package.slug}/enquiries",
        headers={"origin": "http://testserver"},
        data={
            "form_nonce": nonce,
            "website": "",
            "customer_name": "Synthetic QA Traveller",
            "customer_email": "synthetic-traveller@example.invalid",
            "customer_phone": "",
            "travel_start_date": "2027-10-30",
            "travel_end_date": "2027-11-04",
            "travel_dates_flexible": "",
            "adults": "2",
            "children": "1",
            "infants": "0",
            "budget_amount_inr": "120000.00",
            "original_message": original_message,
            "response_notice_accepted": "yes",
            "whatsapp_consent": "",
        },
    )
    assert confirmation.status_code == 200
    assert "We have received your enquiry" in confirmation.text
    assert "email was sent" not in confirmation.text.casefold()
    assert confirmation.headers["cache-control"] == "no-store"
    assert len(scheduled) == 1

    enquiry = db.scalar(select(Enquiry).where(Enquiry.customer_name == "Synthetic QA Traveller"))
    assert enquiry is not None
    assert enquiry.original_message == original_message
    assert enquiry.whatsapp_consent is False and enquiry.whatsapp_consent_at is None
    suggestion = db.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert suggestion is not None and suggestion.generation_status == "completed"
    assert suggestion.accepted_extraction is None

    staff_client = _staff_client(db, admin)
    workspace = staff_client.get(f"/admin/enquiries/{enquiry.id}")
    assert workspace.status_code == 200
    assert "Original message" in workspace.text and original_message.strip() in workspace.text
    assert "Provisional" in workspace.text or "provisional" in workspace.text
    csrf = staff_client._qa_csrf
    reviewed = staff_client.post(
        f"/admin/enquiries/{enquiry.id}/ai-extraction/review",
        data={
            "csrf_token": csrf,
            "destination": "Alleppey",
            "travel_start_date": "2027-10-30",
            "travel_end_date": "2027-11-04",
            "travel_dates_flexible": "false",
            "original_travel_period_text": "30 October to 4 November 2027",
            "adults": "2", "children": "1", "infants": "0",
            "budget_amount": "120000.00", "budget_currency": "INR", "budget_scope": "total",
            "preferences": "Houseboat",
        },
    )
    assert reviewed.status_code == 303
    db.refresh(suggestion)
    event = db.scalar(select(AIExtractionReviewEvent).where(AIExtractionReviewEvent.ai_suggestion_id == suggestion.id))
    assert event is not None and event.actor_id == admin.id and event.action_type == "accepted"
    assert suggestion.accepted_extraction == extraction_to_json(extracted)
    assert enquiry.original_message == original_message

    monkeypatch.setattr(admin_routes, "get_settings", _settings)
    monkeypatch.setattr(ai_reply_drafting, "get_settings", _settings)
    monkeypatch.setattr(ai_reply_drafting, "generate", lambda request, *, settings: GenerationResponse(
        "groq", "mock-model", '{"body":"Thank you for sharing your travel plans. We will review the suitable options."}'
    ))
    workspace = staff_client.get(f"/admin/enquiries/{enquiry.id}")
    assert "Based on staff-accepted extraction" in workspace.text
    assert "₹32,500" in workspace.text
    ai_draft = staff_client.post(
        f"/admin/enquiries/{enquiry.id}/replies/ai-draft",
        data={
            "csrf_token": csrf,
            "expected_enquiry_updated_at": _field(workspace.text, "expected_updated_at"),
            "ai_draft_request_id": _field(workspace.text, "ai_draft_request_id"),
        },
    )
    assert ai_draft.status_code == 303
    reply = db.scalar(select(EnquiryReply).where(EnquiryReply.enquiry_id == enquiry.id))
    assert reply is not None and reply.status == "draft"
    assert reply.author_id == admin.id and reply.source_ai_suggestion_id == suggestion.id
    assert "₹32,500.00" in reply.body and "GST treatment requires staff confirmation" in reply.body
    assert reply.approved_at is None and reply.manually_sent_at is None

    workspace = staff_client.get(f"/admin/enquiries/{enquiry.id}")
    edited = staff_client.post(
        f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/edit",
        data={
            "csrf_token": csrf,
            "expected_enquiry_updated_at": _field(workspace.text, "expected_updated_at"),
            "expected_reply_updated_at": reply.updated_at.isoformat(),
            "body": "Staff corrected draft; stored pricing and caveats retained.",
        },
    )
    assert edited.status_code == 303
    db.refresh(reply)
    assert reply.body.startswith("Staff corrected draft") and reply.status == "draft"

    workspace = staff_client.get(f"/admin/enquiries/{enquiry.id}")
    approved = staff_client.post(
        f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/approve",
        data={
            "csrf_token": csrf,
            "expected_enquiry_updated_at": _field(workspace.text, "expected_updated_at"),
            "expected_reply_updated_at": reply.updated_at.isoformat(),
        },
    )
    assert approved.status_code == 303
    db.refresh(reply)
    assert reply.status == "approved" and reply.approved_by_id == admin.id
    assert reply.body.startswith("Staff corrected draft")

    confirm = staff_client.get(f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent/confirm")
    assert confirm.status_code == 200
    assert "already sent this approved reply" in confirm.text
    assert "This application does not send replies" in confirm.text
    assert confirm.headers["cache-control"] == "no-store"
    db.refresh(reply)
    assert reply.status == "approved" and reply.manually_sent_at is None

    recorded = staff_client.post(
        f"/admin/enquiries/{enquiry.id}/replies/{reply.id}/manual-sent",
        data={
            "csrf_token": csrf,
            "expected_enquiry_updated_at": _field(confirm.text, "expected_enquiry_updated_at"),
            "expected_reply_updated_at": _field(confirm.text, "expected_reply_updated_at"),
            "confirm_already_sent": "yes",
        },
    )
    assert recorded.status_code == 303
    db.refresh(reply)
    assert reply.status == "manually_sent" and reply.manually_sent_by_id == admin.id
    assert reply.manually_sent_at is not None and reply.body.startswith("Staff corrected draft")
