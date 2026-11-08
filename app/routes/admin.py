"""Staff package dashboard and draft package forms."""

from datetime import datetime
import json
import logging
import math
from decimal import InvalidOperation
from typing import Annotated
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import Text, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.database import get_db
from app.config import get_settings
from app.models import AISuggestion, Enquiry, EnquiryNote, EnquiryReply, Package, PackageDraft, PackageVersion, User
from app.services.authorization import get_admin_page_user, get_editor_page_user, get_staff_page_user
from app.services.package_management import (
    create_draft_package,
    duplicate_package,
    package_code_exists,
    save_pending_package_draft,
    update_draft_package,
)
from app.services.package_validation import PackageValidationError, parse_package_payload
from app.services.package_preview import PackagePreview, preview_from_package, preview_from_payload
from app.services.package_publishing import PackageWorkflowError, publish_package, unpublish_package
from app.services.package_restoration import (
    PackageRestoreError,
    PackageRestoreValidationError,
    restore_package_version,
)
from app.services.package_history import (
    compare_snapshot_fields,
    compare_snapshot_prices,
    snapshot_fields_for_display,
    snapshot_prices_for_display,
)
from app.services.enquiry_queries import (
    ENQUIRY_STATUSES,
    count_enquiries,
    get_enquiry_for_staff,
    list_enquiries,
)
from app.services.enquiry_workflow import (
    STATUS_TRANSITIONS,
    EnquiryWorkflowError,
    add_enquiry_note,
    assign_enquiry,
    change_enquiry_status,
)
from app.services.enquiry_reply_workflow import (
    EnquiryReplyWorkflowError,
    approve_reply,
    create_reply_draft,
    get_reply_for_staff,
    record_reply_manually_sent,
    update_reply_draft,
)
from app.services.ai_reply_drafting import (
    AIReplyDraftError,
    ai_reply_drafting_available,
    generate_reply_draft_content,
)
from app.services.ai.diagnostics import log_ai_diagnostic
from app.services.session_store import require_valid_csrf
from app.schemas.ai_extraction import EnquiryExtraction
from app.services.ai_extraction_lifecycle import is_retry_eligible, process_extraction
from app.services.ai_extraction_reviews import AIExtractionReviewError, append_extraction_review
from app.services.package_recommendations import recommend_packages
from app.templating import templates


router = APIRouter(prefix="/admin", tags=["staff dashboard"])
logger = logging.getLogger(__name__)
PACKAGE_STATUSES = ("draft", "published", "archived")


def _private_history_response(response: HTMLResponse) -> HTMLResponse:
    """Prevent historical staff views from being cached or indexed."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def get_package_form_data(
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str | None, Form()] = None,
    expected_base_updated_at: Annotated[str | None, Form()] = None,
    expected_draft_updated_at: Annotated[str | None, Form()] = None,
    package_code: Annotated[str | None, Form()] = None,
    title: Annotated[str | None, Form()] = None,
    summary: Annotated[str | None, Form()] = None,
    description: Annotated[str | None, Form()] = None,
    destinations: Annotated[str | None, Form()] = None,
    duration_days: Annotated[str | None, Form()] = None,
    hotel_category: Annotated[str | None, Form()] = None,
    map_url: Annotated[str | None, Form()] = None,
    inclusions: Annotated[str | None, Form()] = None,
    hotel_name: Annotated[list[str] | None, Form()] = None,
    hotel_location: Annotated[list[str] | None, Form()] = None,
    itinerary_day: Annotated[list[str] | None, Form()] = None,
    itinerary_title: Annotated[list[str] | None, Form()] = None,
    itinerary_description: Annotated[list[str] | None, Form()] = None,
    price_season_name: Annotated[list[str] | None, Form()] = None,
    price_start_month: Annotated[list[str] | None, Form()] = None,
    price_end_month: Annotated[list[str] | None, Form()] = None,
    price_per_person: Annotated[list[str] | None, Form()] = None,
    price_valid_from: Annotated[list[str] | None, Form()] = None,
    price_valid_until: Annotated[list[str] | None, Form()] = None,
    price_note: Annotated[list[str] | None, Form()] = None,
) -> dict[str, object]:
    """Collect only supported form fields; status and slug are intentionally absent."""
    return {
        "csrf_token": csrf_token,
        "expected_updated_at": expected_updated_at,
        "expected_base_updated_at": expected_base_updated_at,
        "expected_draft_updated_at": expected_draft_updated_at,
        "package_code": package_code,
        "title": title,
        "summary": summary,
        "description": description,
        "destinations": destinations,
        "duration_days": duration_days,
        "hotel_category": hotel_category,
        "map_url": map_url,
        "inclusions": inclusions,
        "hotel_name": hotel_name,
        "hotel_location": hotel_location,
        "itinerary_day": itinerary_day,
        "itinerary_title": itinerary_title,
        "itinerary_description": itinerary_description,
        "price_season_name": price_season_name,
        "price_start_month": price_start_month,
        "price_end_month": price_end_month,
        "price_per_person": price_per_person,
        "price_valid_from": price_valid_from,
        "price_valid_until": price_valid_until,
        "price_note": price_note,
    }


def _search_conditions(query: str) -> list:
    """Build escaped text search conditions for package labels and destinations."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return [
        Package.package_code.ilike(pattern, escape="\\"),
        Package.title.ilike(pattern, escape="\\"),
        Package.destinations.cast(Text).ilike(pattern, escape="\\"),
    ]


def _destination_label(destinations: list[str] | None) -> str:
    if not destinations:
        return "Not specified"
    return ", ".join(str(value) for value in destinations if value) or "Not specified"


_EXTRACTION_FAILURE_MESSAGES = {
    "provider_unavailable": "AI extraction is unavailable right now. Staff can continue using the original enquiry.",
    "provider_timeout": "The AI provider did not respond in time. You can retry later or continue manually.",
    "invalid_provider_response": "The AI provider returned an unusable result. The original enquiry is unchanged.",
    "validation_failed": "The generated extraction did not pass validation. Review the original enquiry manually.",
    "generation_failed": "Extraction could not be completed. You can retry or continue manually.",
}


def _typed_extraction(payload: object) -> EnquiryExtraction | None:
    """Parse JSONB through the canonical strict schema; malformed legacy data is display-safe."""
    if not isinstance(payload, dict):
        return None
    try:
        return EnquiryExtraction.model_validate_json(json.dumps(payload), strict=True)
    except (TypeError, ValueError):
        return None


def _extraction_display(payload: object) -> dict[str, object] | None:
    extraction = _typed_extraction(payload)
    if extraction is None:
        return None
    return {
        "destination": extraction.destination,
        "start_date": extraction.travel_start_date,
        "end_date": extraction.travel_end_date,
        "flexible": extraction.travel_dates_flexible,
        "period_text": extraction.original_travel_period_text,
        "adults": extraction.adults,
        "children": extraction.children,
        "infants": extraction.infants,
        "budget_amount": extraction.budget_amount,
        "budget_currency": extraction.budget_currency,
        "budget_scope": extraction.budget_scope,
        "preferences": extraction.preferences,
        "missing_information": extraction.missing_information,
        "ambiguous_information": extraction.ambiguous_information,
    }


