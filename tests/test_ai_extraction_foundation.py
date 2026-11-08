from datetime import date
from decimal import Decimal
from threading import Event
from time import sleep
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import AIExtractionReviewEvent, AISuggestion, Enquiry, EnquiryReply, User
from app.schemas.ai_extraction import (
    AI_FAILURE_CODES,
    EnquiryExtraction,
    extraction_to_json,
    parse_extraction_json,
    validate_failure_code,
)
from app.services.ai_extraction_reviews import AIExtractionReviewError, append_extraction_review


def complete_payload(**overrides):
    payload = {
        "schema_version": "1.0",
        "destination": "Kerala",
        "travel_start_date": "2026-12-01",
        "travel_end_date": "2026-12-07",
        "original_travel_period_text": "1–7 December 2026",
        "adults": 2,
        "children": 1,
        "infants": 0,
        "budget_amount": "120000.00",
        "budget_currency": "INR",
        "budget_scope": "total",
        "preferences": ["houseboat"],
        "missing_information": [],
        "ambiguous_information": [],
    }
    payload.update(overrides)
    return payload


def test_valid_complete_extraction_uses_dates_and_decimal_safely():
    extraction = EnquiryExtraction.model_validate(complete_payload())

    assert extraction.travel_start_date == date(2026, 12, 1)
    assert extraction.travel_end_date == date(2026, 12, 7)
    assert extraction.budget_amount == Decimal("120000.00")
    assert extraction_to_json(extraction)["budget_amount"] == "120000.00"


def test_unknown_values_stay_null_and_ambiguous_period_is_explicit():
    extraction = EnquiryExtraction.model_validate(
        complete_payload(
            destination=None,
            travel_start_date=None,
            travel_end_date=None,
            original_travel_period_text="December",
            adults=None,
            children=None,
            infants=None,
            budget_amount=None,
            budget_currency=None,
            budget_scope=None,
            preferences=[],
            missing_information=[
                "destination", "travel_start_date", "travel_end_date", "adults", "children", "infants", "budget_amount"
            ],
            ambiguous_information=["travel_year"],
        )
    )
    assert extraction.destination is None
    assert extraction.travel_start_date is None
    assert extraction.budget_amount is None
    assert extraction.ambiguous_information


@pytest.mark.parametrize(
    "changes",
    [
        {"travel_start_date": "2026-02-30"},
        {"travel_start_date": "December 2026"},
        {"travel_start_date": "2026-12-08", "travel_end_date": "2026-12-01"},
        {"adults": -1},
        {"children": 101},
        {"destination": None},
        {"budget_amount": "1.234"},
        {"budget_amount": "99999999999.00"},
        {"budget_amount": "120000.00", "budget_currency": None},
        {"budget_amount": "120000.00", "budget_currency": "RUP"},
        {"budget_amount": "120000.00", "budget_currency": "INR", "budget_scope": "household"},
        {"unexpected": "field"},
    ],
)
def test_invalid_extraction_is_rejected(changes):
    with pytest.raises(ValidationError):
        EnquiryExtraction.model_validate(complete_payload(**changes))


def test_budget_float_and_malformed_json_are_rejected():
    with pytest.raises(ValidationError):
        EnquiryExtraction.model_validate(complete_payload(budget_amount=1.2))
    with pytest.raises(ValidationError):
        parse_extraction_json("{not json")


def test_negative_budget_is_rejected_and_zero_is_valid():
    with pytest.raises(ValidationError):
        EnquiryExtraction.model_validate(complete_payload(budget_amount="-0.01"))
    assert EnquiryExtraction.model_validate(complete_payload(budget_amount="0.00")).budget_amount == Decimal("0.00")


