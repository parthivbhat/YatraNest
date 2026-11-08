"""PostgreSQL-backed tests for AI extraction lifecycle persistence and fencing."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, insert, update
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.database import SessionLocal
from app.models import AIExtractionReviewEvent, AISuggestion, Enquiry, EnquiryNote, User
from app.schemas.ai_extraction import EnquiryExtraction
from app.services.ai.base import GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai_extraction_lifecycle import (
    _persist_failure,
    _persist_success,
    claim_extraction_work,
    ensure_initial_suggestion,
    process_extraction,
    processing_stale_after,
)


def configured_settings(**overrides) -> Settings:
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


def worker_factory(db_connection):
    return sessionmaker(
        bind=db_connection,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


def valid_extraction_json() -> str:
    return (
        '{"schema_version":"1.0","destination":"Kerala",'
        '"travel_start_date":null,"travel_end_date":null,'
        '"original_travel_period_text":"December",'
        '"adults":2,"children":1,"infants":0,'
        '"budget_amount":"120000.00","budget_currency":"INR",'
        '"budget_scope":"total","preferences":["houseboat"],'
        '"missing_information":["travel_start_date","travel_end_date"],'
        '"ambiguous_information":["travel_year"]} '
    )


def add_suggestion(db_session, *, message="Kerala in December for 2 adults"):
    enquiry = Enquiry(original_message=message)
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={})
    db_session.add(suggestion)
    db_session.flush()
    return enquiry, suggestion


def response(content: str | None = None) -> GenerationResponse:
    return GenerationResponse(
        provider="groq",
        model="openai/gpt-oss-120b",
        content=content or valid_extraction_json(),
    )


def test_initial_suggestion_is_pending_when_groq_is_configured_and_idempotent(db_session):
    enquiry = Enquiry(
        original_message="Original exact message",
        customer_name="Synthetic Name",
        customer_email="synthetic@example.test",
        adults=2,
    )
    db_session.add(enquiry)
    db_session.flush()

    first = ensure_initial_suggestion(db_session, enquiry, settings=configured_settings())
    second = ensure_initial_suggestion(db_session, enquiry, settings=configured_settings())

    assert first.id == second.id
    assert first.generation_status == "pending"
    assert first.failure_code is None
    assert first.accepted_extraction is None
    assert enquiry.original_message == "Original exact message"
    assert enquiry.customer_name == "Synthetic Name"
    assert enquiry.customer_email == "synthetic@example.test"
    assert enquiry.adults == 2


@pytest.mark.parametrize(
    "settings",
    [
        configured_settings(ai_enabled=False),
        configured_settings(groq_api_key=None),
        configured_settings(groq_model=None),
        configured_settings(ai_provider="gemini", gemini_api_key="deferred-key", gemini_model="deferred-model"),
    ],
)
def test_initial_suggestion_is_unavailable_without_active_groq_configuration(db_session, settings):
    enquiry = Enquiry(original_message="Synthetic enquiry")
    db_session.add(enquiry)
    db_session.flush()

    suggestion = ensure_initial_suggestion(db_session, enquiry, settings=settings)

    assert suggestion.generation_status == "unavailable"
    assert suggestion.failure_code == "provider_unavailable"
    assert suggestion.accepted_extraction is None


def test_existing_legacy_suggestion_with_null_lifecycle_is_not_changed(db_session):
    enquiry = Enquiry(original_message="Legacy message")
    db_session.add(enquiry)
    db_session.flush()
    suggestion_id = db_session.execute(
        insert(AISuggestion)
        .values(
            enquiry_id=enquiry.id,
            extracted_data={"legacy": "raw"},
            generation_status=None,
            extraction_review_status=None,
            accepted_extraction={"legacy": "accepted"},
        )
        .returning(AISuggestion.id)
    ).scalar_one()
    suggestion = db_session.get(AISuggestion, suggestion_id)

    result = ensure_initial_suggestion(db_session, enquiry, settings=configured_settings())

    assert result.id == suggestion.id
    assert result.generation_status is None
    assert result.extraction_review_status is None
    assert result.accepted_extraction == {"legacy": "accepted"}
    assert result.extracted_data == {"legacy": "raw"}


def test_concurrent_initial_creation_uses_parent_row_lock_and_one_shared_helper():
    enquiry_id = uuid4()
    with SessionLocal.begin() as session:
        session.add(Enquiry(id=enquiry_id, original_message="Synthetic concurrent creation"))

    start = Barrier(2)

    def create_once():
        with SessionLocal() as session:
            enquiry = session.get(Enquiry, enquiry_id)
            start.wait(timeout=10)
            suggestion = ensure_initial_suggestion(
                session, enquiry, settings=configured_settings()
            )
            suggestion_id = suggestion.id
            session.commit()
            return suggestion_id

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(lambda _: create_once(), range(2)))
        assert ids[0] == ids[1]
        with SessionLocal() as session:
            assert session.query(AISuggestion).filter_by(enquiry_id=enquiry_id).count() == 1
    finally:
        with SessionLocal.begin() as session:
            session.execute(delete(Enquiry).where(Enquiry.id == enquiry_id))


def test_only_one_duplicate_task_claims_a_pending_suggestion():
    enquiry_id = uuid4()
    suggestion_id = uuid4()
    with SessionLocal.begin() as session:
        enquiry = Enquiry(id=enquiry_id, original_message="Synthetic claim race")
        session.add(enquiry)
        session.flush()
        session.add(AISuggestion(id=suggestion_id, enquiry_id=enquiry_id, extracted_data={}, generation_status="pending"))

    start = Barrier(2)

    def claim_once():
        start.wait(timeout=10)
        return claim_extraction_work(suggestion_id, settings=configured_settings())

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _: claim_once(), range(2)))
        assert sum(claim is not None for claim in claims) == 1
        with SessionLocal() as session:
            suggestion = session.get(AISuggestion, suggestion_id)
            assert suggestion.generation_status == "processing"
            assert suggestion.generation_attempt_id is not None
    finally:
        with SessionLocal.begin() as session:
            session.execute(delete(Enquiry).where(Enquiry.id == enquiry_id))


def test_completed_and_active_processing_suggestions_cannot_be_claimed(db_session, db_connection):
    factory = worker_factory(db_connection)
    enquiry, pending = add_suggestion(db_session)
    pending.generation_status = "processing"
    pending.generation_attempt_id = uuid4()
    db_session.flush()

    assert claim_extraction_work(pending.id, session_factory=factory, settings=configured_settings()) is None
    pending.generation_status = "completed"
    db_session.flush()
    assert claim_extraction_work(pending.id, session_factory=factory, settings=configured_settings()) is None


@pytest.mark.parametrize(
    ("status", "failure_code"),
    [("failed", "generation_failed"), ("unavailable", "provider_unavailable")],
)
def test_failed_and_unavailable_suggestions_are_retryable(db_session, db_connection, status, failure_code):
    factory = worker_factory(db_connection)
    _, suggestion = add_suggestion(db_session)
    suggestion.generation_status = status
    suggestion.failure_code = failure_code
    db_session.flush()

    claim = claim_extraction_work(suggestion.id, session_factory=factory, settings=configured_settings())

    assert claim is not None
    db_session.expire_all()
    saved = db_session.get(AISuggestion, suggestion.id)
    assert saved.generation_status == "processing"
    assert saved.generation_attempt_id == claim.attempt_id
    assert saved.failure_code is None


def test_stale_processing_attempt_can_be_reclaimed_with_a_new_uuid(db_session, db_connection):
    factory = worker_factory(db_connection)
    _, suggestion = add_suggestion(db_session)
    suggestion.generation_status = "processing"
    old_token = uuid4()
    suggestion.generation_attempt_id = old_token
    db_session.flush()
    now = datetime.now(UTC)
    stale_time = now - processing_stale_after(configured_settings()) - timedelta(seconds=1)
    db_session.execute(
        update(AISuggestion)
        .where(AISuggestion.id == suggestion.id)
        .values(updated_at=stale_time)
    )

    claim = claim_extraction_work(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        now=now,
    )

    assert claim is not None
    assert claim.attempt_id != old_token
    db_session.expire_all()
    stored = db_session.get(AISuggestion, suggestion.id)
    assert stored.generation_status == "processing"
    assert stored.generation_attempt_id == claim.attempt_id


def test_success_persists_canonical_extraction_and_groq_attribution(db_session, db_connection):
    factory = worker_factory(db_connection)
    enquiry, suggestion = add_suggestion(db_session)
    original = enquiry.original_message

    result = process_extraction(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        generator=lambda request, settings: response(),
    )

    assert result is True
    db_session.expire_all()
    saved = db_session.get(AISuggestion, suggestion.id)
    assert saved.generation_status == "completed"
    assert saved.generation_attempt_id is None
    assert saved.provider == "groq"
    assert saved.model_name == "openai/gpt-oss-120b"
    assert saved.generated_at is not None
    assert saved.extracted_data["budget_amount"] == "120000.00"
    assert saved.extracted_data["travel_start_date"] is None
    assert saved.accepted_extraction is None
    assert db_session.get(Enquiry, enquiry.id).original_message == original


@pytest.mark.parametrize(
    ("generator", "expected_status", "expected_code"),
    [
        (lambda request, settings: _raise(AIErrorCode.PROVIDER_TIMEOUT), "failed", "provider_timeout"),
        (lambda request, settings: _raise(AIErrorCode.PROVIDER_UNAVAILABLE), "unavailable", "provider_unavailable"),
        (lambda request, settings: response("not-json"), "failed", "invalid_provider_response"),
        (lambda request, settings: response('{"unexpected":true}'), "failed", "validation_failed"),
        (
            lambda request, settings: GenerationResponse("gemini", "deferred-model", valid_extraction_json()),
            "failed",
            "invalid_provider_response",
        ),
        (lambda request, settings: (_ for _ in ()).throw(RuntimeError("sensitive exception text")), "failed", "generation_failed"),
    ],
)
def test_provider_and_validation_failures_map_to_safe_codes(
    db_session, db_connection, generator, expected_status, expected_code
):
    factory = worker_factory(db_connection)
    _, suggestion = add_suggestion(db_session)

    assert process_extraction(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        generator=generator,
    ) is True
    db_session.expire_all()
    saved = db_session.get(AISuggestion, suggestion.id)
    assert saved.generation_status == expected_status
    assert saved.failure_code == expected_code
    assert saved.generation_attempt_id is None


def _raise(code):
    raise AIProviderError(code)


def test_old_worker_success_and_failure_are_discarded_after_reclaim(db_session, db_connection):
    factory = worker_factory(db_connection)
    _, suggestion = add_suggestion(db_session)
    first = claim_extraction_work(suggestion.id, session_factory=factory, settings=configured_settings())
    now = datetime.now(UTC) + processing_stale_after(configured_settings()) + timedelta(seconds=5)
    stale_time = now - processing_stale_after(configured_settings()) - timedelta(seconds=1)
    db_session.execute(
        update(AISuggestion)
        .where(AISuggestion.id == suggestion.id)
        .values(updated_at=stale_time)
    )
    second = claim_extraction_work(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        now=now,
    )
    assert first is not None and second is not None and first.attempt_id != second.attempt_id

    from app.services.ai_extraction_lifecycle import _validated_extraction

    extraction = _validated_extraction(response())
    assert not _persist_success(
        suggestion.id, first.attempt_id, response(), extraction, session_factory=factory
    )
    assert not _persist_failure(
        suggestion.id,
        first.attempt_id,
        status="failed",
        failure_code="provider_timeout",
        session_factory=factory,
    )
    assert _persist_success(suggestion.id, second.attempt_id, response(), extraction, session_factory=factory)

    db_session.expire_all()
    saved = db_session.get(AISuggestion, suggestion.id)
    assert saved.generation_status == "completed"
    assert saved.extracted_data["destination"] == "Kerala"
    assert saved.failure_code is None


def test_worker_preserves_staff_accepted_extraction_and_review_events(db_session, db_connection):
    from app.services.ai_extraction_reviews import append_extraction_review

    factory = worker_factory(db_connection)
    actor = User(name="Synthetic Editor", email=f"{uuid4()}@example.test", role="editor")
    enquiry, suggestion = add_suggestion(db_session)
    db_session.add(actor)
    db_session.flush()
    accepted = EnquiryExtraction.model_validate_json(valid_extraction_json(), strict=True)
    append_extraction_review(db_session, suggestion, actor=actor, accepted_extraction=accepted)
    db_session.flush()
    accepted_before = suggestion.accepted_extraction.copy()
    event_count = db_session.query(AIExtractionReviewEvent).filter_by(ai_suggestion_id=suggestion.id).count()

    assert process_extraction(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        generator=lambda request, settings: response(),
    ) is False  # Reviewed extractions are not claimed or replaced.

    db_session.expire_all()
    saved = db_session.get(AISuggestion, suggestion.id)
    assert saved.accepted_extraction == accepted_before
    assert db_session.query(AIExtractionReviewEvent).filter_by(ai_suggestion_id=suggestion.id).count() == event_count
    assert saved.generation_status == "pending"


def test_provider_receives_only_redacted_original_message(db_session, db_connection, supplied_packages):
    factory = worker_factory(db_connection)
    original = "Kerala in December. Email qa@example.invalid, call +91 98765 43210. Budget INR 120000."
    enquiry, suggestion = add_suggestion(db_session, message=original)
    enquiry.customer_name = "Private Customer"
    enquiry.customer_email = "column@example.invalid"
    enquiry.customer_phone = "+91 91234 56789"
    enquiry.whatsapp_consent = True
    enquiry.package = supplied_packages["kerala"]
    enquiry.adults = 2
    enquiry.budget_amount_inr = 120000
    author = User(name="Synthetic Staff", email=f"{uuid4()}@example.test", role="editor")
    db_session.add(author)
    db_session.flush()
    db_session.add(EnquiryNote(enquiry=enquiry, author=author, note="SECRET INTERNAL NOTE"))
    db_session.flush()
    captured = {}

    def fake_generator(request, settings):
        captured["prompt"] = request.prompt
        captured["output_model"] = request.output_model
        return response()

    assert process_extraction(
        suggestion.id,
        session_factory=factory,
        settings=configured_settings(),
        generator=fake_generator,
    ) is True
    prompt = captured["prompt"]
    assert "qa@example.invalid" not in prompt
    assert "column@example.invalid" not in prompt
    assert "+91 98765 43210" not in prompt
    assert "+91 91234 56789" not in prompt
    assert "Private Customer" not in prompt
    assert "SECRET INTERNAL NOTE" not in prompt
    assert "120000" in prompt  # Budget text remains available for extraction.
    assert "32500" not in prompt
    assert "Private Customer" not in prompt
    assert captured["output_model"] is EnquiryExtraction
    db_session.expire_all()
    saved_enquiry = db_session.get(Enquiry, enquiry.id)
    assert saved_enquiry.original_message == original
    assert saved_enquiry.customer_name == "Private Customer"
    assert saved_enquiry.customer_email == "column@example.invalid"
    assert saved_enquiry.customer_phone == "+91 91234 56789"
    assert saved_enquiry.whatsapp_consent is True
    assert saved_enquiry.adults == 2
    assert saved_enquiry.budget_amount_inr == 120000


def test_worker_session_closes_before_generation_and_persistence_failure_rolls_back(db_connection):
    closed_sessions = []

    class TrackingSession(Session):
        def close(self):
            closed_sessions.append(self)
            super().close()

    factory = sessionmaker(
        bind=db_connection,
        class_=TrackingSession,
        autoflush=False,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    enquiry_id, suggestion_id = uuid4(), uuid4()
    with factory.begin() as session:
        enquiry = Enquiry(id=enquiry_id, original_message="Synthetic rollback")
        session.add(enquiry)
        session.flush()
        session.add(AISuggestion(id=suggestion_id, enquiry_id=enquiry_id, extracted_data={}, generation_status="pending"))

    def fail_completed_flush(session, flush_context, instances):
        if any(
            isinstance(item, AISuggestion)
            and item.generation_status == "completed"
            for item in session.dirty
        ):
            raise RuntimeError("synthetic persistence failure")

    event.listen(TrackingSession, "before_flush", fail_completed_flush)
    try:
        def generator(request, settings):
            # The claim session has exited before the provider callback begins.
            assert closed_sessions
            assert all(not item.in_transaction() for item in closed_sessions)
            return response()

        assert process_extraction(
            suggestion_id,
            session_factory=factory,
            settings=configured_settings(),
            generator=generator,
        ) is False
    finally:
        event.remove(TrackingSession, "before_flush", fail_completed_flush)

    with factory() as session:
        saved = session.get(AISuggestion, suggestion_id)
        assert saved.generation_status == "processing"
        assert saved.extracted_data == {}
        assert saved.generation_attempt_id is not None
    with factory.begin() as session:
        session.execute(delete(Enquiry).where(Enquiry.id == enquiry_id))