def _extraction_form_values(suggestion: AISuggestion | None) -> dict[str, object]:
    """Prefer staff-accepted values; otherwise prefill only valid raw extraction."""
    if suggestion is None:
        extraction = None
    else:
        extraction = (
            _typed_extraction(suggestion.accepted_extraction)
            if suggestion.extraction_review_status in {"accepted", "corrected"}
            else None
        )
        if extraction is None:
            extraction = _typed_extraction(suggestion.extracted_data)
    if extraction is None:
        extraction = EnquiryExtraction(
            missing_information=[
                "destination", "travel_start_date", "travel_end_date",
                "adults", "children", "infants", "budget_amount",
            ]
        )
    return {
        "destination": extraction.destination or "",
        "travel_start_date": extraction.travel_start_date.isoformat() if extraction.travel_start_date else "",
        "travel_end_date": extraction.travel_end_date.isoformat() if extraction.travel_end_date else "",
        "travel_dates_flexible": "unknown" if extraction.travel_dates_flexible is None else str(extraction.travel_dates_flexible).lower(),
        "original_travel_period_text": extraction.original_travel_period_text or "",
        "adults": "" if extraction.adults is None else extraction.adults,
        "children": "" if extraction.children is None else extraction.children,
        "infants": "" if extraction.infants is None else extraction.infants,
        "budget_amount": "" if extraction.budget_amount is None else format(extraction.budget_amount, "f"),
        "budget_currency": extraction.budget_currency or "",
        "budget_scope": extraction.budget_scope or "",
        "preferences": "\n".join(extraction.preferences),
        "missing_information": extraction.missing_information,
        "ambiguous_information": extraction.ambiguous_information,
    }


