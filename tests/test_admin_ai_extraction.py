from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import AIExtractionReviewEvent, AISuggestion, Enquiry, User
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def add_user(db: Session, role: str, *, active: bool = True) -> User:
    user = User(name=f"{role.title()} {uuid4().hex[:6]}", email=f"{uuid4()}@example.test", role=role, is_active=active)
    db.add(user)
    db.flush()
    return user


def add_enquiry(db: Session, assigned: User | None = None) -> Enquiry:
    enquiry = Enquiry(
        customer_name="Synthetic QA Traveller",
        customer_email="qa@example.invalid",
        original_message="Two adults and one child travelling to Kerala in December; budget INR 120000.",
        assigned_to=assigned,
        status="new",
        travel_start_date=date(2027, 12, 10),
        travel_end_date=date(2027, 12, 16),
        travel_dates_flexible=False,
        adults=2,
        children=1,
        infants=0,
        budget_amount_inr=Decimal("120000.00"),
        response_notice_version="m6-v1",
        response_notice_accepted_at=datetime.now(UTC),
    )
    db.add(enquiry)
    db.flush()
    return enquiry


def extraction(*, destination: str = "Kerala") -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "destination": destination,
        "travel_start_date": "2027-12-10",
        "travel_end_date": "2027-12-16",
        "travel_dates_flexible": False,
        "original_travel_period_text": "10–16 December 2027",
        "adults": 2,
        "children": 1,
        "infants": 0,
        "budget_amount": "120000.00",
        "budget_currency": "INR",
        "budget_scope": "total",
        "preferences": ["Houseboat"],
        "missing_information": [],
        "ambiguous_information": [],
    }


@pytest.fixture
def client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def create(user: User | None):
        client = TestClient(app, follow_redirects=False)
        csrf = None
        if user is not None:
            record, token = issue_session(db_session, user_id=user.id)
            csrf = record.csrf_token
            db_session.flush()
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client, csrf

    try:
        yield create
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_workspace_shows_raw_accepted_extraction_audit_and_private_headers(db_session, client_factory):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session, admin)
    suggestion = AISuggestion(
        enquiry=enquiry,
        generation_status="completed",
        provider="groq",
        model_name="synthetic-model",
        generated_at=datetime.now(UTC),
        extracted_data=extraction(),
        accepted_extraction=extraction(destination="Alleppey"),
        extraction_review_status="corrected",
        extraction_reviewed_by=admin,
        extraction_reviewed_at=datetime.now(UTC),
    )
    db_session.add(suggestion)
    db_session.flush()
    event = AIExtractionReviewEvent(
        suggestion=suggestion,
        actor=admin,
        previous_extraction=extraction(),
        accepted_extraction=extraction(destination="Alleppey"),
        action_type="corrected",
    )
    db_session.add(event)
    db_session.flush()
    client, _ = client_factory(admin)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    assert response.status_code == 200
    assert "Original message" in response.text and "Two adults and one child" in response.text
    assert "Raw AI extraction" in response.text and "Kerala" in response.text
    assert "Staff-accepted extraction" in response.text and "Alleppey" in response.text
    assert "INR" in response.text and "120000.00" in response.text
    assert "Extraction corrected" in response.text
    assert "no-store" == response.headers["cache-control"]
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_workspace_does_not_treat_inconsistent_accepted_payload_as_reviewed(db_session, client_factory):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session)
    suggestion = AISuggestion(
        enquiry=enquiry,
        generation_status="completed",
        extracted_data=extraction(destination="RawOnlyland"),
        accepted_extraction={"unexpected": "ForgedReviewland"},
        extraction_review_status="accepted",
        extraction_reviewed_by=admin,
        extraction_reviewed_at=datetime.now(UTC),
    )
    db_session.add(suggestion)
    db_session.flush()
    client, _ = client_factory(admin)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    assert response.status_code == 200
    assert "inconsistent" in response.text.casefold()
    assert "Staff-accepted extraction" in response.text
    assert "No valid structured extraction is available to display" in response.text
    assert "ForgedReviewland" not in response.text


