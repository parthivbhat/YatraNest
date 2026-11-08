"""Read-only enquiry query helpers with staff-scope enforcement."""

from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    AIExtractionReviewEvent,
    AISuggestion,
    Enquiry,
    EnquiryActivity,
    EnquiryNote,
    EnquiryReply,
    Package,
    Quotation,
    User,
)


ENQUIRY_STATUSES = ("new", "in_progress", "waiting_customer", "closed")


def _search_conditions(query: str) -> list:
    """Escape SQL wildcard characters so user search text remains literal."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return [
        Enquiry.customer_name.ilike(pattern, escape="\\"),
        Enquiry.customer_email.ilike(pattern, escape="\\"),
        Enquiry.customer_phone.ilike(pattern, escape="\\"),
        Enquiry.original_message.ilike(pattern, escape="\\"),
    ]


def enquiry_filters(
    *,
    staff: User,
    query: str,
    status: str | None,
    assigned_to_id: UUID | None,
    unassigned: bool,
) -> list:
    conditions = []
    if query:
        conditions.append(or_(*_search_conditions(query)))
    if status:
        conditions.append(Enquiry.status == status)
    if staff.role == "consultant":
        conditions.append(Enquiry.assigned_to_id == staff.id)
    elif unassigned:
        conditions.append(Enquiry.assigned_to_id.is_(None))
    elif assigned_to_id is not None:
        conditions.append(Enquiry.assigned_to_id == assigned_to_id)
    return conditions


def list_enquiries(
    db: Session,
    *,
    staff: User,
    query: str,
    status: str | None,
    assigned_to_id: UUID | None,
    unassigned: bool,
    page: int,
    page_size: int,
) -> tuple[list[Enquiry], int]:
    """Return a stable, scoped page and count for the staff inbox."""
    conditions = enquiry_filters(
        staff=staff,
        query=query,
        status=status,
        assigned_to_id=assigned_to_id,
        unassigned=unassigned,
    )
    total = db.scalar(select(func.count(Enquiry.id)).where(*conditions)) or 0
    rows = db.scalars(
        select(Enquiry)
        .options(selectinload(Enquiry.package), selectinload(Enquiry.assigned_to))
        .where(*conditions)
        .order_by(Enquiry.created_at.desc(), Enquiry.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def count_enquiries(
    db: Session,
    *,
    staff: User,
    query: str,
    status: str | None,
    assigned_to_id: UUID | None,
    unassigned: bool,
) -> int:
    """Count records under the same role and filter scope used by the inbox."""
    conditions = enquiry_filters(
        staff=staff,
        query=query,
        status=status,
        assigned_to_id=assigned_to_id,
        unassigned=unassigned,
    )
    return db.scalar(select(func.count(Enquiry.id)).where(*conditions)) or 0


def get_enquiry_for_staff(db: Session, enquiry_id: UUID, staff: User) -> Enquiry | None:
    """Fetch an enquiry only if the role is allowed to see that record.

    Consultants are scoped in SQL to their assigned records; returning None for
    records outside that scope avoids revealing their existence.
    """
    statement = select(Enquiry).where(Enquiry.id == enquiry_id)
    if staff.role == "consultant":
        statement = statement.where(Enquiry.assigned_to_id == staff.id)
    statement = statement.options(
        selectinload(Enquiry.package).selectinload(Package.prices),
        selectinload(Enquiry.assigned_to),
        selectinload(Enquiry.notes).selectinload(EnquiryNote.author),
        selectinload(Enquiry.activities)
        .selectinload(EnquiryActivity.actor),
        selectinload(Enquiry.activities)
        .selectinload(EnquiryActivity.from_assignee),
        selectinload(Enquiry.activities)
        .selectinload(EnquiryActivity.to_assignee),
        selectinload(Enquiry.replies).selectinload(EnquiryReply.author),
        selectinload(Enquiry.replies).selectinload(EnquiryReply.source_ai_suggestion),
        selectinload(Enquiry.replies).selectinload(EnquiryReply.approved_by),
        selectinload(Enquiry.replies).selectinload(EnquiryReply.manually_sent_by),
        selectinload(Enquiry.quotations).selectinload(Quotation.created_by),
        selectinload(Enquiry.quotations).selectinload(Quotation.approved_by),
        selectinload(Enquiry.ai_suggestions).selectinload(AISuggestion.extraction_reviewed_by),
        selectinload(Enquiry.ai_suggestions)
        .selectinload(AISuggestion.extraction_review_events)
        .selectinload(AIExtractionReviewEvent.actor),
    )
    return db.scalar(statement)