def test_traveller_total_and_boundaries():
    with pytest.raises(ValidationError, match="at least one traveller"):
        EnquiryExtraction.model_validate(complete_payload(adults=0, children=0, infants=0))
    assert EnquiryExtraction.model_validate(complete_payload(adults=1, children=0, infants=0)).adults == 1
    maximums = EnquiryExtraction.model_validate(complete_payload(adults=100, children=100, infants=100))
    assert (maximums.adults, maximums.children, maximums.infants) == (100, 100, 100)
    partial = EnquiryExtraction.model_validate(
        complete_payload(adults=0, children=0, infants=None, missing_information=["infants"])
    )
    assert partial.infants is None


def test_partial_dates_budget_scope_and_unknown_budget_are_explicit():
    with pytest.raises(ValidationError, match="must mark travel_end_date"):
        EnquiryExtraction.model_validate(complete_payload(travel_end_date=None))
    partial = EnquiryExtraction.model_validate(
        complete_payload(travel_end_date=None, missing_information=["travel_end_date"])
    )
    assert partial.travel_end_date is None

    with pytest.raises(ValidationError, match="mark budget_scope"):
        EnquiryExtraction.model_validate(complete_payload(budget_scope=None))
    scope_unknown = EnquiryExtraction.model_validate(
        complete_payload(budget_scope=None, ambiguous_information=["budget_scope"])
    )
    assert scope_unknown.budget_scope is None
    assert EnquiryExtraction.model_validate(complete_payload(budget_scope="per_person")).budget_scope == "per_person"

    with pytest.raises(ValidationError, match="require a known budget"):
        EnquiryExtraction.model_validate(complete_payload(budget_amount=None, budget_currency="INR"))
    with pytest.raises(ValidationError, match="mark budget_currency"):
        EnquiryExtraction.model_validate(complete_payload(budget_currency=None))
    currency_unknown = EnquiryExtraction.model_validate(
        complete_payload(budget_currency=None, ambiguous_information=["budget_currency"])
    )
    assert currency_unknown.budget_currency is None


def test_month_without_year_requires_ambiguity_and_unknown_dates():
    values = complete_payload(
        original_travel_period_text="December",
        travel_start_date=None,
        travel_end_date=None,
        missing_information=["travel_start_date", "travel_end_date"],
    )
    with pytest.raises(ValidationError, match="travel_year as ambiguous"):
        EnquiryExtraction.model_validate(values)
    with pytest.raises(ValidationError, match="Do not assign calendar dates"):
        EnquiryExtraction.model_validate(
            complete_payload(original_travel_period_text="December", ambiguous_information=["travel_year"])
        )


@pytest.mark.parametrize("currency", ["BHD", "ZAR", "XCD"])
def test_active_iso_currency_codes_are_supported(currency):
    assert EnquiryExtraction.model_validate(complete_payload(budget_currency=currency)).budget_currency == currency


def test_iso_currency_normalization_and_unknown_values():
    assert EnquiryExtraction.model_validate(complete_payload(budget_currency="inr")).budget_currency == "INR"
    for invalid in ("RUP", "XXX", "XTS", "XDR", "XAU"):
        with pytest.raises(ValidationError):
            EnquiryExtraction.model_validate(complete_payload(budget_currency=invalid))
    unknown = EnquiryExtraction.model_validate(
        complete_payload(budget_amount=None, budget_currency=None, budget_scope=None,
                         missing_information=["budget_amount"])
    )
    assert unknown.budget_currency is None and unknown.budget_scope is None


def test_failure_codes_are_allowlisted():
    for code in AI_FAILURE_CODES:
        assert validate_failure_code(code) == code
    for bad in ("arbitrary_error", "exception: token=secret", "provider response text"):
        with pytest.raises(ValueError):
            validate_failure_code(bad)


def test_ambiguous_month_without_year_is_kept_as_text_not_guessed():
    extraction = EnquiryExtraction.model_validate(
        complete_payload(
            travel_start_date=None,
            travel_end_date=None,
            original_travel_period_text="December",
            missing_information=["travel_start_date", "travel_end_date"],
            ambiguous_information=["travel_year"],
        )
    )
    assert extraction.travel_start_date is None
    assert extraction.travel_end_date is None
    assert extraction.original_travel_period_text == "December"