def test_authorized_staff_can_accept_correction_without_overwriting_raw_or_original(db_session, client_factory):
    editor = add_user(db_session, "editor")
    enquiry = add_enquiry(db_session)
    original = enquiry.original_message
    raw = extraction()
    suggestion = AISuggestion(enquiry=enquiry, generation_status="completed", extracted_data=raw)
    db_session.add(suggestion)
    db_session.flush()
    client, csrf = client_factory(editor)
    try:
        response = client.post(
            f"/admin/enquiries/{enquiry.id}/ai-extraction/review",
            data={
                "csrf_token": csrf,
                "destination": "Alleppey",
                "travel_start_date": "2027-12-10",
                "travel_end_date": "2027-12-16",
                "travel_dates_flexible": "false",
                "original_travel_period_text": "10–16 December 2027",
                "adults": "2", "children": "1", "infants": "0",
                "budget_amount": "120000.00", "budget_currency": "INR", "budget_scope": "total",
                "preferences": "Houseboat",
            },
        )
    finally:
        client.close()
    db_session.refresh(suggestion)
    db_session.refresh(enquiry)
    events = list(db_session.scalars(select(AIExtractionReviewEvent).where(AIExtractionReviewEvent.ai_suggestion_id == suggestion.id)))
    assert response.status_code == 303
    assert suggestion.extracted_data == raw
    assert suggestion.accepted_extraction["destination"] == "Alleppey"
    assert suggestion.extraction_reviewed_by_id == editor.id
    assert len(events) == 1 and events[0].actor_id == editor.id and events[0].action_type == "accepted"
    assert enquiry.original_message == original


def test_invalid_correction_is_rejected_without_audit_event(db_session, client_factory):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session)
    suggestion = AISuggestion(enquiry=enquiry, generation_status="completed", extracted_data=extraction())
    db_session.add(suggestion)
    db_session.flush()
    client, csrf = client_factory(admin)
    try:
        response = client.post(
            f"/admin/enquiries/{enquiry.id}/ai-extraction/review",
            data={
                "csrf_token": csrf, "destination": "Kerala", "travel_start_date": "2027-12-16",
                "travel_end_date": "2027-12-10", "travel_dates_flexible": "false",
                "adults": "2", "children": "1", "infants": "0", "budget_amount": "-1",
                "budget_currency": "INR", "budget_scope": "total",
                "missing_information": [], "ambiguous_information": [],
            },
        )
    finally:
        client.close()
    db_session.refresh(suggestion)
    assert response.status_code == 422
    assert suggestion.accepted_extraction is None
    assert db_session.scalar(select(AIExtractionReviewEvent.id).where(AIExtractionReviewEvent.ai_suggestion_id == suggestion.id)) is None


