from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Enquiry, EnquiryActivity, EnquiryReply, Package, User
from app.services.enquiry_activity import append_enquiry_activity


def make_enquiry(db_session: Session, *, code: str = "ENQ-SCHEMA") -> Enquiry:
    package = Package(
        package_code=f"{code}-{uuid4().hex[:6]}",
        slug=f"{code.lower()}-{uuid4().hex[:8]}",
        title="Schema test package",
        status="published",
    )
    enquiry = Enquiry(
        package=package,
        customer_name="Sample Traveller",
        customer_email="traveller@example.test",
        original_message="  Please plan a flexible Kerala trip.\nHouseboat preferred.  ",
        travel_start_date=date(2027, 12, 1),
        travel_end_date=date(2027, 12, 31),
        travel_dates_flexible=True,
        adults=2,
        children=1,
        infants=0,
        budget_amount_inr=Decimal("120000.00"),
        response_notice_version="m6-v1",
        response_notice_accepted_at=datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
    )
    db_session.add(enquiry)
    db_session.flush()
    return enquiry


def test_enquiry_structured_fields_and_relationships_persist(db_session: Session) -> None:
    enquiry = make_enquiry(db_session)
    admin = User(name="Schema Admin", email=f"{uuid4()}@example.test", role="admin")
    consultant = User(name="Schema Consultant", email=f"{uuid4()}@example.test", role="consultant")
    db_session.add_all([admin, consultant])
    db_session.flush()

    activity = append_enquiry_activity(
        db_session,
        enquiry,
        actor_id=admin.id,
        event_type="status_changed",
        from_status="new",
        to_status="in_progress",
    )
    reply = EnquiryReply(enquiry=enquiry, author=consultant, body="Thank you for your enquiry.")
    db_session.add(reply)
    db_session.flush()

    assert enquiry.original_message == "  Please plan a flexible Kerala trip.\nHouseboat preferred.  "
    assert enquiry.travel_dates_flexible is True
    assert (enquiry.adults, enquiry.children, enquiry.infants) == (2, 1, 0)
    assert enquiry.budget_amount_inr == Decimal("120000.00")
    assert enquiry.response_notice_version == "m6-v1"
    assert enquiry.activities == [activity]
    assert enquiry.replies == [reply]
    assert activity.actor is admin


@pytest.mark.parametrize(
    "field,value",
    [
        ("travel_end_date", None),
        ("travel_dates_flexible", None),
        ("adults", -1),
        ("children", -1),
        ("infants", -1),
        ("budget_amount_inr", Decimal("-0.01")),
    ],
)
def test_enquiry_check_constraints_reject_invalid_structured_fields(
    db_session: Session, field: str, value
) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            values = {
                "customer_name": "Traveller",
                "original_message": "Original text",
                "travel_start_date": date(2027, 10, 1),
                "travel_end_date": date(2027, 10, 2),
                "travel_dates_flexible": False,
                "adults": 1,
                "children": 0,
                "infants": 0,
                field: value,
            }
            db_session.add(Enquiry(**values))
            db_session.flush()


def test_enquiry_rejects_reversed_travel_dates(db_session: Session) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                Enquiry(
                    customer_name="Traveller",
                    original_message="Original text",
                    travel_start_date=date(2027, 12, 31),
                    travel_end_date=date(2027, 12, 1),
                    travel_dates_flexible=True,
                )
            )
            db_session.flush()


def test_activity_rows_are_immutable(db_session: Session) -> None:
    enquiry = make_enquiry(db_session)
    activity = append_enquiry_activity(
        db_session,
        enquiry,
        actor_id=None,
        event_type="status_changed",
        from_status="new",
        to_status="closed",
    )
    with pytest.raises(ValueError, match="immutable"):
        with db_session.begin_nested():
            activity.to_status = "in_progress"
            db_session.flush()

    with pytest.raises(ValueError, match="cannot be deleted"):
        with db_session.begin_nested():
            db_session.delete(activity)
            db_session.flush()


def test_approved_reply_body_cannot_be_edited(db_session: Session) -> None:
    enquiry = make_enquiry(db_session)
    admin = User(name="Reply Admin", email=f"{uuid4()}@example.test", role="admin")
    author = User(name="Reply Editor", email=f"{uuid4()}@example.test", role="editor")
    db_session.add_all([admin, author])
    db_session.flush()
    reply = EnquiryReply(
        enquiry=enquiry,
        author=author,
        body="Approved reply",
        status="approved",
        approved_by=admin,
        approved_at=datetime.now(UTC),
    )
    db_session.add(reply)
    db_session.flush()

    with pytest.raises(ValueError, match="create a new draft"):
        with db_session.begin_nested():
            reply.body = "Edited after approval"
            db_session.flush()


@pytest.mark.parametrize(
    "reply_values",
    [
        {"status": "unknown"},
        {"status": "approved"},
        {"status": "manually_sent", "approved_at": datetime.now(UTC)},
    ],
)
def test_reply_state_constraints_require_valid_metadata(db_session: Session, reply_values: dict) -> None:
    enquiry = make_enquiry(db_session)
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(EnquiryReply(enquiry=enquiry, body="Reply", **reply_values))
            db_session.flush()
