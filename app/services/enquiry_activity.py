"""Helpers for appending immutable enquiry audit activity."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Enquiry, EnquiryActivity


def append_enquiry_activity(
    session: Session,
    enquiry: Enquiry,
    *,
    actor_id: UUID | None,
    event_type: str,
    from_status: str | None = None,
    to_status: str | None = None,
    from_assigned_to_id: UUID | None = None,
    to_assigned_to_id: UUID | None = None,
) -> EnquiryActivity:
    """Add a validated audit event to the caller's transaction.

    The helper flushes so database constraints fail within the surrounding unit
    of work. It never commits; callers own transaction boundaries.
    """
    if event_type not in {"status_changed", "assignment_changed"}:
        raise ValueError("Unsupported enquiry activity event type.")
    if event_type == "status_changed" and to_status is None:
        raise ValueError("A status change activity requires a destination status.")
    if event_type == "assignment_changed" and from_assigned_to_id == to_assigned_to_id:
        raise ValueError("An assignment activity must record a changed assignee.")

    activity = EnquiryActivity(
        enquiry=enquiry,
        actor_id=actor_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        from_assigned_to_id=from_assigned_to_id,
        to_assigned_to_id=to_assigned_to_id,
    )
    session.add(activity)
    session.flush()
    return activity