def test_consultant_scope_and_inactive_user_are_enforced(db_session, client_factory):
    consultant = add_user(db_session, "consultant")
    other = add_user(db_session, "consultant")
    own = add_enquiry(db_session, consultant)
    other_enquiry = add_enquiry(db_session, other)
    suggestion = AISuggestion(enquiry=own, generation_status="failed", failure_code="generation_failed")
    other_suggestion = AISuggestion(enquiry=other_enquiry, generation_status="failed", failure_code="generation_failed")
    db_session.add_all([suggestion, other_suggestion])
    db_session.flush()
    client, csrf = client_factory(consultant)
    try:
        own_detail = client.get(f"/admin/enquiries/{own.id}")
        denied = client.get(f"/admin/enquiries/{other_enquiry.id}")
        retry_denied = client.post(f"/admin/enquiries/{other_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    inactive = add_user(db_session, "editor", active=False)
    inactive_client, _ = client_factory(inactive)
    try:
        inactive_response = inactive_client.get(f"/admin/enquiries/{own.id}")
    finally:
        inactive_client.close()
    assert own_detail.status_code == 200
    assert denied.status_code == retry_denied.status_code == 404
    assert inactive_response.status_code == 303


def test_retry_is_csrf_protected_and_only_schedules_eligible_rows(db_session, client_factory, monkeypatch):
    from app.routes import admin as admin_routes

    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session)
    suggestion = AISuggestion(enquiry=enquiry, generation_status="failed", failure_code="generation_failed")
    db_session.add(suggestion)
    db_session.flush()
    scheduled = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, fn, *args, **kwargs: scheduled.append((fn, args)))
    client, csrf = client_factory(admin)
    try:
        no_csrf = client.post(f"/admin/enquiries/{enquiry.id}/ai-extraction/retry", data={})
        response = client.post(f"/admin/enquiries/{enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    db_session.refresh(suggestion)
    assert no_csrf.status_code == 403
    assert response.status_code == 303
    assert suggestion.generation_status == "pending"
    assert len(scheduled) == 1 and scheduled[0][1] == (suggestion.id,)
    assert scheduled[0][0] is admin_routes._run_extraction_task_safely


def test_retry_rejects_active_and_completed_and_allows_stale_processing(db_session, client_factory, monkeypatch):
    from app.config import Settings

    admin = add_user(db_session, "admin")
    active_enquiry, done_enquiry, stale_enquiry = add_enquiry(db_session), add_enquiry(db_session), add_enquiry(db_session)
    now = datetime.now(UTC)
    active = AISuggestion(enquiry=active_enquiry, generation_status="processing", generation_attempt_id=uuid4(), updated_at=now)
    done = AISuggestion(enquiry=done_enquiry, generation_status="completed", extracted_data=extraction())
    stale = AISuggestion(
        enquiry=stale_enquiry, generation_status="processing", generation_attempt_id=uuid4(),
        updated_at=now - timedelta(hours=1),
    )
    db_session.add_all([active, done, stale])
    db_session.flush()
    scheduled = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, fn, *args, **kwargs: scheduled.append(args))
    client, csrf = client_factory(admin)
    try:
        active_response = client.post(f"/admin/enquiries/{active_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
        done_response = client.post(f"/admin/enquiries/{done_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
        stale_response = client.post(f"/admin/enquiries/{stale_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    assert active_response.status_code == done_response.status_code == 409
    assert stale_response.status_code == 303
    assert stale.generation_status == "pending" and stale.generation_attempt_id is None
    assert scheduled == [(stale.id,)]

@pytest.mark.parametrize("state", ["pending", "processing", "completed", "failed", "unavailable"])
def test_workspace_displays_every_generation_state_without_exposing_raw_failures(db_session, client_factory, state):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session, admin)
    suggestion = AISuggestion(
        enquiry=enquiry,
        generation_status=state,
        generation_attempt_id=uuid4() if state == "processing" else None,
        failure_code="provider_timeout" if state == "failed" else None,
        extracted_data=extraction() if state == "completed" else {},
    )
    db_session.add(suggestion)
    db_session.flush()
    client, _ = client_factory(admin)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    assert response.status_code == 200
    assert f">{state.title()}<" in response.text
    if state == "failed":
        assert "did not respond in time" in response.text
        assert "exception" not in response.text.lower()


def test_workspace_displays_missing_and_ambiguous_values(db_session, client_factory):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session, admin)
    data = extraction()
    data.update({
        "travel_start_date": None,
        "travel_end_date": None,
        "original_travel_period_text": "December",
        "budget_currency": None,
        "budget_scope": None,
        "missing_information": ["travel_start_date", "travel_end_date", "budget_currency"],
        "ambiguous_information": ["travel_year", "budget_scope"],
    })
    suggestion = AISuggestion(enquiry=enquiry, generation_status="completed", extracted_data=data)
    db_session.add(suggestion)
    db_session.flush()
    client, _ = client_factory(admin)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    assert response.status_code == 200
    assert "Missing information" in response.text and "travel start date" in response.text
    assert "Ambiguous information" in response.text and "travel year" in response.text
    assert "December" in response.text


def test_retry_scheduling_failure_is_safe_and_worker_exception_is_contained(db_session, client_factory, monkeypatch):
    from app.routes import admin as admin_routes

    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session)
    suggestion = AISuggestion(enquiry=enquiry, generation_status="unavailable", failure_code="provider_unavailable")
    db_session.add(suggestion)
    db_session.flush()
    def fail_schedule(self, fn, *args, **kwargs):
        raise RuntimeError("synthetic internal exception must not be displayed")
    monkeypatch.setattr(BackgroundTasks, "add_task", fail_schedule)
    client, csrf = client_factory(admin)
    try:
        response = client.post(f"/admin/enquiries/{enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    db_session.refresh(suggestion)
    assert response.status_code == 303
    assert suggestion.generation_status == "failed"
    assert suggestion.failure_code == "generation_failed"
    assert "synthetic internal exception" not in response.text

    monkeypatch.setattr(admin_routes, "process_extraction", lambda _id: (_ for _ in ()).throw(RuntimeError("private")))
    assert admin_routes._run_extraction_task_safely(suggestion.id) is None

def test_pending_retry_is_only_available_after_recovery_threshold(db_session, client_factory, monkeypatch):
    admin = add_user(db_session, "admin")
    old_enquiry, new_enquiry = add_enquiry(db_session), add_enquiry(db_session)
    now = datetime.now(UTC)
    stale = AISuggestion(enquiry=old_enquiry, generation_status="pending", updated_at=now - timedelta(hours=1))
    fresh = AISuggestion(enquiry=new_enquiry, generation_status="pending", updated_at=now)
    db_session.add_all([stale, fresh])
    db_session.flush()
    scheduled = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda self, fn, *args, **kwargs: scheduled.append(args))
    client, csrf = client_factory(admin)
    try:
        fresh_response = client.post(f"/admin/enquiries/{new_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
        stale_response = client.post(f"/admin/enquiries/{old_enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    assert fresh_response.status_code == 409
    assert stale_response.status_code == 303
    assert scheduled == [(stale.id,)]


def test_retry_schedule_failure_does_not_overwrite_newer_completed_work(db_session, client_factory, monkeypatch):
    admin = add_user(db_session, "admin")
    enquiry = add_enquiry(db_session)
    suggestion = AISuggestion(enquiry=enquiry, generation_status="failed", failure_code="generation_failed")
    db_session.add(suggestion)
    db_session.flush()
    def simulated_race(self, fn, *args, **kwargs):
        suggestion.generation_status = "completed"
        suggestion.generation_attempt_id = None
        suggestion.provider = "groq"
        db_session.flush()
        raise RuntimeError("synthetic schedule failure")
    monkeypatch.setattr(BackgroundTasks, "add_task", simulated_race)
    client, csrf = client_factory(admin)
    try:
        response = client.post(f"/admin/enquiries/{enquiry.id}/ai-extraction/retry", data={"csrf_token": csrf})
    finally:
        client.close()
    db_session.refresh(suggestion)
    assert response.status_code == 303
    assert suggestion.generation_status == "completed"
    assert suggestion.failure_code is None

def test_review_route_enforces_csrf_and_consultant_record_scope(db_session, client_factory):
    assigned, other = add_user(db_session, "consultant"), add_user(db_session, "consultant")
    own, other_enquiry = add_enquiry(db_session, assigned), add_enquiry(db_session, other)
    own_suggestion = AISuggestion(enquiry=own, generation_status="completed", extracted_data=extraction())
    other_suggestion = AISuggestion(enquiry=other_enquiry, generation_status="completed", extracted_data=extraction())
    db_session.add_all([own_suggestion, other_suggestion])
    db_session.flush()
    client, csrf = client_factory(assigned)
    try:
        missing_csrf = client.post(f"/admin/enquiries/{own.id}/ai-extraction/review", data={})
        denied = client.post(
            f"/admin/enquiries/{other_enquiry.id}/ai-extraction/review",
            data={"csrf_token": csrf, "destination": "Kerala"},
        )
    finally:
        client.close()
    assert missing_csrf.status_code == 403
    assert denied.status_code == 404
    assert own_suggestion.accepted_extraction is None
    assert other_suggestion.accepted_extraction is None
