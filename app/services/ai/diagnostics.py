"""Structured, allow-listed diagnostics for AI provider workflows."""

from __future__ import annotations

import json
import logging
import re
from typing import Any


_SAFE_FAILURE_CODES = {
    "provider_unavailable",
    "provider_timeout",
    "provider_rate_limited",
    "provider_authentication_failed",
    "provider_invalid_request",
    "invalid_provider_response",
    "validation_failed",
    "generation_failed",
    "content_refused",
}

_SAFE_RULE_CATEGORIES = {
    "numeric_or_currency",
    "pricing_claim",
    "tax_claim",
    "commercial_commitment",
    "unverified_feature",
    "other",
}
_SAFE_NUMERIC_CATEGORIES = {"date", "traveller_count", "inr_amount", "year", "other"}
_SAFE_NUMERIC_FAILURES = {
    "no_matching_trusted_value",
    "traveller_count_mismatch",
    "date_not_trusted_or_ambiguous",
    "year_not_trusted",
    "budget_clarification_required",
    "budget_currency_not_supported",
    "currency_without_grounded_amount",
}

_SAFE_MATCHED_TERMS = {
    "numeric_or_currency_marker",
    "price", "pricing", "rate", "fare", "discount", "quote", "quotation",
    "gst", "tax", "available", "availability", "guarantee", "guaranteed",
    "booking", "booked", "complimentary", "included", "includes", "hotel",
    "flight", "itinerary",
}

_SAFE_PROVIDER_ERROR_MESSAGES = {
    "The provider rejected an unsupported request parameter.",
    "The provider rejected the JSON response configuration.",
    "The provider rejected the reasoning format configuration.",
    "The provider rejected the completion token limit.",
    "The configured model was rejected.",
    "The request exceeds the model context limit.",
    "The provider rejected a request field.",
    "The provider rejected the request.",
}


def log_ai_diagnostic(
    logger: logging.Logger,
    level: int,
    *,
    event: str,
    stage: str,
    provider: str | None = None,
    model: str | None = None,
    status_code: int | None = None,
    failure_code: str | None = None,
    exception_type: str | None = None,
    rule: str | None = None,
    rule_category: str | None = None,
    matched_term: str | None = None,
    numeric_claim_category: str | None = None,
    trusted_value_found: bool | None = None,
    numeric_validation_condition: str | None = None,
    structured_data_match: bool | None = None,
    accepted_extraction_match: bool | None = None,
    original_message_match: bool | None = None,
    provider_error_code: str | None = None,
    provider_error_param: str | None = None,
    sanitized_provider_message: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Emit JSON containing only safe operational metadata, never request data."""
    record: dict[str, Any] = {"event": event, "stage": stage}
    if provider is not None:
        record["provider"] = provider
    if model is not None:
        record["model"] = model
    if status_code is not None:
        record["provider_status_code"] = status_code
    if failure_code is not None:
        record["failure_code"] = (
            failure_code if failure_code in _SAFE_FAILURE_CODES else "generation_failed"
        )
    if exception_type is not None:
        # Only the class name is accepted; the exception message is never logged.
        record["exception_type"] = exception_type.rsplit(".", 1)[-1]
    if rule is not None:
        record["rule"] = rule
    if rule_category is not None:
        record["rule_category"] = (
            rule_category if rule_category in _SAFE_RULE_CATEGORIES else "other"
        )
    if matched_term is not None:
        # Accept only fixed rule identifiers, never text copied from a response.
        record["matched_term"] = (
            matched_term if matched_term in _SAFE_MATCHED_TERMS else "other"
        )
    if numeric_claim_category is not None:
        record["numeric_claim_category"] = (
            numeric_claim_category if numeric_claim_category in _SAFE_NUMERIC_CATEGORIES else "other"
        )
    if trusted_value_found is not None:
        record["trusted_value_found"] = bool(trusted_value_found)
    if numeric_validation_condition is not None:
        record["numeric_validation_condition"] = (
            numeric_validation_condition
            if numeric_validation_condition in _SAFE_NUMERIC_FAILURES
            else "no_matching_trusted_value"
        )
    if structured_data_match is not None:
        record["structured_data_match"] = bool(structured_data_match)
    if accepted_extraction_match is not None:
        record["accepted_extraction_match"] = bool(accepted_extraction_match)
    if original_message_match is not None:
        record["original_message_match"] = bool(original_message_match)
    if provider_error_code is not None:
        record["provider_error_code"] = (
            provider_error_code
            if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", provider_error_code)
            else "unclassified"
        )
    if provider_error_param is not None:
        record["provider_error_param"] = (
            provider_error_param
            if re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,100}", provider_error_param)
            else "unclassified"
        )
    if sanitized_provider_message is not None:
        record["sanitized_provider_message"] = (
            sanitized_provider_message
            if sanitized_provider_message in _SAFE_PROVIDER_ERROR_MESSAGES
            else "The provider rejected the request."
        )
    if duration_ms is not None:
        record["duration_ms"] = max(0, duration_ms)
    logger.log(level, json.dumps(record, separators=(",", ":"), sort_keys=True))
