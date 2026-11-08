"""AI-assisted reply prose with database-owned recommendations and prices.

Provider calls are synchronous and bounded by the existing AI timeout/retry
configuration. This service never approves or sends a reply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import logging
import re
import time
from typing import Callable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import AISuggestion, Enquiry, Package, PackagePrice, User
from app.schemas.ai_extraction import EnquiryExtraction
from app.schemas.ai_reply import AIReplyContent
from app.services.ai.base import GenerationRequest, GenerationResponse
from app.services.ai.errors import AIErrorCode, AIProviderError
from app.services.ai.diagnostics import log_ai_diagnostic
from app.services.ai.orchestrator import generate
from app.services.ai_extraction_lifecycle import redact_contact_patterns
from app.services.package_recommendations import RecommendationResult, recommend_packages


_SAFE_FAILURE_MESSAGES = {
    "provider_unavailable": "AI drafting is unavailable. Use the manual reply form.",
    "provider_timeout": "The drafting service timed out. Use the manual reply form or try again later.",
    "provider_rate_limited": "The drafting service is busy. Use the manual reply form or try again later.",
    "provider_authentication_failed": "The drafting service is not configured correctly. Contact an administrator.",
    "provider_invalid_request": "The drafting request could not be processed. Use the manual reply form.",
    "invalid_provider_response": "The drafting service returned an unusable response. Use the manual reply form.",
    "validation_failed": "The generated draft did not pass validation. Use the manual reply form.",
    "generation_failed": "A draft could not be generated. Use the manual reply form.",
    "content_refused": "The drafting service could not produce a suitable draft. Use the manual reply form.",
}

_UNSAFE_PROSE = re.compile(
    r"\b(?:price|pricing|rate|fare|discount|gst|tax|"
    r"quote|quotation|available|availability|guarantee|guaranteed|booking|booked|complimentary|"
    r"included|includes|hotel|flight|itinerary)\b",
    re.IGNORECASE,
)
_RESTRICTED_MATCH_PATTERNS = (
    ("numeric_or_currency_marker", "numeric_or_currency", re.compile(r"(?:\d|₹|\bINR\b|\bRs\.?\s*\d)", re.IGNORECASE)),
    ("price", "pricing_claim", re.compile(r"\bprice\b", re.IGNORECASE)),
    ("pricing", "pricing_claim", re.compile(r"\bpricing\b", re.IGNORECASE)),
    ("rate", "pricing_claim", re.compile(r"\brate\b", re.IGNORECASE)),
    ("fare", "pricing_claim", re.compile(r"\bfare\b", re.IGNORECASE)),
    ("discount", "pricing_claim", re.compile(r"\bdiscount\b", re.IGNORECASE)),
    ("gst", "tax_claim", re.compile(r"\bgst\b", re.IGNORECASE)),
    ("tax", "tax_claim", re.compile(r"\btax\b", re.IGNORECASE)),
    ("quote", "pricing_claim", re.compile(r"\bquote\b", re.IGNORECASE)),
    ("quotation", "pricing_claim", re.compile(r"\bquotation\b", re.IGNORECASE)),
    ("available", "commercial_commitment", re.compile(r"\bavailable\b", re.IGNORECASE)),
    ("availability", "commercial_commitment", re.compile(r"\bavailability\b", re.IGNORECASE)),
    ("guarantee", "commercial_commitment", re.compile(r"\bguarantee\b", re.IGNORECASE)),
    ("guaranteed", "commercial_commitment", re.compile(r"\bguaranteed\b", re.IGNORECASE)),
    ("booking", "commercial_commitment", re.compile(r"\bbooking\b", re.IGNORECASE)),
    ("booked", "commercial_commitment", re.compile(r"\bbooked\b", re.IGNORECASE)),
    ("complimentary", "commercial_commitment", re.compile(r"\bcomplimentary\b", re.IGNORECASE)),
    ("included", "unverified_feature", re.compile(r"\bincluded\b", re.IGNORECASE)),
    ("includes", "unverified_feature", re.compile(r"\bincludes\b", re.IGNORECASE)),
    ("hotel", "unverified_feature", re.compile(r"\bhotel\b", re.IGNORECASE)),
    ("flight", "unverified_feature", re.compile(r"\bflight\b", re.IGNORECASE)),
    ("itinerary", "unverified_feature", re.compile(r"\bitinerary\b", re.IGNORECASE)),
)
_UNSAFE_FREE_OFFER = re.compile(
    r"\bfree\s+(?:of\s+charge|cancellation|upgrade|night|stay|tour|transfer|airport\s+transfer|meal|ticket|activity|room|booking|package)\b",
    re.IGNORECASE,
)
logger = logging.getLogger(__name__)

_NUMBER_TOKEN = re.compile(r"(?<![A-Za-z])\d+(?:,\d{3})*(?:\.\d+)?(?![A-Za-z])")
_MONEY_AMOUNT = re.compile(
    r"(?P<prefix>₹|\bINR\b|\bRs\.?\s*)\s*"
    r"(?P<amount>\d+(?:,\d{3})*(?:\.\d{1,2})?)"
    r"(?:\s*(?P<suffix>\bINR\b|\bRs\.?\b))?",
    re.IGNORECASE,
)
_TRAVELLER_COUNT = re.compile(
    r"(?P<count>\d{1,3})\s+(?P<label>adults?|children|child|kids?|infants?)\b",
    re.IGNORECASE,
)
_MONTH_DATE = re.compile(
    r"\b(?:(?P<month_first>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
    r"(?P<day_first>\d{1,2})(?:st|nd|rd|th)?[,]?\s+(?P<year_first>\d{4})|"
    r"(?P<day_second>\d{1,2})(?:st|nd|rd|th)?\s+(?P<month_second>jan(?:uary)?|feb(?:ruary)?|"
    r"mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)[,]?\s+(?P<year_second>\d{4}))\b",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b")
_NUMERIC_DATE = re.compile(r"\b(?P<first>\d{1,2})[/-](?P<second>\d{1,2})[/-](?P<year>\d{4})\b")
_ANY_CURRENCY_MARKER = re.compile(r"₹|\$|€|£|¥|\b(?:INR|USD|EUR|GBP|JPY|Rs\.?)\b", re.IGNORECASE)
_BUDGET_CLARIFICATION = re.compile(
    r"\b(?:clarif\w*|confirm\w*|mean|meant|whether|which|refer\s+to|intend\w*)\b",
    re.IGNORECASE,
)
_BUDGET_SCOPE_WORDING = re.compile(r"\b(?:budget|per\s+person|per-person|total|overall)\b", re.IGNORECASE)
_BUDGET_OWNER_WORDING = re.compile(
    r"\b(?:your\s+(?:submitted\s+)?budget|you\s+(?:mentioned|wrote|submitted)|"
    r"the\s+form\s+(?:lists|records|shows))\b",
    re.IGNORECASE,
)
_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass(frozen=True)
class TrustedReplyNumbers:
    """Customer-submitted or staff-accepted numeric facts allowed in prose."""

    travel_dates: frozenset[date] = frozenset()
    adults: int | None = None
    children: int | None = None
    infants: int | None = None
    budget_amounts: frozenset[Decimal] = frozenset()
    budget_currency: str | None = None
    structured_values: frozenset[str] = frozenset()
    accepted_values: frozenset[str] = frozenset()
    original_message_values: frozenset[str] = frozenset()


def _decimal_amount(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        result = Decimal(str(value).replace(",", "")).quantize(Decimal("0.01"))
    except Exception:
        return None
    return result if result.is_finite() and result >= 0 else None


def _original_message_budget_amounts(message: str) -> set[Decimal]:
    """Recognize only explicit, currency-marked customer budget amounts."""
    safe_message = redact_contact_patterns(message)
    amounts: set[Decimal] = set()
    for match in _MONEY_AMOUNT.finditer(safe_message):
        context = safe_message[max(0, match.start() - 48): min(len(safe_message), match.end() + 48)]
        if re.search(r"\b(?:budget|per\s+person|spend|afford|travell?er)\b", context, re.IGNORECASE):
            amount = _decimal_amount(match.group("amount"))
            if amount is not None:
                amounts.add(amount)
    return amounts


def _trusted_reply_numbers(enquiry_row: object, source: AIDraftSource) -> TrustedReplyNumbers:
    dates: set[date] = set()
    for value in (enquiry_row.travel_start_date, enquiry_row.travel_end_date):
        if isinstance(value, date):
            dates.add(value)
    adults, children, infants = enquiry_row.adults, enquiry_row.children, enquiry_row.infants
    budget_amounts: set[Decimal] = set()
    budget_currency = "INR" if enquiry_row.budget_amount_inr is not None else None
    submitted_budget = _decimal_amount(enquiry_row.budget_amount_inr)
    if submitted_budget is not None:
        budget_amounts.add(submitted_budget)
    budget_amounts.update(_original_message_budget_amounts(enquiry_row.original_message or ""))
    structured_values: set[str] = set()
    accepted_values: set[str] = set()
    original_message_values = {f"amount:{value:.2f}" for value in _original_message_budget_amounts(enquiry_row.original_message or "")}
    for value in dates:
        structured_values.add(f"date:{value.isoformat()}")
    for label, count in (("adults", enquiry_row.adults), ("children", enquiry_row.children), ("infants", enquiry_row.infants)):
        if count is not None:
            structured_values.add(f"traveller_count:{label}:{count}")
    if submitted_budget is not None:
        structured_values.add(f"amount:{submitted_budget:.2f}")

    # AI-generated provisional extraction is deliberately not a numeric source
    # of truth. A staff-reviewed extraction may contribute corrected facts.
    if source.extraction_source == "reviewed" and source.extraction is not None:
        extraction = source.extraction
        dates.update(value for value in (extraction.travel_start_date, extraction.travel_end_date) if value)
        adults = extraction.adults if extraction.adults is not None else adults
        children = extraction.children if extraction.children is not None else children
        infants = extraction.infants if extraction.infants is not None else infants
        accepted_budget = _decimal_amount(extraction.budget_amount)
        for value in (extraction.travel_start_date, extraction.travel_end_date):
            if value:
                accepted_values.add(f"date:{value.isoformat()}")
        for label, count in (("adults", extraction.adults), ("children", extraction.children), ("infants", extraction.infants)):
            if count is not None:
                accepted_values.add(f"traveller_count:{label}:{count}")
        if accepted_budget is not None:
            budget_amounts.add(accepted_budget)
            budget_currency = extraction.budget_currency
            accepted_values.add(f"amount:{accepted_budget:.2f}")
    return TrustedReplyNumbers(
        travel_dates=frozenset(dates), adults=adults, children=children, infants=infants,
        budget_amounts=frozenset(budget_amounts), budget_currency=budget_currency,
        structured_values=frozenset(structured_values),
        accepted_values=frozenset(accepted_values),
        original_message_values=frozenset(original_message_values),
    )


def _date_spans(body: str, trusted_dates: frozenset[date]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for match in _ISO_DATE.finditer(body):
        try:
            parsed = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
        except ValueError:
            continue
        if parsed in trusted_dates:
            spans.append(match.span())
    for match in _MONTH_DATE.finditer(body):
        month_text = match.group("month_first") or match.group("month_second")
        day_text = match.group("day_first") or match.group("day_second")
        year_text = match.group("year_first") or match.group("year_second")
        try:
            parsed = date(int(year_text), _MONTH_NUMBERS[month_text[:3].casefold()], int(day_text))
        except (ValueError, KeyError):
            continue
        if parsed in trusted_dates:
            spans.append(match.span())
    for match in _NUMERIC_DATE.finditer(body):
        first, second, year = int(match.group("first")), int(match.group("second")), int(match.group("year"))
        # Accept either common day/month order only when exactly one interpretation
        # matches trusted enquiry data; never resolve an ambiguous date by guessing.
        candidates: set[date] = set()
        for month, day in ((second, first), (first, second)):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate in trusted_dates:
                candidates.add(candidate)
        if len(candidates) == 1:
            spans.append(match.span())
    return spans


def _budget_clarification_for_span(body: str, start: int, end: int) -> bool:
    boundaries = [match.end() for match in re.finditer(r"[.!?\n]", body[:start])]
    left = boundaries[-1] if boundaries else 0
    right_match = re.search(r"[.!?\n]", body[end:])
    right = end + right_match.start() + 1 if right_match else len(body)
    sentence = body[left:right]
    return (
        "?" in sentence
        and _BUDGET_CLARIFICATION.search(sentence) is not None
        and _BUDGET_SCOPE_WORDING.search(sentence) is not None
        and _BUDGET_OWNER_WORDING.search(sentence) is not None
    )


def _numeric_source_flags(key: str | None, trusted: TrustedReplyNumbers) -> tuple[bool, bool, bool]:
    if key is None:
        return False, False, False
    return (
        key in trusted.structured_values,
        key in trusted.accepted_values,
        key in trusted.original_message_values,
    )


def _numeric_claim_failure(body: str, trusted: TrustedReplyNumbers) -> dict[str, object] | None:
    """Return privacy-safe metadata for the first ungrounded numeric claim."""
    accepted_spans = _date_spans(body, trusted.travel_dates)
    count_keys_by_start: dict[int, str] = {}
    for match in _TRAVELLER_COUNT.finditer(body):
        label = match.group("label").casefold()
        field = "adults" if label.startswith("adult") else "children" if label in {"child", "children", "kid", "kids"} else "infants"
        expected = getattr(trusted, field)
        key = f"traveller_count:{field}:{match.group('count')}"
        count_keys_by_start[match.start("count")] = key
        if expected is not None and int(match.group("count")) == expected:
            accepted_spans.append(match.span("count"))

    for match in _MONEY_AMOUNT.finditer(body):
        amount = _decimal_amount(match.group("amount"))
        marker = (match.group("prefix") or match.group("suffix") or "").strip().upper()
        if marker == "₹":
            marker = "INR"
        grounded_value = amount in trusted.budget_amounts and trusted.budget_currency == "INR"
        clarification = _budget_clarification_for_span(body, *match.span())
        if grounded_value and marker in {"INR", "RS", "RS."} and clarification:
            accepted_spans.append(match.span())
        else:
            key = f"amount:{amount:.2f}" if amount is not None else None
            structured, accepted, original = _numeric_source_flags(key, trusted)
            condition = (
                "budget_clarification_required" if grounded_value and marker in {"INR", "RS", "RS."}
                else "budget_currency_not_supported" if marker not in {"INR", "RS", "RS."}
                else "no_matching_trusted_value"
            )
            return {
                "category": "inr_amount", "trusted": grounded_value,
                "condition": condition, "structured": structured,
                "accepted": accepted, "original": original,
            }

    for match in _ANY_CURRENCY_MARKER.finditer(body):
        if not any(start <= match.start() and match.end() <= end for start, end in accepted_spans):
            marker = match.group().upper()
            category = "inr_amount" if marker in {"₹", "INR", "RS", "RS."} else "other"
            condition = "currency_without_grounded_amount" if category == "inr_amount" else "budget_currency_not_supported"
            return {
                "category": category, "trusted": False, "condition": condition,
                "structured": False, "accepted": False, "original": False,
            }

    for number in _NUMBER_TOKEN.finditer(body):
        if any(start <= number.start() and number.end() <= end for start, end in accepted_spans):
            continue

        count_key = count_keys_by_start.get(number.start())
        if count_key is not None:
            structured, accepted, original = _numeric_source_flags(count_key, trusted)
            return {
                "category": "traveller_count", "trusted": structured or accepted,
                "condition": "traveller_count_mismatch", "structured": structured,
                "accepted": accepted, "original": original,
            }

        amount = _decimal_amount(number.group())
        key = f"amount:{amount:.2f}" if amount is not None else None
        structured, accepted, original = _numeric_source_flags(key, trusted)
        is_budget = amount in trusted.budget_amounts and trusted.budget_currency == "INR"
        nearby_currency = any(
            max(0, number.start() - 4) <= marker.start() <= number.end() + 1
            for marker in _ANY_CURRENCY_MARKER.finditer(body)
        )
        if is_budget and not nearby_currency and _budget_clarification_for_span(body, *number.span()):
            accepted_spans.append(number.span())
            continue
        if is_budget:
            category, condition = "inr_amount", "budget_clarification_required"
        else:
            date_match = next((m for m in _NUMERIC_DATE.finditer(body) if m.start() <= number.start() and number.end() <= m.end()), None)
            if date_match:
                category = "year" if number.group() == date_match.group("year") else "date"
                condition = "date_not_trusted_or_ambiguous"
                possible_dates: set[date] = set()
                first, second, year = int(date_match.group("first")), int(date_match.group("second")), int(date_match.group("year"))
                for month, day in ((second, first), (first, second)):
                    try:
                        possible_dates.add(date(year, month, day))
                    except ValueError:
                        pass
                date_keys = {f"date:{value.isoformat()}" for value in possible_dates}
                structured = structured or any(value in trusted.structured_values for value in date_keys)
                accepted = accepted or any(value in trusted.accepted_values for value in date_keys)
            else:
                count_match = next((m for m in _TRAVELLER_COUNT.finditer(body) if m.start("count") == number.start()), None)
                if count_match:
                    category, condition = "traveller_count", "traveller_count_mismatch"
                elif re.fullmatch(r"\d{4}", number.group()) and re.search(r"\b(?:19|20)\d{2}\b", body[max(0, number.start()-16):number.end()+16]):
                    category, condition = "year", "year_not_trusted"
                else:
                    category, condition = "other", "no_matching_trusted_value"
        return {
            "category": category, "trusted": structured or accepted or original,
            "condition": condition, "structured": structured,
            "accepted": accepted, "original": original,
        }
    return None


def _numeric_claim_is_grounded(body: str, trusted: TrustedReplyNumbers) -> bool:
    return _numeric_claim_failure(body, trusted) is None


def _restricted_prose_match(value: str) -> tuple[str, str] | None:
    """Return only the earliest fixed validator term and category, never matched text."""
    matches = [
        (match.start(), order, identifier, category)
        for order, (identifier, category, pattern) in enumerate(_RESTRICTED_MATCH_PATTERNS)
        if (match := pattern.search(value)) is not None
    ]
    if not matches:
        return None
    _, _, identifier, category = min(matches)
    return identifier, category


class AIReplyDraftError(Exception):
    """Safe user-facing failure with no provider or exception text."""

    def __init__(self, message: str, status_code: int = 503):
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class AIDraftSource:
    suggestion_id: UUID | None
    suggestion_updated_at: datetime | None
    extraction: EnquiryExtraction | None
    extraction_source: str


@dataclass(frozen=True)
class GeneratedReplyDraft:
    body: str
    source: AIDraftSource
    provider: str
    model: str


def ai_reply_drafting_available(settings: Settings | None = None) -> bool:
    configured = settings or get_settings()
    return bool(
        configured.ai_enabled
        and configured.ai_provider == "groq"
        and configured.groq_api_key
        and configured.groq_api_key.get_secret_value().strip()
        and configured.groq_model
        and configured.groq_model.strip()
    )


def _typed_extraction(value: object) -> EnquiryExtraction | None:
    if not isinstance(value, dict):
        return None
    try:
        return EnquiryExtraction.model_validate_json(json.dumps(value), strict=True)
    except (TypeError, ValueError):
        return None


def _source_for_suggestion(suggestion: AISuggestion | None) -> AIDraftSource:
    if suggestion is None:
        return AIDraftSource(None, None, None, "none")
    if suggestion.extraction_review_status in {"accepted", "corrected"}:
        accepted = _typed_extraction(suggestion.accepted_extraction)
        if accepted is not None:
            return AIDraftSource(suggestion.id, suggestion.updated_at, accepted, "reviewed")
        # A reviewed-but-invalid or missing value must not fall back to raw AI.
        return AIDraftSource(suggestion.id, suggestion.updated_at, None, "none")
    if suggestion.accepted_extraction is not None:
        # Inconsistent legacy/corrupt metadata is not proof of staff acceptance.
        return AIDraftSource(suggestion.id, suggestion.updated_at, None, "none")
    if suggestion.generation_status == "completed":
        raw = _typed_extraction(suggestion.extracted_data)
        if raw is not None:
            return AIDraftSource(suggestion.id, suggestion.updated_at, raw, "provisional")
    return AIDraftSource(suggestion.id, suggestion.updated_at, None, "none")


def _latest_suggestion(session: Session, enquiry_id: UUID) -> AISuggestion | None:
    return session.scalar(
        select(AISuggestion)
        .where(AISuggestion.enquiry_id == enquiry_id)
        .order_by(AISuggestion.created_at.desc(), AISuggestion.id.desc())
        .limit(1)
    )


def _recommendation_signature(result: RecommendationResult) -> tuple[tuple[str, str | None], ...]:
    return tuple(
        (item.package_id, item.verified_price.price_id if item.verified_price else None)
        for item in result.recommendations
    )


def _lock_current_recommendations(
    session: Session,
    extraction: EnquiryExtraction | None,
) -> RecommendationResult | None:
    """Recompute current candidates and hold shared row locks through draft commit.

    The provider call has already finished. These brief locks keep the selected
    package content and verified price rows stable while the route inserts the
    reply. Recomputing after locking prevents a just-updated price from being
    copied into the draft from an earlier read.
    """
    if extraction is None:
        return None
    current = recommend_packages(session, extraction)
    for _ in range(3):
        package_ids = sorted({UUID(item.package_id) for item in current.recommendations}, key=str)
        price_ids = sorted(
            {
                UUID(item.verified_price.price_id)
                for item in current.recommendations
                if item.verified_price is not None
            },
            key=str,
        )
        if package_ids:
            session.scalars(
                select(Package.id)
                .where(Package.id.in_(package_ids))
                .order_by(Package.id)
                .with_for_update(read=True)
            ).all()
        if price_ids:
            session.scalars(
                select(PackagePrice.id)
                .where(PackagePrice.id.in_(price_ids))
                .order_by(PackagePrice.id)
                .with_for_update(read=True)
            ).all()
        refreshed = recommend_packages(session, extraction)
        if _recommendation_signature(refreshed) == _recommendation_signature(current):
            return refreshed
        current = refreshed
    raise AIReplyDraftError("Package information changed while the draft was generated. Reload and try again.", 409)


def _money(value: str) -> str:
    # Decimal is formatted from the exact database representation; no arithmetic.
    amount = Decimal(value)
    return f"{amount:,.2f}"


def _period(start_month: int, end_month: int) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{months[start_month - 1]}–{months[end_month - 1]}"


def _recommendation_section(result: RecommendationResult | None, source_kind: str) -> str:
    lines = ["Package suggestions for staff review (not a confirmed quote or booking):"]
    if source_kind == "provisional":
        lines.append("These suggestions use unreviewed AI extraction and are provisional; please verify them with the traveller.")
    if result is None or not result.recommendations:
        lines.append("We will review suitable package options and confirm details with you.")
        lines.append("Pricing requires staff confirmation. No availability or booking is confirmed.")
    else:
        for item in result.recommendations:
            destination = ", ".join(item.destinations) if item.destinations else "Destination not specified"
            duration = f" · {item.duration_days} days" if item.duration_days is not None else ""
            lines.append(f"• {item.title} — {destination}{duration}.")
            if item.match_reasons:
                lines.append(f"  Relevance: {item.match_reasons[0]}")
            price = item.verified_price
            if price is not None and item.price_status == "matched":
                amount = _money(price.price_per_person)
                period = _period(price.start_month, price.end_month)
                currency_amount = f"₹{amount}" if price.currency == "INR" else f"{price.currency} {amount}"
                basis = {
                    "per_person": "per person",
                    "per_person_twin_sharing": "per person, twin sharing",
                    "per_room": "per room",
                }.get(price.price_basis, f"stored basis: {price.price_basis.replace('_', ' ')}")
                lines.append(f"  Stored rate: {currency_amount} · {basis} · {price.season_name} ({period}).")
                if price.valid_from or price.valid_until:
                    lower = price.valid_from.strftime("%d %b %Y") if price.valid_from else "not specified"
                    upper = price.valid_until.strftime("%d %b %Y") if price.valid_until else "not specified"
                    lines.append(f"  Stored effective dates: {lower} to {upper}.")
                if price.price_note:
                    lines.append(f"  Stored price note: {price.price_note}")
            else:
                lines.append("  Pricing requires staff confirmation for the requested period.")
            if item.caveats:
                lines.append(f"  Please confirm: {' '.join(item.caveats)}")
    lines.append("GST treatment requires staff confirmation. These are not final group totals; child/infant fares, availability and booking status are not confirmed.")
    return "\n".join(lines)


def _extraction_context(extraction: EnquiryExtraction | None, source_kind: str) -> str:
    if extraction is None:
        return "No validated extraction is available. Do not infer missing travel details."
    label = "Staff-reviewed extraction" if source_kind == "reviewed" else "Unaccepted provisional extraction"
    values = extraction.model_dump(mode="json")
    return f"{label}: {json.dumps(values, ensure_ascii=False, separators=(',', ':'))}"


def _submitted_enquiry_context(enquiry_row: object) -> dict[str, object]:
    """Expose only customer-entered trip fields needed to ground reply prose."""
    return {
        "travel_start_date": enquiry_row.travel_start_date.isoformat() if enquiry_row.travel_start_date else None,
        "travel_end_date": enquiry_row.travel_end_date.isoformat() if enquiry_row.travel_end_date else None,
        "adults": enquiry_row.adults,
        "children": enquiry_row.children,
        "infants": enquiry_row.infants,
        "budget_amount_inr": str(enquiry_row.budget_amount_inr) if enquiry_row.budget_amount_inr is not None else None,
        "budget_scope_in_form": "total" if enquiry_row.budget_amount_inr is not None else None,
    }


def _build_prompt(
    message: str,
    extraction: EnquiryExtraction | None,
    source_kind: str,
    recs: RecommendationResult | None,
    submitted_context: dict[str, object] | None = None,
) -> str:
    safe_message = redact_contact_patterns(message)
    recommendation_facts = []
    if recs:
        for item in recs.recommendations:
            recommendation_facts.append({
                "package": item.title,
                "destinations": list(item.destinations),
                "duration_days": item.duration_days,
                "verified_relevance": list(item.match_reasons),
            })
    return (
        "Write a concise, warm acknowledgement and any useful follow-up questions for a tour enquiry. "
        "Return JSON only with exactly one string field named body. You may repeat only travel dates and individual "
        "traveller counts present in the customer-submitted structured details below. Do not include any other "
        "numbers. You may mention a submitted "
        "budget amount only in a clarification question when the original message and form disagree; identify it as "
        "the traveller's own stated budget, never as a package rate, quote or confirmed price. In particular, if the "
        "message says an amount is per person while the form records it as a total, ask which scope the traveller "
        "intended and do not choose either interpretation. Do not invent or quote package prices, totals, taxes/GST, "
        "discounts, free or complimentary offers, or availability. Do not claim hotel, transport, flight, itinerary, "
        "or inclusion facts that are not verified. The application adds verified package and pricing sections "
        "separately. Do not invent customer identity or missing facts. Keep the response editable.\n\n"
        f"Original enquiry (contact patterns redacted): {safe_message}\n"
        f"Customer-submitted structured trip details (grounding data, not a quotation): "
        f"{json.dumps(submitted_context or {}, ensure_ascii=False, separators=(',', ':'))}\n"
        f"Structured context: {_extraction_context(extraction, source_kind)}\n"
        f"Verified package relevance context, without prices: {json.dumps(recommendation_facts, ensure_ascii=False)}"
    )


def _parse_provider_content(
    content: str,
    *,
    trusted_numbers: TrustedReplyNumbers | None = None,
) -> AIReplyContent:
    try:
        parsed = AIReplyContent.model_validate_json(content, strict=True)
    except (TypeError, ValueError) as exc:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_content_rejected",
            stage="reply_json_schema_validation",
            provider="groq",
            failure_code="invalid_provider_response",
            exception_type=type(exc).__name__,
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["invalid_provider_response"], 502) from None
    if _UNSAFE_PROSE.search(parsed.body):
        matched_term, rule_category = _restricted_prose_match(parsed.body) or ("other", "other")
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_content_rejected",
            stage="reply_safety_validation",
            provider="groq",
            failure_code="invalid_provider_response",
            rule="restricted_claim_term",
            rule_category=rule_category,
            matched_term=matched_term,
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["invalid_provider_response"], 502)
    numeric_failure = _numeric_claim_failure(parsed.body, trusted_numbers or TrustedReplyNumbers())
    if numeric_failure is not None:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_content_rejected",
            stage="reply_safety_validation",
            provider="groq",
            failure_code="invalid_provider_response",
            rule="untrusted_numeric_claim",
            rule_category="numeric_or_currency",
            matched_term="numeric_or_currency_marker",
            numeric_claim_category=numeric_failure["category"],
            trusted_value_found=numeric_failure["trusted"],
            numeric_validation_condition=numeric_failure["condition"],
            structured_data_match=numeric_failure["structured"],
            accepted_extraction_match=numeric_failure["accepted"],
            original_message_match=numeric_failure["original"],
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["invalid_provider_response"], 502)
    if _UNSAFE_FREE_OFFER.search(parsed.body):
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_content_rejected",
            stage="reply_safety_validation",
            provider="groq",
            failure_code="invalid_provider_response",
            rule="free_commercial_offer",
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["invalid_provider_response"], 502)
    return parsed


def _safe_provider_error(error: AIProviderError) -> AIReplyDraftError:
    code = error.code.value
    return AIReplyDraftError(
        _SAFE_FAILURE_MESSAGES.get(code, _SAFE_FAILURE_MESSAGES["generation_failed"]),
        503 if code in {"provider_unavailable", "provider_timeout", "provider_rate_limited"} else 502,
    )


def generate_reply_draft_content(
    session: Session,
    *,
    enquiry_id: UUID,
    actor: User,
    expected_enquiry_updated_at: str,
    settings: Settings | None = None,
    generator: Callable[..., GenerationResponse] | None = None,
) -> GeneratedReplyDraft:
    """Generate safe prose and app-owned pricing without holding DB locks over HTTP."""
    configured = settings or get_settings()
    if not actor.is_active or actor.role not in {"admin", "editor", "consultant"}:
        raise AIReplyDraftError("You do not have permission to draft a reply.", 403)
    if session.new or session.dirty or session.deleted:
        raise AIReplyDraftError("Reload the enquiry before generating a draft.", 409)

    enquiry_row = session.execute(
        select(
            Enquiry.id, Enquiry.original_message, Enquiry.assigned_to_id, Enquiry.updated_at,
            Enquiry.travel_start_date, Enquiry.travel_end_date, Enquiry.adults, Enquiry.children,
            Enquiry.infants, Enquiry.budget_amount_inr,
        )
        .where(Enquiry.id == enquiry_id)
    ).one_or_none()
    if enquiry_row is None or (actor.role == "consultant" and enquiry_row.assigned_to_id != actor.id):
        raise AIReplyDraftError("Enquiry not found or outside your assigned work.", 404)
    if enquiry_row.updated_at.isoformat() != expected_enquiry_updated_at:
        raise AIReplyDraftError("This enquiry changed after you opened it. Reload and try again.", 409)
    if not ai_reply_drafting_available(configured):
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["provider_unavailable"], 503)
    suggestion = _latest_suggestion(session, enquiry_id)
    source = _source_for_suggestion(suggestion)
    trusted_numbers = _trusted_reply_numbers(enquiry_row, source)
    recs = recommend_packages(session, source.extraction) if source.extraction is not None else None
    prompt = _build_prompt(
        enquiry_row.original_message or "", source.extraction, source.extraction_source, recs,
        _submitted_enquiry_context(enquiry_row),
    )

    # This synchronous staff action performs only unlocked reads before the
    # provider request. Keep the caller-owned transaction intact; the route
    # will commit the reply or roll back on failure.
    request = GenerationRequest(
        prompt=prompt,
        schema_name="ai_reply_content",
        max_output_tokens=900,
        output_model=AIReplyContent,
    )
    provider_started = time.monotonic()
    try:
        response = (generator or generate)(request, settings=configured)
    except AIProviderError as error:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_generation_failed",
            stage="provider_orchestration",
            provider=configured.ai_provider,
            model=configured.groq_model if configured.ai_provider == "groq" else configured.gemini_model,
            status_code=error.provider_status_code,
            failure_code=error.code.value,
            exception_type=type(error).__name__,
            duration_ms=round((time.monotonic() - provider_started) * 1000),
        )
        raise _safe_provider_error(error) from None
    except Exception as exc:
        # Never expose or log raw transport/generator exception details.
        log_ai_diagnostic(
            logger,
            logging.ERROR,
            event="ai_reply_generation_failed",
            stage="provider_orchestration",
            provider=configured.ai_provider,
            model=configured.groq_model if configured.ai_provider == "groq" else configured.gemini_model,
            failure_code="generation_failed",
            exception_type=type(exc).__name__,
            duration_ms=round((time.monotonic() - provider_started) * 1000),
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["generation_failed"], 502) from None
    log_ai_diagnostic(
        logger,
        logging.INFO,
        event="ai_reply_provider_generation_complete",
        stage="provider_generation",
        provider=response.provider if isinstance(response, GenerationResponse) else None,
        model=response.model if isinstance(response, GenerationResponse) else None,
        duration_ms=round((time.monotonic() - provider_started) * 1000),
    )
    if (
        not isinstance(response, GenerationResponse)
        or response.provider != "groq"
        or response.model != configured.groq_model
        or not isinstance(response.content, str)
    ):
        contract_rule = (
            "unexpected_response_type" if not isinstance(response, GenerationResponse)
            else "provider_mismatch" if response.provider != "groq"
            else "model_mismatch" if response.model != configured.groq_model
            else "content_type_mismatch"
        )
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_response_rejected",
            stage="provider_response_contract",
            provider=response.provider if isinstance(response, GenerationResponse) else None,
            model=response.model if isinstance(response, GenerationResponse) else None,
            failure_code="invalid_provider_response",
            rule=contract_rule,
        )
        raise AIReplyDraftError(_SAFE_FAILURE_MESSAGES["invalid_provider_response"], 502)
    parsed = _parse_provider_content(response.content, trusted_numbers=trusted_numbers)

    # Re-read after the HTTP call so staff changes made while generation ran
    # cannot silently produce a draft from stale context.
    current = session.execute(
        select(Enquiry.id, Enquiry.updated_at).where(Enquiry.id == enquiry_id)
    ).one_or_none()
    if current is None or current.updated_at.isoformat() != expected_enquiry_updated_at:
        session.rollback()
        raise AIReplyDraftError("This enquiry changed while the draft was generated. Reload and try again.", 409)
    latest = _latest_suggestion(session, enquiry_id)
    if (latest.id if latest else None) != source.suggestion_id:
        session.rollback()
        raise AIReplyDraftError("Extraction changed while the draft was generated. Reload and try again.", 409)
    if latest is not None and latest.updated_at != source.suggestion_updated_at:
        session.rollback()
        raise AIReplyDraftError("Extraction changed while the draft was generated. Reload and try again.", 409)
    # Recompute recommendations/prices against current PostgreSQL state after
    # the provider call; no provider-authored pricing can enter this section.
    current_recs = _lock_current_recommendations(session, source.extraction)
    body = f"{parsed.body}\n\n{_recommendation_section(current_recs, source.extraction_source)}"
    if len(body) > 10_000:
        log_ai_diagnostic(
            logger,
            logging.WARNING,
            event="ai_reply_content_rejected",
            stage="reply_size_validation",
            provider=response.provider,
            model=response.model,
            failure_code="invalid_provider_response",
            rule="reply_length_limit",
        )
        raise AIReplyDraftError("The generated draft is too long. Use the manual reply form.", 502)
    return GeneratedReplyDraft(body, source, response.provider, response.model)


def provider_failure_message(code: str | None) -> str | None:
    """Return only fixed safe operational guidance for known provider codes."""
    return _SAFE_FAILURE_MESSAGES.get(code) if code else None