@router.get("/packages", response_class=HTMLResponse, name="admin_packages")
def package_dashboard(
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    """List all package statuses for an authenticated staff member."""
    normalized_query = (q or "").strip()
    normalized_status = (status or "").strip().lower() or None
    if normalized_status is not None and normalized_status not in PACKAGE_STATUSES:
        raise HTTPException(status_code=422, detail="Choose draft, published, or archived status.")

    statement = (
        select(Package)
        .options(selectinload(Package.updated_by), selectinload(Package.pending_draft))
        .order_by(Package.updated_at.desc(), Package.title.asc())
    )
    if normalized_query:
        statement = statement.where(or_(*_search_conditions(normalized_query)))
    if normalized_status:
        statement = statement.where(Package.status == normalized_status)

    packages = list(db.scalars(statement))
    rows = [
        {
            "package": package,
            "destination_label": _destination_label(package.destinations),
            "updated_by_label": package.updated_by.name if package.updated_by else "Not recorded",
            "has_pending_changes": package.pending_draft is not None,
            "pending_updated_at": package.pending_draft.updated_at if package.pending_draft else None,
        }
        for package in packages
    ]
    session_record = request.state.staff_session
    response = templates.TemplateResponse(
        request=request,
        name="admin/packages.html",
        context={
            "page_title": "Package dashboard | YatraNest",
            "meta_description": "Staff package dashboard.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/packages",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": session_record.csrf_token,
            "packages": rows,
            "query": normalized_query,
            "selected_status": normalized_status or "",
            "status_options": PACKAGE_STATUSES,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/enquiries", response_class=HTMLResponse, name="admin_enquiries")
def enquiry_inbox(
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
    q: Annotated[str | None, Query(max_length=160)] = None,
    status: Annotated[str | None, Query(max_length=30)] = None,
    assigned_to: Annotated[str | None, Query(max_length=40)] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> HTMLResponse:
    """Render a paginated inbox, enforcing consultant scope in SQL queries."""
    normalized_query = (q or "").strip()
    normalized_status = (status or "").strip().lower() or None
    if normalized_status and normalized_status not in ENQUIRY_STATUSES:
        raise HTTPException(status_code=422, detail="Choose a valid enquiry status.")

    selected_assignee = (assigned_to or "").strip().lower()
    if staff.role == "consultant" and selected_assignee:
        raise HTTPException(status_code=403, detail="Consultants can only view their assigned enquiries.")
    assigned_to_id: UUID | None = None
    unassigned = selected_assignee == "unassigned"
    if selected_assignee and not unassigned:
        try:
            assigned_to_id = UUID(selected_assignee)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Choose a valid consultant filter.") from exc
        is_active_consultant = db.scalar(
            select(User.id).where(
                User.id == assigned_to_id,
                User.role == "consultant",
                User.is_active.is_(True),
            )
        )
        if is_active_consultant is None:
            raise HTTPException(status_code=422, detail="Choose an active consultant.")

    total = count_enquiries(
        db,
        staff=staff,
        query=normalized_query,
        status=normalized_status,
        assigned_to_id=assigned_to_id,
        unassigned=unassigned,
    )
    page_count = max(1, math.ceil(total / page_size))
    current_page = min(page, page_count)
    enquiries, _ = list_enquiries(
        db,
        staff=staff,
        query=normalized_query,
        status=normalized_status,
        assigned_to_id=assigned_to_id,
        unassigned=unassigned,
        page=current_page,
        page_size=page_size,
    )

    active_consultants = []
    if staff.role in {"admin", "editor"}:
        active_consultants = list(
            db.scalars(
                select(User).where(User.role == "consultant", User.is_active.is_(True)).order_by(User.name.asc())
            )
        )

    query_values: dict[str, str | int] = {"page_size": page_size}
    if normalized_query:
        query_values["q"] = normalized_query
    if normalized_status:
        query_values["status"] = normalized_status
    if selected_assignee:
        query_values["assigned_to"] = selected_assignee
    previous_url = None
    next_url = None
    if current_page > 1:
        previous_url = "/admin/enquiries?" + urlencode({**query_values, "page": current_page - 1})
    if current_page < page_count:
        next_url = "/admin/enquiries?" + urlencode({**query_values, "page": current_page + 1})

    response = templates.TemplateResponse(
        request=request,
        name="admin/enquiries.html",
        context={
            "page_title": "Enquiry inbox | YatraNest",
            "meta_description": "Private staff enquiry inbox.",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "enquiries": enquiries,
            "query": normalized_query,
            "selected_status": normalized_status or "",
            "selected_assignee": selected_assignee,
            "status_options": ENQUIRY_STATUSES,
            "consultants": active_consultants,
            "total": total,
            "page": current_page,
            "page_count": page_count,
            "page_size": page_size,
            "previous_url": previous_url,
            "next_url": next_url,
            "destination_label": _destination_label,
        },
    )
    return _private_history_response(response)


@router.get(
    "/enquiries/{enquiry_id:uuid}",
    response_class=HTMLResponse,
    name="admin_enquiry_detail",
)
def enquiry_workspace(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Display a private, read-only enquiry workspace within the user's scope."""
    enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    active_consultants = []
    if staff.role == "admin":
        active_consultants = list(
            db.scalars(
                select(User)
                .where(User.role == "consultant", User.is_active.is_(True))
                .order_by(User.name.asc())
            )
        )
    success_messages = {
        "assignment": "Assignment updated.",
        "status": "Enquiry status updated.",
        "note": "Internal note added.",
        "reply_draft": "Reply draft saved.",
        "reply_approved": "Reply approved. It has not been sent.",
        "reply_recorded": "Manual-sent record saved. No message was sent by this application.",
        "ai_reply_draft": "AI-assisted reply saved as an editable draft. It has not been approved or sent.",
        "extraction_reviewed": "Extraction review saved. Original enquiry details remain unchanged.",
        "extraction_retry": "Extraction retry queued. It may take a short time to complete.",
        "extraction_retry_failed": "The retry could not be queued. The enquiry is saved; continue manually and try again later.",
    }
    suggestions = sorted(enquiry.ai_suggestions, key=lambda item: (item.created_at, str(item.id)))
    suggestion = suggestions[-1] if suggestions else None
    accepted_extraction = (
        _typed_extraction(suggestion.accepted_extraction)
        if suggestion and suggestion.extraction_review_status in {"accepted", "corrected"}
        else None
    )
    inconsistent_accepted_payload = bool(
        suggestion
        and suggestion.accepted_extraction is not None
        and (suggestion.extraction_review_status not in {"accepted", "corrected"} or accepted_extraction is None)
    )
    raw_extraction = (
        _typed_extraction(suggestion.extracted_data)
        if (
            suggestion
            and suggestion.generation_status == "completed"
            and not inconsistent_accepted_payload
        )
        else None
    )
    recommendation_extraction = accepted_extraction or raw_extraction
    recommendation_source = "reviewed" if accepted_extraction else "provisional" if raw_extraction else None
    recommendation_result = (
        recommend_packages(db, recommendation_extraction)
        if recommendation_extraction is not None
        else None
    )
    retry_allowed = bool(
        suggestion
        and is_retry_eligible(suggestion, settings=get_settings())
    )
    next_statuses = sorted(STATUS_TRANSITIONS.get(enquiry.status, ()))
    response = templates.TemplateResponse(
        request=request,
        name="admin/enquiry_detail.html",
        context={
            "page_title": f"Enquiry · {enquiry.customer_name or 'Traveller'} | YatraNest",
            "meta_description": "Private enquiry workspace.",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "enquiry": enquiry,
            "destination_label": _destination_label,
            "active_consultants": active_consultants,
            "expected_updated_at": enquiry.updated_at.isoformat(),
            "can_assign": staff.role == "admin",
            "can_update_status": staff.role in {"admin", "editor", "consultant"},
            "can_add_note": staff.role in {"admin", "editor", "consultant"},
            "can_draft_reply": staff.role in {"admin", "editor", "consultant"},
            "ai_reply_drafting_available": ai_reply_drafting_available(get_settings()),
            "ai_draft_request_id": str(uuid4()),
            "ai_reply_failure_message": {
                "unavailable": "AI drafting is unavailable. Continue with the manual reply form.",
                "failed": "AI drafting did not complete. Continue with the manual reply form or retry later.",
            }.get(request.query_params.get("ai_draft", "")),
            "can_approve_reply": staff.role == "admin",
            "max_reply_body_length": 10_000,
            "next_statuses": next_statuses,
            "success_message": success_messages.get(request.query_params.get("updated", "")),
            "ai_suggestion": suggestion,
            "ai_raw_extraction": _extraction_display(suggestion.extracted_data) if raw_extraction else None,
            "ai_accepted_extraction": _extraction_display(suggestion.accepted_extraction) if accepted_extraction else None,
            "ai_has_accepted_extraction": accepted_extraction is not None,
            "ai_inconsistent_accepted_payload": inconsistent_accepted_payload,
            "ai_form_values": _extraction_form_values(suggestion),
            "ai_failure_message": _EXTRACTION_FAILURE_MESSAGES.get(suggestion.failure_code) if suggestion else None,
            "ai_retry_allowed": retry_allowed,
            "ai_retry_waiting": bool(suggestion and suggestion.generation_status == "pending" and not retry_allowed),
            "ai_review_history": [
                {
                    "event": event,
                    "extraction": _extraction_display(event.accepted_extraction),
                }
                for event in sorted(suggestion.extraction_review_events, key=lambda item: item.created_at, reverse=True)
            ] if suggestion else [],
            "recommendation_result": recommendation_result,
            "recommendation_source": recommendation_source,
            "extraction_field_options": (
                "destination travel_start_date travel_end_date travel_year adults children infants "
                "budget_amount budget_currency budget_scope"
            ).split(),
        },
    )
    return _private_history_response(response)


def _run_extraction_task_safely(suggestion_id: UUID) -> None:
    """Contain unexpected worker/claim errors at the in-process task boundary."""
    try:
        process_extraction(suggestion_id)
    except Exception:
        # Do not log worker exception text: it may contain provider or request data.
        return


@router.post("/enquiries/{enquiry_id:uuid}/ai-extraction/retry", name="admin_enquiry_extraction_retry", response_model=None)
def retry_enquiry_extraction(
    enquiry_id: UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    """Queue an eligible retry; the worker performs the authoritative fenced claim."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    scoped_enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if scoped_enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    suggestion = db.scalar(
        select(AISuggestion)
        .where(AISuggestion.enquiry_id == enquiry_id)
        .order_by(AISuggestion.created_at.desc(), AISuggestion.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if suggestion is not None:
        locked_enquiry = db.scalar(
            select(Enquiry)
            .where(Enquiry.id == enquiry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if locked_enquiry is None or (staff.role == "consultant" and locked_enquiry.assigned_to_id != staff.id):
            db.rollback()
            raise HTTPException(status_code=404, detail="Enquiry not found.")
    if suggestion is None or not is_retry_eligible(suggestion, settings=get_settings()):
        return _workflow_error_response(
            request,
            db,
            staff,
            enquiry_id,
            EnquiryWorkflowError("This extraction cannot be retried while it is active or already complete.", 409),
        )

    # Resetting a stale processing attempt clears its token, fencing any late
    # worker. A new task must acquire a fresh token through process_extraction.
    suggestion.generation_status = "pending"
    suggestion.generation_attempt_id = None
    suggestion.failure_code = None
    db.commit()  # Release the lock before scheduling in-process work.

    try:
        background_tasks.add_task(_run_extraction_task_safely, suggestion.id)
    except Exception:
        # The enquiry is already committed. Only fail the row if no newer task
        # has claimed it since this scheduling attempt.
        try:
            db.execute(
                update(AISuggestion)
                .where(
                    AISuggestion.id == suggestion.id,
                    AISuggestion.generation_status == "pending",
                    AISuggestion.generation_attempt_id.is_(None),
                )
                .values(generation_status="failed", failure_code="generation_failed")
            )
            db.commit()
        except Exception:
            db.rollback()
        return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=extraction_retry_failed", status_code=303)
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=extraction_retry", status_code=303)


def _parse_optional_integer(value: str, field_name: str) -> int | None:
    if value == "":
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise AIExtractionReviewError(f"Enter a whole number for {field_name}.") from exc


@router.post("/enquiries/{enquiry_id:uuid}/ai-extraction/review", name="admin_enquiry_extraction_review", response_model=None)
def review_enquiry_extraction(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    destination: Annotated[str, Form(max_length=200)] = "",
    travel_start_date: Annotated[str, Form(max_length=10)] = "",
    travel_end_date: Annotated[str, Form(max_length=10)] = "",
    travel_dates_flexible: Annotated[str, Form()] = "unknown",
    original_travel_period_text: Annotated[str, Form(max_length=300)] = "",
    adults: Annotated[str, Form(max_length=3)] = "",
    children: Annotated[str, Form(max_length=3)] = "",
    infants: Annotated[str, Form(max_length=3)] = "",
    budget_amount: Annotated[str, Form(max_length=13)] = "",
    budget_currency: Annotated[str, Form(max_length=3)] = "",
    budget_scope: Annotated[str, Form()] = "",
    preferences: Annotated[str, Form(max_length=2000)] = "",
    missing_information: Annotated[list[str] | None, Form()] = None,
    ambiguous_information: Annotated[list[str] | None, Form()] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    """Validate staff corrections strictly and append them through the audit service."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    suggestion = db.scalar(
        select(AISuggestion)
        .where(AISuggestion.enquiry_id == enquiry_id)
        .order_by(AISuggestion.created_at.desc(), AISuggestion.id.desc())
        .limit(1)
    )
    if suggestion is None:
        return _workflow_error_response(
            request, db, staff, enquiry_id, EnquiryWorkflowError("No extraction is available to review.", 409)
        )

    try:
        flexible = {"true": True, "false": False, "unknown": None}.get(travel_dates_flexible, "invalid")
        if flexible == "invalid":
            raise AIExtractionReviewError("Choose flexible, exact, or unknown travel dates.")
        scope = budget_scope or None
        payload: dict[str, object] = {
            "schema_version": "1.0",
            "destination": destination.strip() or None,
            "travel_start_date": travel_start_date or None,
            "travel_end_date": travel_end_date or None,
            "travel_dates_flexible": flexible,
            "original_travel_period_text": original_travel_period_text or None,
            "adults": _parse_optional_integer(adults, "adults"),
            "children": _parse_optional_integer(children, "children"),
            "infants": _parse_optional_integer(infants, "infants"),
            "budget_amount": budget_amount or None,
            "budget_currency": budget_currency or None,
            "budget_scope": scope,
            "preferences": [item.strip() for item in preferences.splitlines() if item.strip()],
            "missing_information": missing_information or [],
            "ambiguous_information": ambiguous_information or [],
        }
        extraction = EnquiryExtraction.model_validate_json(json.dumps(payload), strict=True)
        append_extraction_review(db, suggestion.id, actor=staff, accepted_extraction=extraction)
        db.commit()
    except AIExtractionReviewError as exc:
        return _workflow_error_response(
            request, db, staff, enquiry_id, EnquiryWorkflowError(str(exc), exc.status_code)
        )
    except (TypeError, ValueError, InvalidOperation):
        return _workflow_error_response(
            request, db, staff, enquiry_id, EnquiryWorkflowError("Correct the extraction fields and try again.", 422)
        )
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=extraction_reviewed", status_code=303)


@router.post("/enquiries/{enquiry_id:uuid}/replies", name="admin_enquiry_reply_create", response_model=None)
def create_enquiry_reply(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_enquiry_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    body: Annotated[str, Form()] = "",
    supersedes_reply_id: Annotated[str | None, Form(max_length=40)] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        try:
            source_reply_id = UUID(supersedes_reply_id) if supersedes_reply_id else None
        except ValueError as exc:
            raise EnquiryReplyWorkflowError("Choose a valid reply to correct.") from exc
        create_reply_draft(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            body=body,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
            supersedes_reply_id=source_reply_id,
        )
        db.commit()
    except EnquiryReplyWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except ValueError as exc:
        return _workflow_error_response(
            request, db, staff, enquiry_id, EnquiryReplyWorkflowError("Choose a valid reply to correct.")
        )
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=reply_draft", status_code=303)


@router.post(
    "/enquiries/{enquiry_id:uuid}/replies/ai-draft",
    name="admin_enquiry_reply_ai_draft",
    response_model=None,
)
def create_ai_enquiry_reply_draft(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    ai_draft_request_id: Annotated[str | None, Form(min_length=36, max_length=36)] = None,
    expected_enquiry_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    """Generate editable prose and persist it through the ordinary reply draft service."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        try:
            if ai_draft_request_id is None:
                raise ValueError
            request_key = UUID(ai_draft_request_id)
        except (ValueError, TypeError):
            raise AIReplyDraftError("Reload the enquiry before generating a draft.", 409) from None
        reply_id = uuid5(NAMESPACE_URL, f"yatranest-ai-reply:{enquiry_id}:{staff.id}:{request_key}")
        # Check record visibility before looking up an idempotency result.
        if get_enquiry_for_staff(db, enquiry_id, staff) is None:
            raise AIReplyDraftError("Enquiry not found or outside your assigned work.", 404)
        existing = db.scalar(
            select(EnquiryReply).where(
                EnquiryReply.id == reply_id,
                EnquiryReply.enquiry_id == enquiry_id,
                EnquiryReply.author_id == staff.id,
            )
        )
        if existing is not None:
            log_ai_diagnostic(
                logger,
                logging.INFO,
                event="ai_reply_request_replayed",
                stage="idempotency_check",
                provider="groq",
            )
            return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=ai_reply_draft", status_code=303)
        generated = generate_reply_draft_content(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
        )
        create_reply_draft(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            body=generated.body,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
            source_ai_suggestion_id=generated.source.suggestion_id,
            reply_id=reply_id,
            expected_source_suggestion_updated_at=(
                generated.source.suggestion_updated_at.isoformat()
                if generated.source.suggestion_updated_at is not None else None
            ),
        )
        db.commit()
        log_ai_diagnostic(
            logger,
            logging.INFO,
            event="ai_reply_draft_persisted",
            stage="reply_persistence",
            provider=generated.provider,
            model=generated.model,
        )
    except AIReplyDraftError as exc:
        db.rollback()
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_route_failed",
            stage="reply_generation_route",
            provider="groq",
            failure_code="invalid_provider_response" if exc.status_code == 502 and "unusable response" in str(exc) else None,
            exception_type=type(exc).__name__,
        )
        return _workflow_error_response(
            request, db, staff, enquiry_id,
            EnquiryWorkflowError(str(exc), exc.status_code),
        )
    except EnquiryReplyWorkflowError as exc:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_route_failed",
            stage="reply_persistence",
            provider="groq",
            failure_code="validation_failed",
            exception_type=type(exc).__name__,
        )
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception as exc:
        db.rollback()
        log_ai_diagnostic(
            logger,
            logging.ERROR,
            event="ai_reply_route_failed",
            stage="reply_persistence",
            provider="groq",
            failure_code="generation_failed",
            exception_type=type(exc).__name__,
        )
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=ai_reply_draft", status_code=303)


@router.post(
    "/enquiries/{enquiry_id:uuid}/replies/{reply_id:uuid}/edit",
    name="admin_enquiry_reply_edit",
    response_model=None,
)
def edit_enquiry_reply(
    enquiry_id: UUID,
    reply_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_enquiry_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    expected_reply_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    body: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        update_reply_draft(
            db,
            enquiry_id=enquiry_id,
            reply_id=reply_id,
            actor=staff,
            body=body,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
            expected_reply_updated_at=expected_reply_updated_at,
        )
        db.commit()
    except EnquiryReplyWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=reply_draft", status_code=303)


@router.post(
    "/enquiries/{enquiry_id:uuid}/replies/{reply_id:uuid}/approve",
    name="admin_enquiry_reply_approve",
    response_model=None,
)
def approve_enquiry_reply(
    enquiry_id: UUID,
    reply_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_enquiry_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    expected_reply_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        approve_reply(
            db,
            enquiry_id=enquiry_id,
            reply_id=reply_id,
            actor=staff,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
            expected_reply_updated_at=expected_reply_updated_at,
        )
        db.commit()
    except EnquiryReplyWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=reply_approved", status_code=303)


@router.get(
    "/enquiries/{enquiry_id:uuid}/replies/{reply_id:uuid}/manual-sent/confirm",
    response_class=HTMLResponse,
    name="admin_enquiry_reply_manual_sent_confirm",
)
def confirm_enquiry_reply_manual_sent(
    enquiry_id: UUID,
    reply_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    reply = get_reply_for_staff(
        db,
        enquiry_id=enquiry_id,
        reply_id=reply_id,
        actor=staff,
        require_consultant_author=True,
    )
    if reply is None:
        raise HTTPException(status_code=404, detail="Reply not found.")
    if reply.status != "approved":
        return _workflow_error_response(
            request,
            db,
            staff,
            enquiry_id,
            EnquiryReplyWorkflowError("Only an approved reply can be recorded as manually sent.", 409),
        )
    enquiry = get_enquiry_for_staff(db, enquiry_id, staff)
    if enquiry is None:
        raise HTTPException(status_code=404, detail="Enquiry not found.")
    response = templates.TemplateResponse(
        request=request,
        name="admin/enquiry_reply_manual_sent_confirm.html",
        context={
            "page_title": "Confirm manual reply record | YatraNest",
            "meta_description": "Private confirmation for recording a manually sent reply.",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "enquiry": enquiry,
            "reply": reply,
            "expected_enquiry_updated_at": enquiry.updated_at.isoformat(),
            "expected_reply_updated_at": reply.updated_at.isoformat(),
        },
    )
    return _private_history_response(response)


@router.post(
    "/enquiries/{enquiry_id:uuid}/replies/{reply_id:uuid}/manual-sent",
    name="admin_enquiry_reply_manual_sent",
    response_model=None,
)
def record_enquiry_reply_manual_sent(
    enquiry_id: UUID,
    reply_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_enquiry_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    expected_reply_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    confirm_already_sent: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    if confirm_already_sent != "yes":
        return _workflow_error_response(
            request,
            db,
            staff,
            enquiry_id,
            EnquiryReplyWorkflowError("Confirm that you already sent this reply outside the application."),
        )
    try:
        record_reply_manually_sent(
            db,
            enquiry_id=enquiry_id,
            reply_id=reply_id,
            actor=staff,
            expected_enquiry_updated_at=expected_enquiry_updated_at,
            expected_reply_updated_at=expected_reply_updated_at,
        )
        db.commit()
    except EnquiryReplyWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=reply_recorded", status_code=303)


def _workflow_error_response(
    request: Request,
    db: Session,
    staff: User,
    enquiry_id: UUID,
    error: EnquiryWorkflowError,
) -> HTMLResponse:
    db.rollback()
    response = templates.TemplateResponse(
        request=request,
        name="admin/enquiry_action_error.html",
        status_code=error.status_code,
        context={
            "page_title": "Enquiry action needs attention | YatraNest",
            "meta_description": "Private enquiry workflow feedback.",
            "robots": "noindex, nofollow",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "error_message": str(error),
            "enquiry_url": f"/admin/enquiries/{enquiry_id}",
        },
    )
    return _private_history_response(response)


@router.post("/enquiries/{enquiry_id:uuid}/assignment", name="admin_enquiry_assignment", response_model=None)
def update_enquiry_assignment(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    assigned_to_id: Annotated[str, Form(max_length=40)] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    """Admin-only assignment action, protected by CSRF and stale-form checks."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        try:
            target_id = UUID(assigned_to_id) if assigned_to_id else None
        except ValueError as exc:
            raise EnquiryWorkflowError("Choose a valid consultant account.") from exc
        changed = assign_enquiry(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            assignee_id=target_id,
            expected_updated_at=expected_updated_at,
        )
        db.commit()
    except ValueError as exc:
        if isinstance(exc, EnquiryWorkflowError):
            return _workflow_error_response(request, db, staff, enquiry_id, exc)
        db.rollback()
        return _workflow_error_response(
            request, db, staff, enquiry_id, EnquiryWorkflowError("Choose a valid consultant account.")
        )
    except Exception:
        db.rollback()
        raise
    suffix = "?updated=assignment" if changed else ""
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}{suffix}", status_code=303)


@router.post("/enquiries/{enquiry_id:uuid}/status", name="admin_enquiry_status", response_model=None)
def update_enquiry_status(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    new_status: Annotated[str, Form(min_length=1, max_length=30)] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        changed = change_enquiry_status(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            new_status=new_status,
            expected_updated_at=expected_updated_at,
        )
        db.commit()
    except EnquiryWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception:
        db.rollback()
        raise
    suffix = "?updated=status" if changed else ""
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}{suffix}", status_code=303)


@router.post("/enquiries/{enquiry_id:uuid}/notes", name="admin_enquiry_note", response_model=None)
def create_enquiry_note(
    enquiry_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_staff_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str, Form(min_length=1, max_length=64)] = "",
    note_text: Annotated[str, Form(max_length=5000)] = "",
    db: Session = Depends(get_db),
) -> RedirectResponse | HTMLResponse:
    require_valid_csrf(request.state.staff_session, csrf_token)
    try:
        add_enquiry_note(
            db,
            enquiry_id=enquiry_id,
            actor=staff,
            note_text=note_text,
            expected_updated_at=expected_updated_at,
        )
        db.commit()
    except EnquiryWorkflowError as exc:
        return _workflow_error_response(request, db, staff, enquiry_id, exc)
    except Exception:
        db.rollback()
        raise
    return RedirectResponse(f"/admin/enquiries/{enquiry_id}?updated=note", status_code=303)


def _preview_or_conflict(package: Package, pending: PackageDraft | None) -> PackagePreview:
    try:
        if package.status == "draft":
            return preview_from_package(package)
        if package.status == "published" and pending is not None:
            return preview_from_payload(
                pending.draft_payload,
                package_code=package.package_code,
                slug=package.slug,
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409,
            detail="The saved package content cannot be previewed. Open the editor and save valid content again.",
        ) from exc
    raise HTTPException(status_code=409, detail="This package has no private draft content to preview.")


def _review_display(value: object) -> str:
    if value is None or value == "":
        return "Not specified"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return str(value)


def _review_fields(current: PackagePreview | None, proposed: PackagePreview) -> list[dict[str, str]]:
    fields = (
        ("Title", "title"),
        ("Summary", "summary"),
        ("Description", "description"),
        ("Destinations", "destinations"),
        ("Duration", "duration_days"),
        ("Accommodation category", "hotel_category"),
        ("Hotel details", "hotel_details"),
        ("Itinerary", "itinerary"),
        ("Inclusions", "inclusions"),
        ("Map URL", "map_url"),
    )
    return [
        {
            "label": label,
            "current": _review_display(getattr(current, attribute)) if current else "No published content",
            "proposed": _review_display(getattr(proposed, attribute)),
        }
        for label, attribute in fields
    ]


@router.get("/packages/{package_id}/versions", response_class=HTMLResponse, name="admin_package_versions")
def package_version_history(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show immutable snapshots newest first to administrators."""
    package = db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    versions = db.scalars(
        select(PackageVersion)
        .options(selectinload(PackageVersion.created_by))
        .where(PackageVersion.package_id == package_id)
        .order_by(PackageVersion.version_number.desc())
    ).all()
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_versions.html",
        context={
            "page_title": f"Version history · {package.package_code} | YatraNest",
            "meta_description": "Private package version history.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package": package,
            "versions": versions,
        },
    )
    return _private_history_response(response)


@router.get(
    "/packages/{package_id}/versions/{version_id:uuid}",
    response_class=HTMLResponse,
    name="admin_package_version_detail",
)
def package_version_detail(
    package_id: UUID,
    version_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
    restored: Annotated[bool, Query()] = False,
) -> HTMLResponse:
    """Render one package-scoped historical snapshot without changing live data."""
    package = db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    version = db.scalar(
        select(PackageVersion)
        .options(selectinload(PackageVersion.created_by))
        .where(PackageVersion.id == version_id, PackageVersion.package_id == package_id)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Package version not found.")

    fields, field_warnings = snapshot_fields_for_display(version.snapshot)
    prices, price_warnings = snapshot_prices_for_display(version.snapshot)
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_version_detail.html",
        context={
            "page_title": f"Version {version.version_number} · {package.package_code} | YatraNest",
            "meta_description": "Private historical package snapshot.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package": package,
            "version": version,
            "fields": fields,
            "prices": prices,
            "warnings": field_warnings + price_warnings,
            "restored": restored,
        },
    )
    return _private_history_response(response)


def _package_restore_result(
    request: Request,
    staff: User,
    *,
    package_id: UUID,
    version_id: UUID,
    message: str,
    status_code: int,
    errors: dict[str, list[str]] | None = None,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_restore_result.html",
        context={
            "page_title": "Package restore needs attention | YatraNest",
            "meta_description": "Package version restoration result.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package_id": package_id,
            "version_id": version_id,
            "message": message,
            "errors": errors or {},
        },
        status_code=status_code,
    )
    return _private_history_response(response)


@router.get(
    "/packages/{package_id}/versions/{version_id:uuid}/restore",
    response_class=HTMLResponse,
    name="admin_package_version_restore_confirm",
)
def confirm_package_version_restore(
    package_id: UUID,
    version_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Show an explicit review of the state that a restore would apply."""
    package = db.scalar(
        select(Package)
        .options(selectinload(Package.pending_draft))
        .where(Package.id == package_id)
    )
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    version = db.scalar(
        select(PackageVersion)
        .where(PackageVersion.id == version_id, PackageVersion.package_id == package_id)
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Package version not found.")

    message = None
    if package.status == "archived":
        message = "Archived packages cannot be restored."
    elif package.pending_draft is not None:
        message = "Resolve the pending package changes before restoring a version."
    elif package.status not in {"draft", "published"}:
        message = "This package is not in a restorable state."

    fields, field_warnings = snapshot_fields_for_display(version.snapshot)
    fields = [
        field
        for field in fields
        if field["label"]
        not in {
            "Package code at snapshot",
            "URL slug at snapshot",
            "Status at snapshot",
            "Scheduled publication at snapshot",
            "Published at snapshot",
        }
    ]
    prices, price_warnings = snapshot_prices_for_display(version.snapshot)
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_restore_confirm.html",
        context={
            "page_title": f"Restore version {version.version_number} · {package.package_code} | YatraNest",
            "meta_description": "Review a package restoration before applying it.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package": package,
            "version": version,
            "expected_updated_at": package.updated_at.isoformat(),
            "message": message,
            "fields": fields,
            "prices": prices,
            "warnings": field_warnings + price_warnings,
        },
    )
    return _private_history_response(response)


@router.post(
    "/packages/{package_id}/versions/{version_id:uuid}/restore",
    name="admin_package_version_restore",
)
def restore_package_version_route(
    package_id: UUID,
    version_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Atomically restore validated historical content and price rows."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    db.rollback()
    try:
        with db.begin():
            restore_package_version(
                db,
                package_id=package_id,
                version_id=version_id,
                staff=staff,
                expected_updated_at=expected_updated_at,
            )
    except PackageRestoreValidationError as exc:
        return _package_restore_result(
            request,
            staff,
            package_id=package_id,
            version_id=version_id,
            message=exc.message,
            status_code=exc.status_code,
            errors=exc.errors,
        )
    except PackageRestoreError as exc:
        return _package_restore_result(
            request,
            staff,
            package_id=package_id,
            version_id=version_id,
            message=exc.message,
            status_code=exc.status_code,
        )
    except IntegrityError:
        db.rollback()
        return _package_restore_result(
            request,
            staff,
            package_id=package_id,
            version_id=version_id,
            message="Restoration could not be completed because a database constraint was violated. No changes were saved.",
            status_code=409,
        )
    except Exception:
        db.rollback()
        return _package_restore_result(
            request,
            staff,
            package_id=package_id,
            version_id=version_id,
            message="Restoration failed unexpectedly. No changes were saved; reload the package and try again.",
            status_code=500,
        )
    return RedirectResponse(
        url=f"/admin/packages/{package_id}/versions/{version_id}?restored=true",
        status_code=303,
    )


@router.get(
    "/packages/{package_id}/versions/compare",
    response_class=HTMLResponse,
    name="admin_package_version_compare",
)
def compare_package_versions(
    package_id: UUID,
    request: Request,
    from_version_id: Annotated[UUID, Query(alias="from")],
    to_version_id: Annotated[UUID, Query(alias="to")],
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Compare two immutable snapshots belonging to one package."""
    package = db.get(Package, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")

    versions = db.scalars(
        select(PackageVersion)
        .options(selectinload(PackageVersion.created_by))
        .where(
            PackageVersion.package_id == package_id,
            PackageVersion.id.in_({from_version_id, to_version_id}),
        )
    ).all()
    by_id = {version.id: version for version in versions}
    left_version = by_id.get(from_version_id)
    right_version = by_id.get(to_version_id)
    if left_version is None or right_version is None:
        raise HTTPException(status_code=404, detail="Package version not found.")

    field_comparisons, field_warnings = compare_snapshot_fields(left_version.snapshot, right_version.snapshot)
    price_comparisons, price_warnings = compare_snapshot_prices(left_version.snapshot, right_version.snapshot)
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_version_compare.html",
        context={
            "page_title": f"Compare package versions · {package.package_code} | YatraNest",
            "meta_description": "Private comparison of historical package versions.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package": package,
            "left_version": left_version,
            "right_version": right_version,
            "field_comparisons": field_comparisons,
            "price_comparisons": price_comparisons,
            "warnings": field_warnings + price_warnings,
            "identical_versions": left_version.id == right_version.id,
        },
    )
    return _private_history_response(response)


@router.get("/packages/{package_id}/review", response_class=HTMLResponse, name="admin_package_review")
def review_package(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Let admins compare pending content or review a new draft before publishing."""
    package = db.scalar(
        select(Package)
        .options(
            selectinload(Package.prices),
            selectinload(Package.pending_draft),
        )
        .where(Package.id == package_id)
    )
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if package.status == "archived":
        raise HTTPException(status_code=409, detail="Archived packages are read-only.")

    pending = package.pending_draft if package.status == "published" else None
    if package.status == "published" and pending is None:
        current = preview_from_package(package)
        proposed = current
    else:
        current = preview_from_package(package) if package.status == "published" else None
        proposed = _preview_or_conflict(package, pending)
    pending_changed = pending is not None
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_review.html",
        context={
            "page_title": f"Review {package.package_code} | YatraNest",
            "meta_description": "Private package content review.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "package": package,
            "current_content": current,
            "proposed_content": proposed,
            "review_fields": _review_fields(current, proposed),
            "pending_changes": pending_changed,
            "pending_updated_at": pending.updated_at if pending else None,
            "pending_updated_by": pending.updated_by.name if pending and pending.updated_by else None,
            "expected_updated_at": package.updated_at.isoformat(),
            "expected_base_updated_at": pending.base_updated_at.isoformat() if pending else "",
            "expected_draft_updated_at": pending.updated_at.isoformat() if pending else "",
            "live_prices": current.prices if current else (),
            "proposed_prices": proposed.prices,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


def _package_action_error(
    request: Request,
    staff: User,
    *,
    package_id: UUID,
    message: str,
    status_code: int,
    errors: dict[str, list[str]] | None = None,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_action_error.html",
        context={
            "page_title": "Package action needs attention | YatraNest",
            "meta_description": "Package publishing action result.",
            "staff": staff,
            "message": message,
            "package_id": package_id,
            "errors": errors or {},
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@router.post("/packages/{package_id}/publish", name="admin_package_publish")
def publish_package_route(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str | None, Form()] = None,
    expected_base_updated_at: Annotated[str | None, Form()] = None,
    expected_draft_updated_at: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Publish a current draft or atomically apply its pending changes."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    db.rollback()  # End the read transaction opened while resolving staff access.
    try:
        with db.begin():
            publish_package(
                db,
                package_id=package_id,
                staff=staff,
                expected_updated_at=expected_updated_at,
                expected_base_updated_at=expected_base_updated_at,
                expected_draft_updated_at=expected_draft_updated_at,
            )
    except PackageValidationError as exc:
        return _package_action_error(
            request,
            staff,
            package_id=package_id,
            message="Publishing was stopped. Review the package details and prices, then try again.",
            status_code=422,
            errors=exc.errors,
        )
    except PackageWorkflowError as exc:
        return _package_action_error(
            request,
            staff,
            package_id=package_id,
            message=exc.message,
            status_code=exc.status_code,
        )
    except IntegrityError:
        db.rollback()
        return _package_action_error(
            request,
            staff,
            package_id=package_id,
            message="Publishing could not be completed because a database constraint was violated. No changes were saved.",
            status_code=409,
        )
    return RedirectResponse(url="/admin/packages", status_code=303)


@router.post("/packages/{package_id}/unpublish", name="admin_package_unpublish")
def unpublish_package_route(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_admin_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    expected_updated_at: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Return an eligible Published package to Draft without dropping content."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    db.rollback()
    try:
        with db.begin():
            unpublish_package(
                db,
                package_id=package_id,
                staff=staff,
                expected_updated_at=expected_updated_at,
            )
    except PackageWorkflowError as exc:
        return _package_action_error(
            request,
            staff,
            package_id=package_id,
            message=exc.message,
            status_code=exc.status_code,
        )
    except IntegrityError:
        db.rollback()
        return _package_action_error(
            request,
            staff,
            package_id=package_id,
            message="Unpublishing could not be completed because a database constraint was violated. No changes were saved.",
            status_code=409,
        )
    return RedirectResponse(url="/admin/packages", status_code=303)


@router.get("/packages/{package_id}/preview", response_class=HTMLResponse, name="admin_package_preview")
def preview_package(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    """Render unpublished draft content privately using public package styling."""
    package = db.scalar(
        select(Package)
        .options(
            selectinload(Package.prices),
            selectinload(Package.pending_draft),
        )
        .where(Package.id == package_id)
    )
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    presentation = _preview_or_conflict(
        package,
        package.pending_draft if package.status == "published" else None,
    )
    response = templates.TemplateResponse(
        request=request,
        name="admin/package_preview.html",
        context={
            "page_title": f"Preview {package.package_code} | YatraNest",
            "meta_description": "Private package preview for YatraNest staff.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/packages/{package.id}/preview",
            "robots": "noindex, nofollow",
            "package": presentation,
            "package_status": package.status,
            "pending_changes": package.status == "published" and package.pending_draft is not None,
        },
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


@router.post("/packages/{package_id}/duplicate", name="admin_package_duplicate")
def duplicate_package_route(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    csrf_token: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
) -> Response:
    """Create a new draft from any existing package without copying history."""
    require_valid_csrf(request.state.staff_session, csrf_token)
    db.rollback()  # End the read transaction opened while resolving staff access.
    try:
        with db.begin():
            source = db.scalar(
                select(Package)
                .options(selectinload(Package.prices))
                .where(Package.id == package_id)
                .with_for_update()
            )
            if source is None:
                raise HTTPException(status_code=404, detail="Package not found.")
            duplicate = duplicate_package(db, source, staff)
            duplicate_id = duplicate.id
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="A unique package code or URL was claimed at the same time. Please try duplicating again.",
        ) from None
    return RedirectResponse(url=f"/admin/packages/{duplicate_id}/edit", status_code=303)


class _StalePackageEdit(Exception):
    """Internal signal used to render a stale edit without leaking DB details."""


class _StalePublishedPackageEdit(Exception):
    """Published content or its pending edit changed after the form was opened."""


def _list_value(values: dict[str, object], key: str) -> list[object]:
    value = values.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _form_rows(
    values: dict[str, object],
    fields: tuple[str, ...],
    *,
    initial_rows: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    columns = {field: _list_value(values, field) for field in fields}
    count = max((len(column) for column in columns.values()), default=0)
    if count:
        return [
            {field: column[index] if index < len(column) else "" for field, column in columns.items()}
            for index in range(count)
        ]
    return initial_rows or [{field: "" for field in fields}]


def _initial_package_values(package: Package | None) -> dict[str, object]:
    if package is None:
        return {}
    prices = list(package.prices)
    return {
        "package_code": package.package_code,
        "title": package.title,
        "summary": package.summary or "",
        "description": package.description or "",
        "destinations": "\n".join(package.destinations or []),
        "duration_days": str(package.duration_days or ""),
        "hotel_category": package.hotel_category or "",
        "map_url": package.map_url or "",
        "inclusions": "\n".join(package.inclusions or []),
        "hotel_name": [hotel.get("name", "") for hotel in package.hotel_details or []],
        "hotel_location": [hotel.get("location", "") for hotel in package.hotel_details or []],
        "itinerary_day": [str(day.get("day", "")) for day in package.itinerary or []],
        "itinerary_title": [day.get("title", "") for day in package.itinerary or []],
        "itinerary_description": [day.get("description", "") for day in package.itinerary or []],
        "price_season_name": [price.season_name for price in prices],
        "price_start_month": [str(price.start_month) for price in prices],
        "price_end_month": [str(price.end_month) for price in prices],
        "price_per_person": [format(price.price_per_person, ".2f") for price in prices],
        "price_valid_from": [price.valid_from.isoformat() if price.valid_from else "" for price in prices],
        "price_valid_until": [price.valid_until.isoformat() if price.valid_until else "" for price in prices],
        "price_note": [price.price_note or "" for price in prices],
    }


def _pending_payload_form_values(payload: dict[str, object]) -> dict[str, object]:
    """Adapt the validated JSONB snapshot to the existing repeated form fields."""
    prices = payload.get("prices") or []
    hotels = payload.get("hotel_details") or []
    itinerary = payload.get("itinerary") or []
    return {
        "title": payload.get("title", ""),
        "summary": payload.get("summary") or "",
        "description": payload.get("description") or "",
        "destinations": "\n".join(payload.get("destinations") or []),
        "duration_days": str(payload.get("duration_days") or ""),
        "hotel_category": payload.get("hotel_category") or "",
        "map_url": payload.get("map_url") or "",
        "inclusions": "\n".join(payload.get("inclusions") or []),
        "hotel_name": [hotel.get("name", "") for hotel in hotels],
        "hotel_location": [hotel.get("location", "") for hotel in hotels],
        "itinerary_day": [str(day.get("day", "")) for day in itinerary],
        "itinerary_title": [day.get("title", "") for day in itinerary],
        "itinerary_description": [day.get("description", "") for day in itinerary],
        "price_season_name": [price.get("season_name", "") for price in prices],
        "price_start_month": [str(price.get("start_month", "")) for price in prices],
        "price_end_month": [str(price.get("end_month", "")) for price in prices],
        "price_per_person": [str(price.get("price_per_person", "")) for price in prices],
        "price_valid_from": [price.get("valid_from") or "" for price in prices],
        "price_valid_until": [price.get("valid_until") or "" for price in prices],
        "price_note": [price.get("price_note") or "" for price in prices],
    }


def _package_form_response(
    request: Request,
    staff: User,
    *,
    mode: str,
    package_code: str | None = None,
    package_id: UUID | None = None,
    values: dict[str, object] | None = None,
    errors: dict[str, list[str]] | None = None,
    expected_updated_at: str | None = None,
    expected_base_updated_at: str | None = None,
    expected_draft_updated_at: str | None = None,
    pending_changes: bool = False,
    pending_updated_by: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    form_values = values or {}

    response = templates.TemplateResponse(
        request=request,
        name="admin/package_form.html",
        context={
            "page_title": "Create package | YatraNest" if mode == "create" else f"Edit {package_code} | YatraNest",
            "meta_description": "Staff package editor.",
            "staff": staff,
            "csrf_token": request.state.staff_session.csrf_token,
            "mode": mode,
            "package_code": package_code,
            "package_id": package_id,
            "form_values": form_values,
            "errors": errors or {},
            "expected_updated_at": expected_updated_at or "",
            "expected_base_updated_at": expected_base_updated_at or "",
            "expected_draft_updated_at": expected_draft_updated_at or "",
            "pending_changes": pending_changes,
            "pending_updated_by": pending_updated_by,
            "hotel_rows": _form_rows(
                form_values,
                ("hotel_name", "hotel_location"),
            ),
            "itinerary_rows": _form_rows(
                form_values,
                ("itinerary_day", "itinerary_title", "itinerary_description"),
            ),
            "price_rows": _form_rows(
                form_values,
                (
                    "price_season_name",
                    "price_start_month",
                    "price_end_month",
                    "price_per_person",
                    "price_valid_from",
                    "price_valid_until",
                    "price_note",
                ),
            ),
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/packages/new", response_class=HTMLResponse, name="admin_package_new")
def new_package_form(
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
) -> HTMLResponse:
    return _package_form_response(request, staff, mode="create")


@router.post("/packages/new", name="admin_package_create")
def create_package_route(
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    form: Annotated[dict[str, object], Depends(get_package_form_data)],
    db: Session = Depends(get_db),
) -> Response:
    require_valid_csrf(request.state.staff_session, form.get("csrf_token"))
    values = {key: value for key, value in form.items() if key != "expected_updated_at"}
    db.rollback()  # End the read transaction opened while resolving the staff page dependency.
    try:
        with db.begin():
            payload = parse_package_payload(values)
            if package_code_exists(db, payload.package_code):
                raise PackageValidationError(
                    {"package_code": ["That package code is already in use."]},
                    values,
                )
            package = create_draft_package(db, payload, staff)
            new_package_id = package.id
    except PackageValidationError as exc:
        return _package_form_response(
            request,
            staff,
            mode="create",
            values=exc.values,
            errors=exc.errors,
            status_code=422,
        )
    except IntegrityError:
        db.rollback()
        return _package_form_response(
            request,
            staff,
            mode="create",
            values=values,
            errors={"package_code": ["The package code or generated slug was just taken. Review and try again."]},
            status_code=409,
        )
    return RedirectResponse(url=f"/admin/packages/{new_package_id}/edit", status_code=303)


@router.get("/packages/{package_id}/edit", response_class=HTMLResponse, name="admin_package_edit")
def edit_package_form(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    db: Session = Depends(get_db),
) -> HTMLResponse:
    package = db.scalar(
        select(Package)
        .options(
            selectinload(Package.prices),
            selectinload(Package.pending_draft).selectinload(PackageDraft.updated_by),
        )
        .where(Package.id == package_id)
    )
    if package is None:
        raise HTTPException(status_code=404, detail="Package not found.")
    if package.status == "archived":
        raise HTTPException(status_code=409, detail="Archived packages are read-only. Duplicate one to create a draft.")
    if package.status == "draft":
        return _package_form_response(
            request,
            staff,
            mode="edit",
            package_code=package.package_code,
            package_id=package.id,
            values=_initial_package_values(package),
            expected_updated_at=package.updated_at.isoformat(),
        )
    if package.status != "published":
        raise HTTPException(status_code=409, detail="This package cannot be edited in this phase.")

    pending = package.pending_draft
    form_values = (
        _pending_payload_form_values(pending.draft_payload)
        if pending is not None
        else _initial_package_values(package)
    )
    return _package_form_response(
        request,
        staff,
        mode="published_edit",
        package_code=package.package_code,
        package_id=package.id,
        values=form_values,
        expected_base_updated_at=(pending.base_updated_at if pending else package.updated_at).isoformat(),
        expected_draft_updated_at=pending.updated_at.isoformat() if pending else "",
        pending_changes=pending is not None,
        pending_updated_by=pending.updated_by.name if pending and pending.updated_by else None,
    )


@router.post("/packages/{package_id}/edit", name="admin_package_update")
def edit_package_route(
    package_id: UUID,
    request: Request,
    staff: Annotated[User, Depends(get_editor_page_user)],
    form: Annotated[dict[str, object], Depends(get_package_form_data)],
    db: Session = Depends(get_db),
) -> Response:
    require_valid_csrf(request.state.staff_session, form.get("csrf_token"))
    concurrency_fields = {
        "expected_updated_at",
        "expected_base_updated_at",
        "expected_draft_updated_at",
    }
    values = {key: value for key, value in form.items() if key not in concurrency_fields}
    expected_updated_at = form.get("expected_updated_at")
    expected_base_updated_at = form.get("expected_base_updated_at")
    expected_draft_updated_at = form.get("expected_draft_updated_at")
    db.rollback()
    package_code = ""
    published_edit = False
    try:
        with db.begin():
            package = db.scalar(
                select(Package)
                .options(selectinload(Package.prices))
                .where(Package.id == package_id)
                .with_for_update()
            )
            if package is None:
                raise HTTPException(status_code=404, detail="Package not found.")
            package_code = package.package_code
            if package.status == "archived":
                raise HTTPException(
                    status_code=409,
                    detail="Archived packages are read-only. Duplicate one to create a draft.",
                )
            published_edit = package.status == "published"
            if package.status == "draft":
                try:
                    expected_timestamp = datetime.fromisoformat(str(expected_updated_at))
                except (TypeError, ValueError):
                    raise _StalePackageEdit from None
                if expected_timestamp.tzinfo is None or expected_timestamp != package.updated_at:
                    raise _StalePackageEdit

                payload = parse_package_payload(
                    values,
                    existing_package_code=package.package_code,
                    existing_slug=package.slug,
                )
                update_draft_package(db, package, payload, staff)
            elif published_edit:
                pending = db.scalar(
                    select(PackageDraft)
                    .where(PackageDraft.package_id == package.id)
                    .with_for_update()
                )
                try:
                    expected_base = datetime.fromisoformat(str(expected_base_updated_at))
                    expected_pending = (
                        datetime.fromisoformat(str(expected_draft_updated_at))
                        if expected_draft_updated_at
                        else None
                    )
                except (TypeError, ValueError):
                    raise _StalePublishedPackageEdit from None

                if expected_base.tzinfo is None or expected_base != package.updated_at:
                    raise _StalePublishedPackageEdit
                if pending is None:
                    if expected_pending is not None:
                        raise _StalePublishedPackageEdit
                elif (
                    expected_pending is None
                    or expected_pending.tzinfo is None
                    or pending.base_updated_at != package.updated_at
                    or pending.base_updated_at != expected_base
                    or pending.updated_at != expected_pending
                ):
                    raise _StalePublishedPackageEdit

                payload = parse_package_payload(
                    values,
                    existing_package_code=package.package_code,
                    existing_slug=package.slug,
                )
                save_pending_package_draft(db, package, payload, staff, existing_draft=pending)
            else:
                raise HTTPException(
                    status_code=409,
                    detail="This package cannot be edited in this phase.",
                )
    except PackageValidationError as exc:
        return _package_form_response(
            request,
            staff,
            mode="published_edit" if published_edit else "edit",
            package_code=package_code,
            package_id=package_id,
            values=exc.values,
            errors=exc.errors,
            expected_updated_at=str(expected_updated_at or ""),
            expected_base_updated_at=str(expected_base_updated_at or ""),
            expected_draft_updated_at=str(expected_draft_updated_at or ""),
            pending_changes=bool(expected_draft_updated_at),
            status_code=422,
        )
    except _StalePackageEdit:
        return _package_form_response(
            request,
            staff,
            mode="edit",
            package_code=package_code,
            package_id=package_id,
            values=values,
            errors={"form": ["This draft changed after you opened it. Reload the edit page before saving again."]},
            expected_updated_at=str(expected_updated_at or ""),
            status_code=409,
        )
    except _StalePublishedPackageEdit:
        return _package_form_response(
            request,
            staff,
            mode="published_edit",
            package_code=package_code,
            package_id=package_id,
            values=values,
            errors={"form": ["The published package or its pending changes changed after you opened this page. Reload before saving again."]},
            expected_base_updated_at=str(expected_base_updated_at or ""),
            expected_draft_updated_at=str(expected_draft_updated_at or ""),
            pending_changes=bool(expected_draft_updated_at),
            status_code=409,
        )
    except IntegrityError:
        db.rollback()
        return _package_form_response(
            request,
            staff,
            mode="published_edit" if published_edit else "edit",
            package_code=package_code,
            package_id=package_id,
            values=values,
            errors={"form": ["The draft could not be saved because a database constraint was violated."]},
            expected_updated_at=str(expected_updated_at or ""),
            expected_base_updated_at=str(expected_base_updated_at or ""),
            expected_draft_updated_at=str(expected_draft_updated_at or ""),
            pending_changes=bool(expected_draft_updated_at),
            status_code=409,
        )
    return RedirectResponse(url="/admin/packages", status_code=303)