def test_suggestion_defaults_and_reply_provenance_are_backward_compatible(db_session):
    user = User(name="AI Schema Admin", email=f"{uuid4()}@example.test", role="admin")
    enquiry = Enquiry(original_message="Synthetic schema foundation test")
    suggestion = AISuggestion(enquiry=enquiry, provider=None, extracted_data={})
    db_session.add_all([user, enquiry, suggestion])
    db_session.flush()

    assert suggestion.generation_status == "pending"
    assert suggestion.extraction_review_status == "pending"
    assert suggestion.accepted_extraction is None
    assert suggestion.generation_attempt_id is None

    attempt_id = uuid4()
    suggestion.generation_attempt_id = attempt_id
    db_session.flush()
    assert db_session.get(AISuggestion, suggestion.id).generation_attempt_id == attempt_id

    reply = EnquiryReply(enquiry=enquiry, author=user, body="Synthetic draft", source_ai_suggestion=suggestion)
    db_session.add(reply)
    db_session.flush()
    assert reply.source_ai_suggestion_id == suggestion.id


def test_legacy_suggestion_fields_remain_usable(db_session):
    enquiry = Enquiry(original_message="Legacy compatibility")
    suggestion = AISuggestion(
        enquiry=enquiry,
        extracted_data={"destination": "Kerala"},
        package_suggestions=[{"package_code": "PKG-KL-07"}],
        draft_reply="Legacy draft",
        review_status="pending",
    )
    db_session.add(suggestion)
    db_session.flush()
    assert suggestion.extracted_data == {"destination": "Kerala"}
    assert suggestion.package_suggestions[0]["package_code"] == "PKG-KL-07"
    assert suggestion.draft_reply == "Legacy draft"
    assert suggestion.generation_attempt_id is None


def test_review_events_record_each_correction_and_are_immutable(db_session):
    actor = User(name="Review Actor", email=f"{uuid4()}@example.test", role="editor")
    enquiry = Enquiry(original_message="Synthetic extraction review test")
    original = {"schema_version": "1.0", "destination": "Kerala"}
    corrected = complete_payload(destination="Munnar")
    corrected_again = complete_payload(destination="Alleppey")
    suggestion = AISuggestion(enquiry=enquiry, extracted_data=original)
    db_session.add_all([actor, suggestion])
    db_session.flush()

    first = append_extraction_review(db_session, suggestion, actor=actor, accepted_extraction=corrected)
    second = append_extraction_review(db_session, suggestion, actor=actor, accepted_extraction=corrected_again)
    assert first.action_type == "accepted"
    assert first.previous_extraction is None
    assert second.action_type == "corrected"
    assert second.previous_extraction["destination"] == "Munnar"
    assert second.accepted_extraction["destination"] == "Alleppey"
    assert suggestion.extracted_data == original
    assert suggestion.accepted_extraction["destination"] == "Alleppey"
    assert suggestion.extraction_reviewed_by is actor
    assert suggestion.extraction_reviewed_at is not None
    assert first.actor is actor
    assert second.actor is actor

    first.action_type = "corrected"
    with pytest.raises(ValueError, match="immutable"):
        db_session.flush()
    db_session.rollback()


def test_review_event_cannot_be_deleted(db_session):
    enquiry = Enquiry(original_message="Synthetic immutable event test")
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={})
    db_session.add(suggestion)
    db_session.flush()
    event = AIExtractionReviewEvent(
        suggestion=suggestion,
        accepted_extraction={"schema_version": "1.0"},
        action_type="accepted",
    )
    db_session.add(event)
    db_session.flush()
    db_session.delete(event)
    with pytest.raises(ValueError, match="cannot be deleted"):
        db_session.flush()
    db_session.rollback()


