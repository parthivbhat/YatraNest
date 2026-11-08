"""Transactional staff workflows for enquiry assignment, status and notes."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Enquiry, EnquiryNote, User
from app.services.enquiry_activity import append_enquiry_activity
from app.services.enquiry_queries import ENQUIRY_STATUSES


STATUS_TRANSITIONS = {
    "new": frozenset({"in_progress", "closed"}),
    "in_progress": frozenset({"waiting_customer", "closed"}),
    "waiting_customer": frozenset({"in_progress", "closed"}),
    "closed": frozenset({"in_progress"}),
}


class EnquiryWorkflowError(ValueError):
    """A safe, user-facing workflow validation or concurrency error."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _parse_expected_updated_at(value: str) -> datetime:
    try:
        expected = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EnquiryWorkflowError("Reload the enquiry and try again.", 409) from exc
    if expected.tzinfo is None:
        raise EnquiryWorkflowError("Reload the enquiry and try again.", 409)
    return expected


def _lock_enquiry(
    session: Session,
    enquiry_id: UUID,
    actor: User,
    expected_updated_at: str,
) -> Enquiry:
    statement = select(Enquiry).where(Enquiry.id == enquiry_id).with_for_update()
    if actor.role == "consultant":
        statement = statement.where(Enquiry.assigned_to_id == actor.id)
    enquiry = session.scalar(statement)
    if enquiry is None:
        raise EnquiryWorkflowError("Enquiry not found or outside your assigned work.", 404)
    expected = _parse_expected_updated_at(expected_updated_at)
    current = enquiry.updated_at
    if current is None or current != expected:
        raise EnquiryWorkflowError("This enquiry changed after you opened it. Reload and try again.", 409)
    return enquiry


def assign_enquiry(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    assignee_id: UUID | None,
    expected_updated_at: str,
) -> bool:
    """Assign/unassign as admin. Return False for an unchanged assignment."""
    if actor.role != "admin":
        raise EnquiryWorkflowError("Only administrators can assign enquiries.", 403)
    enquiry = _lock_enquiry(session, enquiry_id, actor, expected_updated_at)
    if assignee_id is not None:
        assignee = session.scalar(
            select(User)
            .where(User.id == assignee_id, User.role == "consultant", User.is_active.is_(True))
            .with_for_update()
        )
        if assignee is None:
            raise EnquiryWorkflowError("Choose an active consultant account.")
    old_assignee_id = enquiry.assigned_to_id
    if old_assignee_id == assignee_id:
        return False
    enquiry.assigned_to_id = assignee_id
    append_enquiry_activity(
        session,
        enquiry,
        actor_id=actor.id,
        event_type="assignment_changed",
        from_assigned_to_id=old_assignee_id,
        to_assigned_to_id=assignee_id,
    )
    session.flush()
    return True


def change_enquiry_status(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    new_status: str,
    expected_updated_at: str,
) -> bool:
    """Change status only along the approved transition graph."""
    if actor.role not in {"admin", "editor", "consultant"}:
        raise EnquiryWorkflowError("You do not have permission to update enquiries.", 403)
    if new_status not in ENQUIRY_STATUSES:
        raise EnquiryWorkflowError("Choose a valid enquiry status.")
    enquiry = _lock_enquiry(session, enquiry_id, actor, expected_updated_at)
    if enquiry.status == new_status:
        return False
    if new_status not in STATUS_TRANSITIONS.get(enquiry.status, frozenset()):
        raise EnquiryWorkflowError(
            f"Status cannot change from {enquiry.status.replace('_', ' ')} to {new_status.replace('_', ' ')}."
        )
    old_status = enquiry.status
    enquiry.status = new_status
    append_enquiry_activity(
        session,
        enquiry,
        actor_id=actor.id,
        event_type="status_changed",
        from_status=old_status,
        to_status=new_status,
    )
    session.flush()
    return True


def add_enquiry_note(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    note_text: str,
    expected_updated_at: str,
) -> EnquiryNote:
    """Append an internal-only note for an enquiry within the actor's scope."""
    if actor.role not in {"admin", "editor", "consultant"}:
        raise EnquiryWorkflowError("You do not have permission to add notes.", 403)
    _lock_enquiry(session, enquiry_id, actor, expected_updated_at)
    clean_note = note_text.strip()
    if not clean_note:
        raise EnquiryWorkflowError("Enter an internal note.")
    if len(clean_note) > 5000:
        raise EnquiryWorkflowError("Internal notes must be 5,000 characters or fewer.")
    note = EnquiryNote(enquiry_id=enquiry_id, author_id=actor.id, note=clean_note)
    session.add(note)
    session.flush()
    return note
