"""Persisted lifecycle helpers for asynchronous, human-reviewed extraction.

No function in this module commits caller-owned request work. Worker operations
use their own short transactions and never hold a transaction during provider IO.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import re
from typing import Callable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.models import AISuggestion, Enquiry
from app.schemas.ai_extraction import EnquiryExtraction, extraction_to_json
from app.services.ai.base import GenerationRequest, GenerationResponse, validate_structured_output
from app.services.ai.errors import AIErrorCode, AIProviderError


_EMAIL_PATTERN = re.compile(r"(?<![\w.+-])[\w.!#$%&'*+/=?^`{|}~-]+@[\w-]+(?:\.[\w-]+)+(?![\w.-])")
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+\s*)?\d[\d().\s-]{5,}\d(?!\w)")
_DATE_LIKE_PATTERN = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}")
_LABELED_PHONE_PATTERN = re.compile(
    r"(?i)(?P<label>\b(?:phone|mobile|whatsapp|telephone|tel|call)\s*(?:number|no\.?)?\s*[:#-]?\s*)"
    r"(?P<number>\+?\d[\d().\s-]{5,}\d)"
)

_FAILURE_CODE_MAP = {
    AIErrorCode.PROVIDER_UNAVAILABLE: ("unavailable", "provider_unavailable"),
    AIErrorCode.PROVIDER_TIMEOUT: ("failed", "provider_timeout"),
    AIErrorCode.INVALID_PROVIDER_RESPONSE: ("failed", "invalid_provider_response"),
    AIErrorCode.VALIDATION_FAILED: ("failed", "validation_failed"),
}
_SAFE_FAILURE_CODES = frozenset(
    {"provider_unavailable", "provider_timeout", "invalid_provider_response", "validation_failed", "generation_failed"}
)


@dataclass(frozen=True)
class ClaimedExtraction:
    """Claim token plus minimized provider input, returned after claim commit."""

    suggestion_id: UUID
    attempt_id: UUID
    redacted_message: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _configured_groq(settings: Settings) -> bool:
    """Only the approved active provider is considered available in this phase."""
    if not settings.ai_enabled or settings.ai_provider != "groq":
        return False
    key = settings.groq_api_key.get_secret_value().strip() if settings.groq_api_key else ""
    model = settings.groq_model.strip() if settings.groq_model else ""
    return bool(key and model)


def ensure_initial_suggestion(
    session: Session,
    enquiry: Enquiry,
    *,
    settings: Settings | None = None,
) -> AISuggestion:
    """Return the enquiry's initial suggestion, creating it once under parent lock.

    The caller owns the transaction. A pending enquiry may be flushed by itself
    so it can be locked; unrelated pending session changes are not flushed here.
    Existing suggestions, including legacy rows with NULL lifecycle fields, are
    returned without modification.
    """
    configured = settings or get_settings()
    if enquiry.id is None:
        session.add(enquiry)
        session.flush([enquiry])

    with session.no_autoflush:
        locked_id = session.scalar(
            select(Enquiry.id)
            .where(Enquiry.id == enquiry.id)
            .with_for_update()
        )
    if locked_id is None:
        raise ValueError("Enquiry must exist in the caller's transaction.")

    with session.no_autoflush:
        existing_id = session.scalar(
            select(AISuggestion.id)
            .where(AISuggestion.enquiry_id == enquiry.id)
            .order_by(AISuggestion.created_at, AISuggestion.id)
            .limit(1)
            .with_for_update()
        )
    if existing_id is not None:
        existing = session.get(AISuggestion, existing_id)
        if existing is None:  # Defensive against unusual concurrent deletion.
            raise RuntimeError("Locked AI suggestion disappeared.")
        return existing

    available = _configured_groq(configured)
    suggestion = AISuggestion(
        enquiry_id=enquiry.id,
        generation_status="pending" if available else "unavailable",
        failure_code=None if available else "provider_unavailable",
    )
    session.add(suggestion)
    session.flush([suggestion])
    return suggestion


def redact_contact_patterns(message: str) -> str:
    """Redact recognizable emails and phones without changing stored source text.

    Date-shaped numeric strings and currency/budget amounts are excluded from
    phone matching. Plain ten-digit Indian mobile numbers and explicitly
    labelled phone values are redacted as well as formatted international ones.
    """
    redacted = _EMAIL_PATTERN.sub("[email redacted]", message)

    def redact_labeled(match: re.Match[str]) -> str:
        number = match.group("number")
        digit_count = sum(char.isdigit() for char in number)
        if 7 <= digit_count <= 15:
            return f"{match.group('label')}[phone redacted]"
        return match.group(0)

    redacted = _LABELED_PHONE_PATTERN.sub(redact_labeled, redacted)

    def redact_phone(match: re.Match[str]) -> str:
        number = match.group(0).strip()
        digits = "".join(char for char in number if char.isdigit())
        if not 7 <= len(digits) <= 15 or _DATE_LIKE_PATTERN.fullmatch(number):
            return match.group(0)
        prefix = redacted[max(0, match.start() - 16):match.start()]
        if re.search(r"(?i)(?:₹|\bINR\s*|\bRs\.?\s*|\bbudget\s*(?:of|:)?\s*)$", prefix):
            return match.group(0)
        # Do not redact unlabelled plain numbers except a recognizable Indian
        # ten-digit mobile number; this avoids eating ordinary budgets/counts.
        if number.isdigit() and not (len(digits) == 10 and digits[0] in "6789"):
            return match.group(0)
        return "[phone redacted]"

    return _PHONE_PATTERN.sub(redact_phone, redacted)


def _generation_request(redacted_message: str) -> GenerationRequest:
    prompt = (
        "Extract only information stated in the customer message. Return exactly one JSON object "
        "with every key shown in the contract below and no additional keys. Groq JSON mode "
        "guarantees JSON syntax only; this explicit contract is required. Do not guess, infer, "
        "repair, normalize into a more specific value, or calculate missing facts.\n\n"
        "EXACT CONTRACT (all keys must be present):\n"
        '{"schema_version":"1.0","destination":string_1_to_200_chars|null,'
        '"travel_start_date":"YYYY-MM-DD"|null,"travel_end_date":"YYYY-MM-DD"|null,'
        '"travel_dates_flexible":true|false|null,"original_travel_period_text":string_up_to_300_chars|null,'
        '"adults":integer_0_to_100|null,"children":integer_0_to_100|null,'
        '"infants":integer_0_to_100|null,"budget_amount":decimal_string|null,'
        '"budget_currency":ISO_4217_alpha3|null,"budget_scope":"total"|"per_person"|null,'
        '"preferences":[string_1_to_200_chars,...]_max_30,'
        '"missing_information":[field_name,...]_max_30,'
        '"ambiguous_information":[field_name,...]_max_30}\n'
        'Allowed missing_information and ambiguous_information names: '
        '"destination", "travel_start_date", "travel_end_date", "travel_year", '
        '"travel_dates_flexible", "adults", "children", "infants", "budget_amount", '
        '"budget_currency", "budget_scope".\n'
        "Currency must be an uppercase active ISO 4217 alphabetic three-letter code from the "
        "application-supported currency set, or null. Budget amount must be from 0 through "
        "9999999999.99 inclusive with no more than two decimal places. Unknown scalar values "
        "must be JSON null, not the strings 'unknown', 'n/a', or an empty string. A missing "
        "field was not supplied; an ambiguous "
        "field was supplied but its meaning cannot be resolved. For every null destination, "
        "traveller count, date bound, or budget amount, identify the relevant field as missing "
        "or ambiguous. When both exact travel dates are unknown, list both date fields. For a "
        "partial range, list the unknown bound. Preserve the stated period wording in "
        "original_travel_period_text. A month without an explicitly stated year requires "
        '"travel_year" in ambiguous_information and both dates null. Do not turn a month or '
        "month-and-year into exact dates. Set travel_dates_flexible only when flexibility is "
        "explicitly stated; otherwise use null and mark it missing or ambiguous as appropriate.\n"
        "Adults, children and infants are separate counts. Do not infer zero for an unmentioned "
        "group; use null and mark it missing. Counts must be integers from 0 to 100. A budget "
        "amount must be a decimal string encoded as a JSON string such as \"70000.00\", never "
        "a JSON number; use null if "
        "unknown. Use an ISO 4217 alpha-3 currency only when explicit (₹ means INR); never "
        "assume INR or convert currencies. Budget scope must be total or per_person only when "
        "explicit; otherwise use null and mark budget_scope ambiguous or missing. If all three "
        "traveller counts are known, their combined total must be greater than zero. Use empty "
        "arrays when there are no preferences or no ambiguous fields.\n\n"
        "REFERENCE EXAMPLE (synthetic; use only to understand the contract, not as input facts):\n"
        'Message: "Hi, I\'m planning a 6-day trip to Munnar, Thekkady and Alleppey in February '
        "2027 for two adults. We prefer a comfortable 4-star accommodation and would like to "
        "explore tea plantations, nature and the Alleppey backwaters. Our budget is around "
        '₹70,000 per person."\n'
        "Correct shape for that message (exact dates and children/infants were not stated):\n"
        '{"schema_version":"1.0","destination":"Munnar, Thekkady and Alleppey",'
        '"travel_start_date":null,"travel_end_date":null,"travel_dates_flexible":null,'
        '"original_travel_period_text":"February 2027","adults":2,"children":null,'
        '"infants":null,"budget_amount":"70000.00","budget_currency":"INR",'
        '"budget_scope":"per_person","preferences":["4-star accommodation",'
        '"tea plantations","nature","Alleppey backwaters"],'
        '"missing_information":["travel_start_date","travel_end_date",'
        '"travel_dates_flexible","children",'
        '"infants"],"ambiguous_information":[]}\n\n'
        "Do not recommend packages, calculate prices, or draft a reply.\n\n"
        f"Customer message (contact details redacted):\n{redacted_message}"
    )
    return GenerationRequest(
        prompt=prompt,
        response_schema=EnquiryExtraction.model_json_schema(),
        schema_name="enquiry_extraction_v1",
        max_output_tokens=1200,
        output_model=EnquiryExtraction,
    )


def _worker_session_factory(session_factory: sessionmaker | None):
    if session_factory is not None:
        return session_factory
    from app.database import SessionLocal

    return SessionLocal


def _claimable(suggestion: AISuggestion, *, now: datetime, settings: Settings) -> bool:
    if suggestion.generation_status in {"pending", "failed", "unavailable"}:
        return True
    if suggestion.generation_status != "processing" or suggestion.updated_at is None:
        return False
    return suggestion.updated_at <= now - processing_stale_after(settings)


def is_retry_eligible(
    suggestion: AISuggestion,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> bool:
    """Whether staff may enqueue this lifecycle without duplicating active work.

    Failed/unavailable work can retry immediately. Pending work is only exposed
    for recovery after the normal stale threshold, and processing must be stale.
    The worker still performs the authoritative locked claim before provider IO.
    """
    configured = settings or get_settings()
    current_time = now or _utcnow()
    if suggestion.extraction_review_status in {"accepted", "corrected"}:
        return False
    if suggestion.generation_status == "pending":
        return suggestion.updated_at is not None and suggestion.updated_at <= current_time - processing_stale_after(configured)
    return _claimable(suggestion, now=current_time, settings=configured)


def processing_stale_after(settings: Settings) -> timedelta:
    """Use twice the configured worst-case call budget plus a scheduling grace.

    Groq is the active provider. The calculation also accounts for the existing
    orchestrator's possible Gemini-to-Groq fallback if configuration changes in
    a later deployment. The 2x margin avoids reclaiming a slow but live request;
    60 seconds allow for task scheduling and database/clock jitter.
    """
    retries = settings.ai_max_retries
    attempts = retries + 1
    backoff = sum(min(0.25 * (2**attempt), 1.0) for attempt in range(retries))
    provider_count = 2 if settings.ai_provider == "gemini" and settings.ai_fallback_enabled else 1
    call_budget = provider_count * (attempts * settings.ai_timeout_seconds + backoff)
    return timedelta(seconds=(2 * call_budget) + 60)


def claim_extraction_work(
    suggestion_id: UUID,
    *,
    session_factory: sessionmaker | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> ClaimedExtraction | None:
    """Lock and claim eligible work; commit only this worker-owned short transaction."""
    configured = settings or get_settings()
    factory = _worker_session_factory(session_factory)
    current_time = now or _utcnow()
    claimed: ClaimedExtraction | None = None

    with factory() as session:
        with session.begin():
            suggestion = session.scalar(
                select(AISuggestion)
                .where(AISuggestion.id == suggestion_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if suggestion is None or not _claimable(suggestion, now=current_time, settings=configured):
                return None
            if suggestion.extraction_review_status in {"accepted", "corrected"}:
                return None
            enquiry_message = session.scalar(
                select(Enquiry.original_message).where(Enquiry.id == suggestion.enquiry_id)
            )
            if enquiry_message is None:
                return None

            attempt_id = uuid4()
            suggestion.generation_status = "processing"
            suggestion.generation_attempt_id = attempt_id
            suggestion.failure_code = None
            session.flush()
            claimed = ClaimedExtraction(
                suggestion_id=suggestion.id,
                attempt_id=attempt_id,
                redacted_message=redact_contact_patterns(enquiry_message),
            )
    return claimed


def _failure_result(error: Exception) -> tuple[str, str]:
    if isinstance(error, AIProviderError):
        code = _FAILURE_CODE_MAP.get(error.code)
        if code is not None:
            return code
    return "failed", "generation_failed"


def _persist_failure(
    suggestion_id: UUID,
    attempt_id: UUID,
    *,
    status: str,
    failure_code: str,
    session_factory: sessionmaker,
) -> bool:
    if status not in {"failed", "unavailable"} or failure_code not in _SAFE_FAILURE_CODES:
        raise ValueError("Unsupported extraction failure state.")
    with session_factory() as session:
        with session.begin():
            suggestion = session.scalar(
                select(AISuggestion)
                .where(AISuggestion.id == suggestion_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                suggestion is None
                or suggestion.generation_status != "processing"
                or suggestion.generation_attempt_id != attempt_id
            ):
                return False
            suggestion.generation_status = status
            suggestion.generation_attempt_id = None
            suggestion.failure_code = failure_code
            session.flush()
    return True


def _validated_extraction(response: GenerationResponse) -> EnquiryExtraction:
    if not isinstance(response, GenerationResponse) or not isinstance(response.content, str):
        raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
    if (
        response.provider != "groq"
        or not response.model
        or len(response.model) > 120
    ):
        raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
    try:
        # Re-parse even orchestrator-validated output at the persistence boundary.
        extraction = validate_structured_output(response.content, EnquiryExtraction)
    except AIProviderError:
        raise
    if not isinstance(extraction, EnquiryExtraction):
        raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
    return extraction


def _persist_success(
    suggestion_id: UUID,
    attempt_id: UUID,
    response: GenerationResponse,
    extraction: EnquiryExtraction,
    *,
    session_factory: sessionmaker,
) -> bool:
    if (
        response.provider != "groq"
        or len(response.provider) > 80
        or not response.model
        or len(response.model) > 120
    ):
        raise AIProviderError(AIErrorCode.INVALID_PROVIDER_RESPONSE)
    canonical = extraction_to_json(extraction)
    with session_factory() as session:
        with session.begin():
            suggestion = session.scalar(
                select(AISuggestion)
                .where(AISuggestion.id == suggestion_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                suggestion is None
                or suggestion.generation_status != "processing"
                or suggestion.generation_attempt_id != attempt_id
            ):
                return False
            suggestion.extracted_data = canonical
            suggestion.provider = response.provider
            suggestion.model_name = response.model
            suggestion.generated_at = response.generated_at
            suggestion.generation_status = "completed"
            suggestion.generation_attempt_id = None
            suggestion.failure_code = None
            session.flush()
    return True


def _default_generator(request: GenerationRequest, settings: Settings) -> GenerationResponse:
    from app.services.ai.orchestrator import generate

    return generate(request, settings=settings)


def process_extraction(
    suggestion_id: UUID,
    *,
    session_factory: sessionmaker | None = None,
    settings: Settings | None = None,
    generator: Callable[[GenerationRequest, Settings], GenerationResponse] | None = None,
) -> bool:
    """Claim, generate outside transactions, and persist only the fenced result.

    Returns True only when a current attempt reaches a persisted terminal state.
    Provider and validation errors are reduced to the approved safe failure codes.
    """
    configured = settings or get_settings()
    factory = _worker_session_factory(session_factory)
    claim = claim_extraction_work(
        suggestion_id,
        session_factory=factory,
        settings=configured,
    )
    if claim is None:
        return False

    try:
        if not _configured_groq(configured):
            raise AIProviderError(AIErrorCode.PROVIDER_UNAVAILABLE)
        request = _generation_request(claim.redacted_message)
        response = (generator or _default_generator)(request, configured)
        extraction = _validated_extraction(response)
        try:
            return _persist_success(
                suggestion_id,
                claim.attempt_id,
                response,
                extraction,
                session_factory=factory,
            )
        except AIProviderError:
            raise
        except Exception:
            # Persistence exceptions must roll back; no sensitive exception text
            # is logged or surfaced from a background task.
            return False
    except Exception as error:
        status, failure_code = _failure_result(error)
        try:
            return _persist_failure(
                suggestion_id,
                claim.attempt_id,
                status=status,
                failure_code=failure_code,
                session_factory=factory,
            )
        except Exception:
            return False
