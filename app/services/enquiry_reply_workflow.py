"""Staff reply drafting and recordkeeping workflow."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AISuggestion, Enquiry, EnquiryReply, User
from app.services.enquiry_workflow import EnquiryWorkflowError


MAX_REPLY_BODY_LENGTH = 10_000


class EnquiryReplyWorkflowError(EnquiryWorkflowError):
    """Safe validation, authorization, or concurrency error for reply actions."""


def _expected_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise EnquiryReplyWorkflowError("Reload the enquiry and try again.", 409) from exc
    if parsed.tzinfo is None:
        raise EnquiryReplyWorkflowError("Reload the enquiry and try again.", 409)
    return parsed


def _lock_enquiry(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    expected_updated_at: str,
) -> Enquiry:
    if actor.role not in {"admin", "editor", "consultant"}:
        raise EnquiryReplyWorkflowError("You do not have permission to manage replies.", 403)
    statement = (
        select(Enquiry)
        .where(Enquiry.id == enquiry_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if actor.role == "consultant":
        statement = statement.where(Enquiry.assigned_to_id == actor.id)
    enquiry = session.scalar(statement)
    if enquiry is None:
        raise EnquiryReplyWorkflowError("Enquiry not found or outside your assigned work.", 404)
    if enquiry.updated_at != _expected_timestamp(expected_updated_at):
        raise EnquiryReplyWorkflowError("This enquiry changed after you opened it. Reload and try again.", 409)
    return enquiry


def _lock_reply(
    session: Session,
    *,
    enquiry: Enquiry,
    reply_id: UUID,
    expected_updated_at: str,
) -> EnquiryReply:
    reply = session.scalar(
        select(EnquiryReply)
        .where(EnquiryReply.id == reply_id, EnquiryReply.enquiry_id == enquiry.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if reply is None:
        raise EnquiryReplyWorkflowError("Reply not found for this enquiry.", 404)
    if reply.updated_at != _expected_timestamp(expected_updated_at):
        raise EnquiryReplyWorkflowError("This reply changed after you opened it. Reload and try again.", 409)
    return reply


def _validated_body(body: str) -> str:
    clean_body = body.strip()
    if not clean_body:
        raise EnquiryReplyWorkflowError("Enter a reply before saving.")
    if len(clean_body) > MAX_REPLY_BODY_LENGTH:
        raise EnquiryReplyWorkflowError(
            f"Replies must be {MAX_REPLY_BODY_LENGTH:,} characters or fewer."
        )
    return clean_body


def create_reply_draft(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    body: str,
    expected_enquiry_updated_at: str,
    reply_id: UUID | None = None,
    supersedes_reply_id: UUID | None = None,
    source_ai_suggestion_id: UUID | None = None,
    expected_source_suggestion_updated_at: str | None = None,
) -> EnquiryReply:
    """Create a separate draft authored by the authenticated staff member."""
    # Use the same suggestion-before-enquiry lock order as extraction review.
    # This both validates provenance and rejects a draft based on stale AI data.
    if source_ai_suggestion_id is not None:
        if expected_source_suggestion_updated_at is None:
            raise EnquiryReplyWorkflowError("Reload the extraction and try again.", 409)
        source = session.scalar(
            select(AISuggestion)
            .where(
                AISuggestion.id == source_ai_suggestion_id,
                AISuggestion.enquiry_id == enquiry_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source is None:
            raise EnquiryReplyWorkflowError("The extraction used for this draft is no longer available.", 409)
        if source.updated_at != _expected_timestamp(expected_source_suggestion_updated_at):
            raise EnquiryReplyWorkflowError("Extraction changed while the draft was generated. Reload and try again.", 409)
    enquiry = _lock_enquiry(
        session,
        enquiry_id=enquiry_id,
        actor=actor,
        expected_updated_at=expected_enquiry_updated_at,
    )
    if supersedes_reply_id is not None:
        source_reply = session.scalar(
            select(EnquiryReply)
            .where(EnquiryReply.id == supersedes_reply_id, EnquiryReply.enquiry_id == enquiry.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source_reply is None:
            raise EnquiryReplyWorkflowError("The reply to correct was not found for this enquiry.", 404)
        if source_reply.status not in {"approved", "manually_sent"}:
            raise EnquiryReplyWorkflowError("Only approved or manually sent replies can be corrected.", 409)
    if reply_id is not None:
        existing = session.scalar(
            select(EnquiryReply)
            .where(EnquiryReply.id == reply_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is not None:
            if (
                existing.enquiry_id != enquiry.id
                or existing.author_id != actor.id
                or existing.source_ai_suggestion_id != source_ai_suggestion_id
            ):
                raise EnquiryReplyWorkflowError("This draft request was already used. Reload the enquiry and try again.", 409)
            return existing
    reply = EnquiryReply(
        id=reply_id,
        enquiry_id=enquiry.id,
        author_id=actor.id,
        body=_validated_body(body),
        status="draft",
        supersedes_reply_id=supersedes_reply_id,
        source_ai_suggestion_id=source_ai_suggestion_id,
    )
    session.add(reply)
    session.flush()
    return reply


def update_reply_draft(
    session: Session,
    *,
    enquiry_id: UUID,
    reply_id: UUID,
    actor: User,
    body: str,
    expected_enquiry_updated_at: str,
    expected_reply_updated_at: str,
) -> EnquiryReply:
    """Edit only a still-draft reply; approved content remains immutable."""
    enquiry = _lock_enquiry(
        session,
        enquiry_id=enquiry_id,
        actor=actor,
        expected_updated_at=expected_enquiry_updated_at,
    )
    reply = _lock_reply(
        session,
        enquiry=enquiry,
        reply_id=reply_id,
        expected_updated_at=expected_reply_updated_at,
    )
    if reply.status != "draft":
        raise EnquiryReplyWorkflowError("Only draft replies can be edited.", 409)
    reply.body = _validated_body(body)
    session.flush()
    return reply


def approve_reply(
    session: Session,
    *,
    enquiry_id: UUID,
    reply_id: UUID,
    actor: User,
    expected_enquiry_updated_at: str,
    expected_reply_updated_at: str,
) -> EnquiryReply:
    """Approve a draft as an administrator without marking it sent."""
    if actor.role != "admin":
        raise EnquiryReplyWorkflowError("Administrator approval is required.", 403)
    enquiry = _lock_enquiry(
        session,
        enquiry_id=enquiry_id,
        actor=actor,
        expected_updated_at=expected_enquiry_updated_at,
    )
    reply = _lock_reply(
        session,
        enquiry=enquiry,
        reply_id=reply_id,
        expected_updated_at=expected_reply_updated_at,
    )
    if reply.status != "draft":
        raise EnquiryReplyWorkflowError("Only a draft reply can be approved.", 409)
    reply.status = "approved"
    reply.approved_by_id = actor.id
    reply.approved_at = datetime.now(UTC)
    session.flush()
    return reply


def record_reply_manually_sent(
    session: Session,
    *,
    enquiry_id: UUID,
    reply_id: UUID,
    actor: User,
    expected_enquiry_updated_at: str,
    expected_reply_updated_at: str,
) -> EnquiryReply:
    """Record prior external manual sending; this function never sends a message."""
    if actor.role not in {"admin", "editor", "consultant"}:
        raise EnquiryReplyWorkflowError("You do not have permission to record replies.", 403)
    enquiry = _lock_enquiry(
        session,
        enquiry_id=enquiry_id,
        actor=actor,
        expected_updated_at=expected_enquiry_updated_at,
    )
    reply = _lock_reply(
        session,
        enquiry=enquiry,
        reply_id=reply_id,
        expected_updated_at=expected_reply_updated_at,
    )
    if actor.role == "consultant" and reply.author_id != actor.id:
        raise EnquiryReplyWorkflowError("Consultants can record only replies they authored.", 403)
    if reply.status != "approved":
        raise EnquiryReplyWorkflowError("Only an approved reply can be marked manually sent.", 409)
    reply.status = "manually_sent"
    reply.manually_sent_by_id = actor.id
    reply.manually_sent_at = datetime.now(UTC)
    session.flush()
    return reply


def get_reply_for_staff(
    session: Session,
    *,
    enquiry_id: UUID,
    reply_id: UUID,
    actor: User,
    require_consultant_author: bool = False,
) -> EnquiryReply | None:
    """Fetch a reply only inside the staff member's enquiry scope."""
    statement = (
        select(EnquiryReply)
        .join(Enquiry, Enquiry.id == EnquiryReply.enquiry_id)
        .where(EnquiryReply.id == reply_id, Enquiry.id == enquiry_id)
    )
    if actor.role == "consultant":
        statement = statement.where(Enquiry.assigned_to_id == actor.id)
        if require_consultant_author:
            statement = statement.where(EnquiryReply.author_id == actor.id)
    return session.scalar(statement)
