"""Validation and one-use anti-CSRF tokens for public package enquiries."""

import hashlib
import re
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import Enquiry, Package, PublicFormNonce


FORM_NONCE_TTL = timedelta(minutes=30)
RESPONSE_NOTICE_VERSION = "m6-v1"
MAX_FORM_BYTES = 16 * 1024
MAX_BUDGET_INR = Decimal("9999999999.99")
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_RE = re.compile(r"^\+?[0-9().\s-]+$")
_BUDGET_RE = re.compile(r"^[0-9]+(?:\.[0-9]{1,2})?$")


def _now() -> datetime:
    return datetime.now(UTC)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_form_nonce(db: Session, package: Package) -> str:
    """Create a high-entropy, package-bound nonce; persist only its SHA-256 hash."""
    now = _now()
    db.execute(delete(PublicFormNonce).where(PublicFormNonce.expires_at <= now))
    token = secrets.token_urlsafe(32)
    db.add(
        PublicFormNonce(
            token_hash=_token_hash(token),
            package_id=package.id,
            created_at=now,
            expires_at=now + FORM_NONCE_TTL,
        )
    )
    db.flush()
    return token


def consume_form_nonce(db: Session, package: Package, token: str | None) -> bool:
    """Atomically consume a valid, unexpired nonce for this package exactly once."""
    if not token or len(token) > 128:
        return False
    now = _now()
    consumed_hash = db.execute(
        delete(PublicFormNonce)
        .where(
            PublicFormNonce.token_hash == _token_hash(token),
            PublicFormNonce.package_id == package.id,
            PublicFormNonce.expires_at > now,
        )
        .returning(PublicFormNonce.token_hash)
    ).scalar_one_or_none()
    return consumed_hash is not None


def valid_form_nonce(db: Session, package: Package, token: str | None) -> bool:
    """Check an unexpired nonce without consuming it, allowing form correction."""
    if not token or len(token) > 128:
        return False
    return db.scalar(
        select(PublicFormNonce.token_hash).where(
            PublicFormNonce.token_hash == _token_hash(token),
            PublicFormNonce.package_id == package.id,
            PublicFormNonce.expires_at > _now(),
        )
    ) is not None


def _text(form: Mapping[str, object], field: str) -> str:
    value = form.get(field, "")
    return value if isinstance(value, str) else ""


def parse_public_enquiry_form(form: Mapping[str, object]) -> tuple[dict[str, object], dict[str, str]]:
    """Parse untrusted form values into Enquiry fields and field-specific errors.

    original_message is checked for non-whitespace content but stored byte-for-
    byte as decoded by the form parser, including leading/trailing whitespace.
    """
    errors: dict[str, str] = {}
    raw_name = _text(form, "customer_name")
    raw_email = _text(form, "customer_email")
    raw_phone = _text(form, "customer_phone")
    email = raw_email.strip()
    phone = raw_phone.strip()
    raw_message = _text(form, "original_message")
    start_text = _text(form, "travel_start_date").strip()
    end_text = _text(form, "travel_end_date").strip()
    adults_text = _text(form, "adults").strip()
    children_text = _text(form, "children").strip()
    infants_text = _text(form, "infants").strip()
    budget_text = _text(form, "budget_amount_inr").strip()

    name = raw_name.strip()
    if not name:
        errors["customer_name"] = "Enter your name."
    elif len(raw_name) > 160:
        errors["customer_name"] = "Name must be 160 characters or fewer."

    if len(raw_email) > 320:
        errors["customer_email"] = "Email must be 320 characters or fewer."
    elif email and not _EMAIL_RE.fullmatch(email):
        errors["customer_email"] = "Enter a valid email address."

    if len(raw_phone) > 40:
        errors["customer_phone"] = "Phone number must be 40 characters or fewer."
    elif phone and (not _PHONE_RE.fullmatch(phone) or not 7 <= sum(ch.isdigit() for ch in phone) <= 15):
        errors["customer_phone"] = "Enter a valid phone number with 7 to 15 digits."
    if not email and not phone:
        errors["contact_method"] = "Enter an email address or phone number so we can respond."

    def parse_date(value: str, key: str, label: str) -> date | None:
        if not value:
            errors[key] = f"Enter the {label}."
            return None
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            errors[key] = f"Enter a valid {label.lower()}."
            return None
        if len(value) != 10:
            errors[key] = f"Enter a valid {label.lower()}."
            return None
        return parsed

    start_date = parse_date(start_text, "travel_start_date", "travel start date")
    end_date = parse_date(end_text, "travel_end_date", "travel end date")
    if start_date and end_date and end_date < start_date:
        errors["travel_end_date"] = "End date cannot be before the start date."

    def parse_count(value: str, key: str, label: str, minimum: int) -> int | None:
        numeric_text = value[1:] if value.startswith("-") else value
        if not value or len(value) > 3 or not numeric_text.isascii() or not numeric_text.isdecimal():
            errors[key] = f"Enter the number of {label}."
            return None
        count = int(value)
        if count < minimum or count > 999:
            errors[key] = f"Enter a value from {minimum} to 999."
            return None
        return count

    adults = parse_count(adults_text, "adults", "adults", 1)
    children = parse_count(children_text, "children", "children", 0)
    infants = parse_count(infants_text, "infants", "infants", 0)

    budget: Decimal | None = None
    if budget_text:
        if len(budget_text) > 13 or not _BUDGET_RE.fullmatch(budget_text):
            errors["budget_amount_inr"] = "Enter a nonnegative INR amount with up to two decimal places."
        else:
            try:
                budget = Decimal(budget_text)
            except InvalidOperation:
                errors["budget_amount_inr"] = "Enter a valid INR amount."
            else:
                if budget > MAX_BUDGET_INR:
                    errors["budget_amount_inr"] = "Budget is too large."

    if len(raw_message) > 5000:
        errors["original_message"] = "Message must be 5,000 characters or fewer."
    elif not raw_message.strip():
        errors["original_message"] = "Enter a message about the trip you are planning."

    accepted_notice = _text(form, "response_notice_accepted") == "yes"
    if not accepted_notice:
        errors["response_notice_accepted"] = "Please acknowledge the enquiry-response notice."

    values: dict[str, object] = {
        "customer_name": name,
        "customer_email": email or None,
        "customer_phone": phone or None,
        "travel_start_date": start_date,
        "travel_end_date": end_date,
        "travel_dates_flexible": _text(form, "travel_dates_flexible") == "yes",
        "adults": adults,
        "children": children,
        "infants": infants,
        "budget_amount_inr": budget,
        "original_message": raw_message,
        "response_notice_version": RESPONSE_NOTICE_VERSION if accepted_notice else None,
        "response_notice_accepted_at": _now() if accepted_notice else None,
        "whatsapp_consent": _text(form, "whatsapp_consent") == "yes",
    }
    return values, errors


def enquiry_from_form(package: Package, values: dict[str, object]) -> Enquiry:
    """Build a new unassigned enquiry from validated data only."""
    consent = bool(values.pop("whatsapp_consent"))
    return Enquiry(
        package=package,
        status="new",
        assigned_to_id=None,
        whatsapp_consent=consent,
        whatsapp_consent_at=_now() if consent else None,
        **values,
    )