@pytest.fixture
def review_setup(db_session):
    admin = User(name="Review Admin", email=f"{uuid4()}@example.test", role="admin")
    editor = User(name="Review Editor", email=f"{uuid4()}@example.test", role="editor")
    assigned = User(name="Assigned Consultant", email=f"{uuid4()}@example.test", role="consultant")
    other = User(name="Other Consultant", email=f"{uuid4()}@example.test", role="consultant")
    inactive = User(name="Inactive Editor", email=f"{uuid4()}@example.test", role="editor", is_active=False)
    enquiry = Enquiry(original_message="Synthetic review authorization test", assigned_to=assigned)
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={})
    unassigned_enquiry = Enquiry(original_message="Synthetic unassigned review test")
    unassigned_suggestion = AISuggestion(enquiry=unassigned_enquiry, extracted_data={})
    db_session.add_all([admin, editor, assigned, other, inactive, suggestion, unassigned_suggestion])
    db_session.flush()
    return {"admin": admin, "editor": editor, "assigned": assigned, "other": other,
            "inactive": inactive, "enquiry": enquiry, "suggestion": suggestion,
            "unassigned_enquiry": unassigned_enquiry, "unassigned_suggestion": unassigned_suggestion}


@pytest.mark.parametrize("actor_key", ["admin", "editor", "assigned"])
def test_allowed_reviewer_roles_are_attributed(db_session, review_setup, actor_key):
    actor = review_setup[actor_key]
    event = append_extraction_review(
        db_session, review_setup["suggestion"], actor=actor, accepted_extraction=complete_payload()
    )
    assert event.actor_id == actor.id
    assert event.actor is actor
    assert review_setup["suggestion"].extraction_reviewed_by_id == actor.id


@pytest.mark.parametrize(
    ("actor_key", "suggestion_key"),
    [("other", "suggestion"), ("other", "unassigned_suggestion"), ("inactive", "suggestion")],
)
def test_unassigned_consultant_and_inactive_staff_are_rejected(db_session, review_setup, actor_key, suggestion_key):
    with pytest.raises(AIExtractionReviewError) as error:
        append_extraction_review(
            db_session, review_setup[suggestion_key], actor=review_setup[actor_key],
            accepted_extraction=complete_payload()
        )
    assert error.value.status_code in {403, 404}
    db_session.expire(review_setup[suggestion_key])
    assert review_setup[suggestion_key].accepted_extraction is None


def test_event_insert_failure_rolls_back_accepted_extraction(db_session, review_setup, monkeypatch):
    original_flush = Session.flush

    def fail_event_insert(session, *args, **kwargs):
        if any(isinstance(value, AIExtractionReviewEvent) for value in session.new):
            raise RuntimeError("synthetic insertion failure")
        return original_flush(session, *args, **kwargs)

    monkeypatch.setattr(Session, "flush", fail_event_insert)
    with pytest.raises(AIExtractionReviewError, match="could not be saved"):
        append_extraction_review(
            db_session, review_setup["suggestion"], actor=review_setup["admin"],
            accepted_extraction=complete_payload()
        )
    monkeypatch.setattr(Session, "flush", original_flush)
    db_session.expire_all()
    fresh = db_session.get(AISuggestion, review_setup["suggestion"].id)
    assert fresh.accepted_extraction is None
    assert fresh.extraction_reviewed_by_id is None
    assert fresh.extraction_reviewed_at is None
    assert fresh.extraction_review_events == []


