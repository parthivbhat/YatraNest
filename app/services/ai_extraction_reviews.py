"""Authorized, transactional human review of an AI extraction."""

import json
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AIExtractionReviewEvent, AISuggestion, Enquiry, User
from app.schemas.ai_extraction import EnquiryExtraction, extraction_to_json


class AIExtractionReviewError(ValueError):
    """Safe review error with an HTTP-compatible status code for route adapters."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        self.status_code = status_code
        super().__init__(message)


def append_extraction_review(
    session: Session,
    suggestion: AISuggestion | UUID,
    *,
    actor: User,
    accepted_extraction: EnquiryExtraction | Mapping[str, Any],
) -> AIExtractionReviewEvent:
    """Validate, authorize, lock, and append a human acceptance/correction.

    The operation uses a savepoint inside the caller's transaction: the helper
    never commits, while a failed event insert rolls back the accepted-value
    update. The original ``extracted_data`` is never changed. Row locks serialize
    corrections so each event's previous value follows the accepted-value chain.
    """
    if not isinstance(actor, User):
        raise AIExtractionReviewError("An authenticated staff account is required.", 403)
    # begin_nested() flushes pending work before opening its savepoint. Refuse
    # dirty sessions so that pre-existing caller changes cannot escape the
    # review operation's rollback boundary.
    if session.new or session.deleted or any(session.is_modified(item) for item in session.dirty):
        raise AIExtractionReviewError("Save or rollback other changes before reviewing this extraction.", 409)
    if isinstance(accepted_extraction, EnquiryExtraction):
        validated = accepted_extraction
    else:
        try:
            validated = EnquiryExtraction.model_validate_json(json.dumps(accepted_extraction), strict=True)
        except (TypeError, ValueError) as exc:
            raise AIExtractionReviewError("The corrected extraction is invalid.") from exc
    new_value = extraction_to_json(validated)
    suggestion_id = suggestion.id if isinstance(suggestion, AISuggestion) else suggestion

    try:
        with session.begin_nested():
            current_actor = session.scalar(
                select(User)
                .where(User.id == actor.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current_actor is None or not current_actor.is_active:
                raise AIExtractionReviewError("An active staff account is required.", 403)
            if current_actor.role not in {"admin", "editor", "consultant"}:
                raise AIExtractionReviewError("This staff role cannot review AI extractions.", 403)

            current_suggestion = session.scalar(
                select(AISuggestion)
                .where(AISuggestion.id == suggestion_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if current_suggestion is None:
                raise AIExtractionReviewError("AI suggestion not found.", 404)

            enquiry = session.scalar(
                select(Enquiry)
                .where(Enquiry.id == current_suggestion.enquiry_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if enquiry is None:
                raise AIExtractionReviewError("Enquiry not found.", 404)
            if current_actor.role == "consultant" and enquiry.assigned_to_id != current_actor.id:
                raise AIExtractionReviewError("Enquiry not found or outside your assigned work.", 404)

            previous_value = current_suggestion.accepted_extraction
            action = "accepted" if previous_value is None else "corrected"
            event = AIExtractionReviewEvent(
                suggestion=current_suggestion,
                actor=current_actor,
                previous_extraction=previous_value,
                accepted_extraction=new_value,
                action_type=action,
            )
            current_suggestion.accepted_extraction = new_value
            current_suggestion.extraction_review_status = action
            current_suggestion.extraction_reviewed_by = current_actor
            current_suggestion.extraction_reviewed_at = datetime.now(timezone.utc)
            session.add(event)
            session.flush()
        return event
    except AIExtractionReviewError:
        raise
    except Exception:
        # The savepoint has rolled back. Keep provider/DB exception details out
        # of user-facing errors and logs; callers can record a safe code.
        raise AIExtractionReviewError("The extraction review could not be saved. Reload and try again.", 409) from None
