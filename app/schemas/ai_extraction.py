"""Strict, versioned schema for untrusted AI enquiry extraction output."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal

import pycountry
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


EXTRACTION_SCHEMA_VERSION = "1.0"
"""Policy: accept active ISO 4217 alphabetic currency codes, not historical
codes, funds, precious metals, test-only codes, or the `XXX` no-currency marker.
ISO codes are checked from pycountry's bundled reference data; there is no
runtime network lookup and no conversion.
"""
SUPPORTED_BUDGET_CURRENCIES = frozenset(
    currency.alpha_3
    for currency in pycountry.currencies
    if getattr(currency, "withdrawal_date", None) is None
    and currency.alpha_3
    not in {"XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XDR", "XPD", "XPT", "XSU", "XTS", "XUA", "XXX"}
)
MAX_TRAVELLERS_PER_TYPE = 100
MAX_BUDGET = Decimal("9999999999.99")  # Fits Numeric(12, 2).

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
InfoText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
BudgetScope = Literal["total", "per_person"]
ExtractionField = Literal[
    "destination",
    "travel_start_date",
    "travel_end_date",
    "travel_year",
    "travel_dates_flexible",
    "adults",
    "children",
    "infants",
    "budget_amount",
    "budget_currency",
    "budget_scope",
]

AI_FAILURE_CODES = frozenset(
    {
        "provider_unavailable",
        "provider_timeout",
        "invalid_provider_response",
        "validation_failed",
        "generation_failed",
    }
)


class EnquiryExtraction(BaseModel):
    """Normalized model output or human-corrected extraction.

    `None` means unknown. `missing_information` lists fields the customer did
    not supply; `ambiguous_information` lists fields they did supply but whose
    meaning cannot be resolved. The model deliberately does not infer a year
    from a month or infer a currency from a locale.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1.0"] = EXTRACTION_SCHEMA_VERSION
    destination: ShortText | None = None
    travel_start_date: date | None = None
    travel_end_date: date | None = None
    travel_dates_flexible: bool | None = None
    original_travel_period_text: Annotated[str, StringConstraints(max_length=300)] | None = None
    adults: Annotated[int, Field(ge=0, le=MAX_TRAVELLERS_PER_TYPE)] | None = None
    children: Annotated[int, Field(ge=0, le=MAX_TRAVELLERS_PER_TYPE)] | None = None
    infants: Annotated[int, Field(ge=0, le=MAX_TRAVELLERS_PER_TYPE)] | None = None
    budget_amount: Decimal | None = None
    budget_currency: str | None = None
    budget_scope: BudgetScope | None = None
    preferences: list[ShortText] = Field(default_factory=list, max_length=30)
    missing_information: list[ExtractionField] = Field(default_factory=list, max_length=30)
    ambiguous_information: list[ExtractionField] = Field(default_factory=list, max_length=30)

    @field_validator("travel_start_date", "travel_end_date", mode="before")
    @classmethod
    def parse_iso_date_without_guessing(cls, value: object) -> object:
        if value is None or isinstance(value, date):
            return value
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Use an ISO calendar date (YYYY-MM-DD), or null when unknown.")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Enter a valid calendar date.") from exc

    @field_validator("original_travel_period_text", mode="before")
    @classmethod
    def validate_period_text(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("Travel period text must be a string or null.")
        return value

    @field_validator("budget_amount", mode="before")
    @classmethod
    def parse_decimal_budget(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        # JSON numbers decode to binary floats. Require decimal text (or Decimal)
        # so money never passes through an imprecise float conversion.
        if isinstance(value, bool) or isinstance(value, float):
            raise ValueError("Budget must be supplied as a decimal string, not a floating-point number.")
        if not isinstance(value, (str, int, Decimal)):
            raise ValueError("Budget must be a decimal string or null.")
        try:
            amount = Decimal(value)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Enter a valid decimal budget amount.") from exc
        if not amount.is_finite() or amount < 0 or amount > MAX_BUDGET:
            raise ValueError("Budget must be between 0 and 9999999999.99.")
        if amount.as_tuple().exponent < -2:
            raise ValueError("Budget can have no more than two decimal places.")
        return amount

    @field_validator("budget_currency", mode="before")
    @classmethod
    def validate_currency(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("Currency must be a supported currency code or null.")
        normalized = value.strip().upper()
        if normalized not in SUPPORTED_BUDGET_CURRENCIES:
            raise ValueError("Currency is unsupported; use a supported explicit currency code or null.")
        return normalized

    @model_validator(mode="after")
    def validate_related_fields(self) -> EnquiryExtraction:
        if self.travel_start_date and self.travel_end_date and self.travel_end_date < self.travel_start_date:
            raise ValueError("Travel end date cannot precede travel start date.")
        if self.budget_amount is None and (self.budget_currency is not None or self.budget_scope is not None):
            raise ValueError("Budget currency and scope require a known budget amount.")
        known_traveller_counts = (self.adults, self.children, self.infants)
        if all(count is not None for count in known_traveller_counts) and sum(known_traveller_counts) == 0:
            raise ValueError("A known traveller party must include at least one traveller.")

        missing = set(self.missing_information)
        ambiguous = set(self.ambiguous_information)
        for field_name, field_value in (
            ("destination", self.destination),
            ("adults", self.adults),
            ("children", self.children),
            ("infants", self.infants),
        ):
            if field_value is None and field_name not in missing and field_name not in ambiguous:
                raise ValueError(f"Unknown {field_name} must be marked missing or ambiguous.")
        if (self.travel_start_date is None) != (self.travel_end_date is None):
            missing_bound = "travel_start_date" if self.travel_start_date is None else "travel_end_date"
            if missing_bound not in missing | ambiguous:
                raise ValueError(f"A partial date range must mark {missing_bound} as missing or ambiguous.")
        if self.original_travel_period_text:
            period = self.original_travel_period_text.casefold()
            month_pattern = (
                r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
                r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
            )
            has_month = re.search(month_pattern, period) is not None
            has_year = re.search(r"\b(?:19|20|21)\d{2}\b", period) is not None
            if has_month and not has_year:
                if "travel_year" not in ambiguous:
                    raise ValueError("A month without a year must mark travel_year as ambiguous.")
                if self.travel_start_date is not None or self.travel_end_date is not None:
                    raise ValueError("Do not assign calendar dates when the stated travel year is unresolved.")
        if self.travel_start_date is None and self.travel_end_date is None:
            if not {"travel_start_date", "travel_end_date"}.issubset(missing | ambiguous):
                raise ValueError("An incomplete travel period must mark both travel dates missing or ambiguous.")

        if self.budget_amount is None:
            if "budget_amount" not in missing | ambiguous:
                raise ValueError("An unknown budget amount must be listed as missing or ambiguous.")
        elif self.budget_currency is None and not (
            {"budget_currency"} & (missing | ambiguous)
        ):
            raise ValueError("A known amount with unknown currency must mark budget_currency missing or ambiguous.")
        if self.budget_amount is not None and self.budget_scope is None and not (
            {"budget_scope"} & (missing | ambiguous)
        ):
            raise ValueError("A known budget with unknown scope must mark budget_scope missing or ambiguous.")
        return self


def validate_failure_code(value: str | None) -> str | None:
    """Accept only safe machine codes; never persist provider or exception text."""
    if value is None:
        return None
    if value not in AI_FAILURE_CODES:
        raise ValueError("Unsupported AI failure code.")
    return value


def parse_extraction_json(raw_json: str) -> EnquiryExtraction:
    """Parse provider JSON safely; unknown keys, malformed JSON and bad types fail closed."""
    if not isinstance(raw_json, str):
        raise ValueError("Extraction payload must be JSON text.")
    return EnquiryExtraction.model_validate_json(raw_json, strict=True)


def extraction_to_json(extraction: EnquiryExtraction) -> dict[str, object]:
    """Return a JSONB-safe canonical extraction with dates and decimals as strings."""
    return extraction.model_dump(mode="json")