@pytest.mark.parametrize(
    "invalid_fields",
    [
        {"generation_status": "invalid"},
        {"extraction_review_status": "accepted"},
        {"failure_code": "provider exception token=secret"},
    ],
)
def test_database_rejects_invalid_lifecycle_and_failure_values(db_session, invalid_fields):
    enquiry = Enquiry(original_message="Synthetic lifecycle constraint test")
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={}, **invalid_fields)
    db_session.add(suggestion)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_concurrent_review_attempts_serialize_on_suggestion_lock():
    from concurrent.futures import ThreadPoolExecutor

    first_has_lock = Event()
    second_started = Event()
    release_first = Event()
    admin_one = User(name="Concurrent Admin A", email=f"{uuid4()}@example.test", role="admin")
    admin_two = User(name="Concurrent Admin B", email=f"{uuid4()}@example.test", role="admin")
    enquiry = Enquiry(original_message="Synthetic concurrent review test")
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={})
    admin_one.id, admin_two.id, enquiry.id, suggestion.id = (uuid4(), uuid4(), uuid4(), uuid4())
    with Session(engine) as setup:
        ids = (admin_one.id, admin_two.id, enquiry.id, suggestion.id)
        with setup.begin():
            setup.add_all([admin_one, admin_two, suggestion])
    outcomes = {}

    class RollBackReview(Exception):
        pass

    def first_review():
        try:
            with Session(engine) as session, session.begin():
                actor = session.get(User, ids[0])
                append_extraction_review(session, ids[3], actor=actor, accepted_extraction=complete_payload())
                first_has_lock.set()
                if not release_first.wait(5):
                    raise TimeoutError("review lock test timed out")
                raise RollBackReview()
        except RollBackReview:
            return

    def second_review():
        if not first_has_lock.wait(5):
            raise TimeoutError("first reviewer did not acquire row lock")
        try:
            with Session(engine) as session, session.begin():
                actor = session.get(User, ids[1])
                second_started.set()
                event = append_extraction_review(
                    session, ids[3], actor=actor, accepted_extraction=complete_payload(destination="Munnar")
                )
                outcomes["action"] = event.action_type
                outcomes["previous"] = event.previous_extraction
                raise RollBackReview()
        except RollBackReview:
            return

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(first_review)
            second_future = pool.submit(second_review)
            assert second_started.wait(5)
            sleep(0.15)
            release_first.set()
            first_future.result(8)
            second_future.result(8)
        assert outcomes == {"action": "accepted", "previous": None}
        with Session(engine) as verify:
            persisted = verify.get(AISuggestion, ids[3])
            assert persisted.accepted_extraction is None
            assert persisted.extraction_review_events == []
    finally:
        release_first.set()
        with engine.begin() as cleanup:
            cleanup.execute(text("DELETE FROM ai_suggestions WHERE id = :id"), {"id": ids[3]})
            cleanup.execute(text("DELETE FROM enquiries WHERE id = :id"), {"id": ids[2]})
            cleanup.execute(
                text("DELETE FROM users WHERE id IN (:first_id, :second_id)"),
                {"first_id": ids[0], "second_id": ids[1]},
            )


@pytest.mark.parametrize("mutation", ["bulk_update", "raw_update", "bulk_delete", "raw_delete"])
def test_database_trigger_rejects_bulk_and_direct_sql_mutations(db_session, mutation):
    enquiry = Enquiry(original_message="Synthetic database trigger test")
    suggestion = AISuggestion(enquiry=enquiry, extracted_data={})
    db_session.add(suggestion)
    db_session.flush()
    event = AIExtractionReviewEvent(
        suggestion=suggestion,
        accepted_extraction=extraction_to_json(EnquiryExtraction.model_validate(complete_payload())),
        action_type="accepted",
    )
    db_session.add(event)
    db_session.flush()
    is_delete = mutation.endswith("delete")

    with pytest.raises(DBAPIError):
        with db_session.begin_nested():
            if mutation == "bulk_update":
                db_session.execute(
                    update(AIExtractionReviewEvent)
                    .where(AIExtractionReviewEvent.id == event.id)
                    .values(action_type="corrected")
                )
            elif mutation == "bulk_delete":
                db_session.execute(delete(AIExtractionReviewEvent).where(AIExtractionReviewEvent.id == event.id))
            elif is_delete:
                db_session.execute(
                    text("DELETE FROM ai_extraction_review_events WHERE id = :event_id"),
                    {"event_id": event.id},
                )
            else:
                db_session.execute(
                    text("UPDATE ai_extraction_review_events SET action_type = 'corrected' WHERE id = :event_id"),
                    {"event_id": event.id},
                )
    db_session.refresh(event)
    assert event.action_type == "accepted"
