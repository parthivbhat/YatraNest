import re
from datetime import date
from decimal import Decimal
from uuid import uuid4
import hashlib

import pytest
from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import Settings
from app.database import get_db
from app.main import app
from app.models import AISuggestion, Enquiry, Package, PublicFormNonce
from app.routes import public as public_routes
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai_extraction_lifecycle import ensure_initial_suggestion


_ORIGINAL_ADD_TASK = BackgroundTasks.add_task


def _override_db(db_session: Session):
    def dependency():
        yield db_session

    return dependency


@pytest.fixture
def public_client(db_session: Session, monkeypatch):
    app.dependency_overrides[get_db] = _override_db(db_session)
    # Public route tests never execute real provider work.
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda *args, **kwargs: None)
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_db, None)


def _nonce(response) -> str:
    match = re.search(r'name="form_nonce" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _valid_form(token: str) -> dict[str, str]:
    return {
        "form_nonce": token,
        "customer_name": "  A Traveller  ",
        "customer_email": "traveller@example.test",
        "customer_phone": "+91 98765 43210",
        "travel_start_date": "2027-12-10",
        "travel_end_date": "2027-12-17",
        "travel_dates_flexible": "yes",
        "adults": "2",
        "children": "1",
        "infants": "0",
        "budget_amount_inr": "120000.50",
        "original_message": "  Kerala, 6 nights. Houseboat is important.\nPlease call after 5.  ",
        "response_notice_accepted": "yes",
    }


def _get_form(client: TestClient, slug: str) -> tuple[object, dict[str, str]]:
    response = client.get(f"/packages/{slug}")
    return response, _valid_form(_nonce(response))


def _post(client: TestClient, slug: str, form: dict[str, str]):
    return client.post(
        f"/packages/{slug}/enquiries",
        data=form,
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
    )


def _package(code: str, status: str) -> Package:
    suffix = uuid4().hex[:8]
    return Package(
        package_code=f"{code}-{suffix}",
        slug=f"{code.lower()}-{suffix}",
        title=f"{code} package",
        status=status,
    )


def test_public_package_page_renders_enquiry_form(public_client: TestClient, supplied_packages) -> None:
    response = public_client.get("/packages/munnar-thekkady-alleppey-houseboat")

    assert response.status_code == 200
    assert 'action="/packages/munnar-thekkady-alleppey-houseboat/enquiries"' in response.text
    assert 'name="response_notice_accepted"' in response.text
    assert 'name="whatsapp_consent"' in response.text
    assert 'name="customer_name"' in response.text
    assert 'name="original_message"' in response.text
    assert "WhatsApp consent is optional" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_successful_submission_persists_structured_values_and_original_message(
    public_client: TestClient, db_session: Session, supplied_packages
) -> None:
    page, form = _get_form(public_client, supplied_packages["kerala"].slug)
    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    assert page.status_code == 200
    assert response.status_code == 200
    assert enquiry is not None
    assert enquiry.customer_name == "A Traveller"
    assert enquiry.customer_email == "traveller@example.test"
    assert enquiry.customer_phone == "+91 98765 43210"
    assert enquiry.travel_start_date == date(2027, 12, 10)
    assert enquiry.travel_end_date == date(2027, 12, 17)
    assert enquiry.travel_dates_flexible is True
    assert (enquiry.adults, enquiry.children, enquiry.infants) == (2, 1, 0)
    assert enquiry.budget_amount_inr == Decimal("120000.50")
    assert enquiry.original_message == "  Kerala, 6 nights. Houseboat is important.\nPlease call after 5.  "
    assert enquiry.response_notice_version == "m6-v1"
    assert enquiry.response_notice_accepted_at is not None
    assert enquiry.whatsapp_consent is False
    assert enquiry.whatsapp_consent_at is None
    assert enquiry.status == "new"
    assert enquiry.assigned_to_id is None
    assert "We have received your enquiry." in response.text
    assert "email was sent" not in response.text.lower()
    assert "whatsapp message was sent" not in response.text.lower()
    assert str(enquiry.id) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_explicit_whatsapp_consent_records_timestamp_and_exact_dates_are_not_flexible(
    public_client: TestClient, db_session: Session, supplied_packages
) -> None:
    slug = supplied_packages["kerala"].slug
    _, form = _get_form(public_client, slug)
    form.pop("travel_dates_flexible")
    form["whatsapp_consent"] = "yes"
    response = _post(public_client, slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    assert response.status_code == 200
    assert enquiry is not None
    assert enquiry.travel_dates_flexible is False
    assert enquiry.whatsapp_consent is True
    assert enquiry.whatsapp_consent_at is not None


@pytest.mark.parametrize(
    ("changes", "error_text"),
    [
        ({"customer_email": "", "customer_phone": ""}, "email address or phone number"),
        ({"travel_start_date": ""}, "start date"),
        ({"travel_end_date": "2027-12-01"}, "End date cannot be before"),
        ({"adults": "0"}, "from 1 to 999"),
        ({"children": "-1"}, "from 0 to 999"),
        ({"infants": "-1"}, "from 0 to 999"),
        ({"budget_amount_inr": "-1"}, "nonnegative INR amount"),
        ({"budget_amount_inr": "1.234"}, "up to two decimal places"),
        ({"budget_amount_inr": "10000000000"}, "too large"),
        ({"customer_email": "not-an-email"}, "valid email address"),
        ({"customer_phone": "call me"}, "valid phone number"),
        ({"response_notice_accepted": ""}, "acknowledge the enquiry-response notice"),
        ({"customer_name": " "}, "Enter your name"),
        ({"original_message": ""}, "Enter a message"),
    ],
)
def test_invalid_form_renders_field_error_without_creating_enquiry(
    public_client: TestClient,
    db_session: Session,
    supplied_packages,
    changes: dict[str, str],
    error_text: str,
) -> None:
    slug = supplied_packages["kerala"].slug
    _, form = _get_form(public_client, slug)
    form.update(changes)
    before = db_session.scalar(select(func.count()).select_from(Enquiry))

    response = _post(public_client, slug, form)

    after = db_session.scalar(select(func.count()).select_from(Enquiry))
    assert response.status_code == 422
    assert error_text in response.text
    assert after == before


@pytest.mark.parametrize("status", ["draft", "archived"])
def test_unpublished_package_enquiry_is_not_available(
    public_client: TestClient, db_session: Session, status: str
) -> None:
    package = _package("PRIVATE", status)
    db_session.add(package)
    db_session.flush()

    response = public_client.post(
        f"/packages/{package.slug}/enquiries",
        data={"form_nonce": "anything"},
        headers={"Origin": "http://testserver"},
    )
    assert response.status_code == 404
    assert db_session.scalar(select(func.count()).select_from(Enquiry)) == 0


def test_route_slug_binds_package_and_forged_ids_or_prices_are_ignored(
    public_client: TestClient, db_session: Session
) -> None:
    first = _package("FIRST", "published")
    second = _package("SECOND", "published")
    db_session.add_all([first, second])
    db_session.flush()
    _, form = _get_form(public_client, first.slug)
    form["package_id"] = str(second.id)
    form["package_price"] = "0.01"
    form["status"] = "closed"
    form["assigned_to_id"] = str(uuid4())

    response = _post(public_client, first.slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == first.id))
    assert response.status_code == 200
    assert enquiry is not None
    assert enquiry.package_id == first.id
    assert enquiry.status == "new"
    assert enquiry.assigned_to_id is None
    assert not hasattr(enquiry, "package_price")


@pytest.mark.parametrize(
    ("field", "value", "expected_status"),
    [
        ("website", "bot-filled", 400),
        ("form_nonce", "invalid-token", 403),
        ("form_nonce", "", 403),
    ],
)
def test_honeypot_or_invalid_nonce_is_rejected_without_persisting(
    public_client: TestClient,
    db_session: Session,
    supplied_packages,
    field: str,
    value: str,
    expected_status: int,
) -> None:
    slug = supplied_packages["kerala"].slug
    _, form = _get_form(public_client, slug)
    form[field] = value
    response = _post(public_client, slug, form)

    assert response.status_code == expected_status
    assert db_session.scalar(select(func.count()).select_from(Enquiry)) == 0


def test_cross_origin_submission_is_rejected(public_client: TestClient, supplied_packages) -> None:
    slug = supplied_packages["kerala"].slug
    _, form = _get_form(public_client, slug)
    response = public_client.post(
        f"/packages/{slug}/enquiries",
        data=form,
        headers={"Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_nonce_is_one_use(public_client: TestClient, db_session: Session, supplied_packages) -> None:
    slug = supplied_packages["kerala"].slug
    _, form = _get_form(public_client, slug)
    first = _post(public_client, slug, form)
    replay = _post(public_client, slug, form)

    assert first.status_code == 200
    assert replay.status_code == 403
    assert db_session.scalar(select(func.count()).select_from(Enquiry)) == 1


def _groq_settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "ai_enabled": True,
        "ai_provider": "groq",
        "groq_api_key": "synthetic-test-key",
        "groq_model": "openai/gpt-oss-120b",
        "ai_timeout_seconds": 5,
        "ai_max_retries": 0,
        "ai_fallback_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def _use_lifecycle_settings(monkeypatch, settings: Settings) -> None:
    real_ensure = ensure_initial_suggestion

    def ensure_with_settings(db, enquiry):
        return real_ensure(db, enquiry, settings=settings)

    monkeypatch.setattr(public_routes, "ensure_initial_suggestion", ensure_with_settings)


def _nonce_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_submission_commits_enquiry_nonce_and_one_pending_suggestion_before_scheduling(
    public_client, db_session, supplied_packages, monkeypatch
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())
    page, form = _get_form(public_client, supplied_packages["kerala"].slug)
    committed = {"value": False}
    original_commit = db_session.commit

    def tracked_commit():
        original_commit()
        committed["value"] = True

    monkeypatch.setattr(db_session, "commit", tracked_commit)
    tasks = []

    def capture_task(background_tasks, function, *args, **kwargs):
        assert committed["value"] is True
        tasks.append((function, args, kwargs))

    monkeypatch.setattr(BackgroundTasks, "add_task", capture_task)
    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    assert page.status_code == response.status_code == 200
    assert enquiry is not None
    suggestion = db_session.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert suggestion is not None
    assert suggestion.generation_status == "pending"
    assert suggestion.accepted_extraction is None
    assert db_session.scalar(select(func.count()).select_from(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id)) == 1
    assert db_session.scalar(select(func.count()).select_from(PublicFormNonce).where(PublicFormNonce.token_hash == _nonce_hash(form["form_nonce"]))) == 0
    assert len(tasks) == 1
    assert tasks[0][0] is public_routes._run_extraction_background
    assert tasks[0][1] == (suggestion.id,)
    assert tasks[0][2] == {}
    assert "  Kerala, 6 nights. Houseboat is important.\nPlease call after 5.  " == enquiry.original_message
    assert "We have received your enquiry." in response.text


@pytest.mark.parametrize(
    "settings",
    [_groq_settings(ai_enabled=False), _groq_settings(groq_api_key=None)],
)
def test_unavailable_ai_creates_unavailable_suggestion_without_scheduling(
    public_client, db_session, supplied_packages, monkeypatch, settings
):
    _use_lifecycle_settings(monkeypatch, settings)
    tasks = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda *args, **kwargs: tasks.append((args, kwargs)))
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    assert response.status_code == 200
    assert enquiry is not None
    suggestion = db_session.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert suggestion.generation_status == "unavailable"
    assert suggestion.failure_code == "provider_unavailable"
    assert tasks == []
    assert "We have received your enquiry." in response.text


def test_lifecycle_creation_failure_rolls_back_enquiry_and_nonce_consumption(
    public_client, db_session, supplied_packages, monkeypatch
):
    monkeypatch.setattr(
        public_routes,
        "ensure_initial_suggestion",
        lambda db, enquiry: (_ for _ in ()).throw(RuntimeError("synthetic failure")),
    )
    tasks = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda *args, **kwargs: tasks.append(args))
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    assert response.status_code == 503
    assert "could not be saved" in response.text
    assert db_session.scalar(select(func.count()).select_from(Enquiry)) == 0
    assert db_session.scalar(select(func.count()).select_from(AISuggestion)) == 0
    assert db_session.scalar(
        select(func.count()).select_from(PublicFormNonce).where(
            PublicFormNonce.token_hash == _nonce_hash(form["form_nonce"])
        )
    ) == 1
    assert tasks == []


def test_commit_failure_does_not_schedule_and_rolls_back_transaction(
    public_client, db_session, supplied_packages, monkeypatch
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)
    monkeypatch.setattr(db_session, "commit", lambda: (_ for _ in ()).throw(SQLAlchemyError("synthetic")))
    tasks = []
    monkeypatch.setattr(BackgroundTasks, "add_task", lambda *args, **kwargs: tasks.append(args))

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    assert response.status_code == 503
    assert db_session.scalar(select(func.count()).select_from(Enquiry)) == 0
    assert db_session.scalar(select(func.count()).select_from(AISuggestion)) == 0
    assert tasks == []


def test_scheduling_failure_keeps_confirmation_and_marks_only_pending_unclaimed_row_failed(
    public_client, db_session, supplied_packages, monkeypatch
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())
    monkeypatch.setattr(
        BackgroundTasks,
        "add_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("synthetic dispatch failure")),
    )
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    suggestion = db_session.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert response.status_code == 200
    assert "We have received your enquiry." in response.text
    assert suggestion.generation_status == "failed"
    assert suggestion.failure_code == "generation_failed"
    assert suggestion.generation_attempt_id is None


@pytest.mark.parametrize(
    ("new_status", "attempt_id"),
    [("processing", uuid4()), ("completed", None)],
)
def test_scheduling_failure_does_not_overwrite_newer_lifecycle_state(
    public_client, db_session, supplied_packages, monkeypatch, new_status, attempt_id
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())

    def mutate_then_fail(background_tasks, function, *args, **kwargs):
        db_session.execute(
            update(AISuggestion)
            .where(AISuggestion.id == args[0])
            .values(generation_status=new_status, generation_attempt_id=attempt_id)
        )
        raise RuntimeError("synthetic dispatch failure")

    monkeypatch.setattr(BackgroundTasks, "add_task", mutate_then_fail)
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    suggestion = db_session.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert response.status_code == 200
    assert suggestion.generation_status == new_status
    assert suggestion.generation_attempt_id == attempt_id
    assert suggestion.failure_code is None


def test_failure_to_record_scheduling_problem_does_not_suppress_confirmation(
    public_client, db_session, supplied_packages, monkeypatch
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())
    monkeypatch.setattr(
        BackgroundTasks,
        "add_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("dispatch failed")),
    )
    monkeypatch.setattr(
        public_routes,
        "_mark_unscheduled_extraction_failed",
        lambda *args: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    enquiry = db_session.scalar(select(Enquiry).where(Enquiry.package_id == supplied_packages["kerala"].id))
    suggestion = db_session.scalar(select(AISuggestion).where(AISuggestion.enquiry_id == enquiry.id))
    assert response.status_code == 200
    assert "We have received your enquiry." in response.text
    assert suggestion.generation_status == "pending"


def test_unexpected_worker_exception_does_not_change_public_confirmation(
    public_client, supplied_packages, monkeypatch
):
    _use_lifecycle_settings(monkeypatch, _groq_settings())
    monkeypatch.setattr(BackgroundTasks, "add_task", _ORIGINAL_ADD_TASK)
    monkeypatch.setattr(
        public_routes,
        "process_extraction",
        lambda suggestion_id: (_ for _ in ()).throw(AIProviderError(AIErrorCode.GENERATION_FAILED)),
    )
    _, form = _get_form(public_client, supplied_packages["kerala"].slug)

    response = _post(public_client, supplied_packages["kerala"].slug, form)

    assert response.status_code == 200
    assert "We have received your enquiry." in response.text
    assert "email was sent" not in response.text.lower()
    assert "whatsapp message was sent" not in response.text.lower()
