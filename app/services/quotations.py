"""MVP quotation calculation, persistence and access rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import secrets
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Enquiry, Package, PackagePrice, Quotation, User


CENT = Decimal("0.01")
MAX_TRAVEL_DAYS = 366
MAX_QUOTE_AMOUNT = Decimal("9999999999.99")


class QuotationError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        self.status_code = status_code
        super().__init__(message)


@dataclass(frozen=True)
class QuoteCalculation:
    subtotal: Decimal | None
    gst: Decimal | None
    total: Decimal | None
    total_before_gst: bool


def _amount(value: object, field: str, *, required: bool = False) -> Decimal | None:
    if value is None or str(value).strip() == "":
        if required:
            raise QuotationError(f"Enter {field}.")
        return None
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise QuotationError(f"Enter a valid {field}.") from None
    if not amount.is_finite() or amount < 0 or amount > MAX_QUOTE_AMOUNT or amount.as_tuple().exponent < -2:
        raise QuotationError(f"{field.title()} must be nonnegative, fit the quotation amount limit and use at most two decimal places.")
    return amount.quantize(CENT)


def calculate_quote(
    *,
    adults: int,
    children: int,
    infants: int,
    adult_unit_price: Decimal,
    child_unit_price: Decimal | None,
    infant_unit_price: Decimal | None,
    gst_rate_percent: Decimal | None,
) -> QuoteCalculation:
    """Calculate totals only when every included traveller has a stated fare."""
    if children and child_unit_price is None or infants and infant_unit_price is None:
        return QuoteCalculation(None, None, None, gst_rate_percent is None)
    subtotal = (
        adult_unit_price * adults
        + (child_unit_price or Decimal("0")) * children
        + (infant_unit_price or Decimal("0")) * infants
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    if subtotal > MAX_QUOTE_AMOUNT:
        raise QuotationError("Calculated quotation amount exceeds the supported limit.")
    gst = (
        (subtotal * gst_rate_percent / Decimal("100")).quantize(CENT, rounding=ROUND_HALF_UP)
        if gst_rate_percent is not None else None
    )
    total = (subtotal + (gst or Decimal("0"))).quantize(CENT)
    if total > MAX_QUOTE_AMOUNT:
        raise QuotationError("Calculated quotation total exceeds the supported limit.")
    return QuoteCalculation(subtotal, gst, total, gst_rate_percent is None)


def _check_actor(actor: User, enquiry: Enquiry) -> None:
    if not actor.is_active or actor.role not in {"admin", "editor", "consultant"}:
        raise QuotationError("You are not allowed to create quotations.", 403)
    if actor.role == "consultant" and enquiry.assigned_to_id != actor.id:
        raise QuotationError("Enquiry not found or outside your assigned work.", 404)


def _travel_period(start: date, end: date) -> None:
    if end < start:
        raise QuotationError("Travel end date cannot precede the start date.")
    if (end - start).days > MAX_TRAVEL_DAYS:
        raise QuotationError("Travel period must be no longer than one year.")


def create_quotation(
    session: Session,
    *,
    enquiry: Enquiry,
    actor: User,
    package_id: UUID,
    package_price_id: UUID | None,
    travel_start_date: date,
    travel_end_date: date,
    adults: int,
    children: int,
    infants: int,
    accommodation: str,
    manual_adult_price: object = None,
    child_unit_price: object = None,
    infant_unit_price: object = None,
    manual_currency: str = "INR",
    manual_price_basis: str = "per_person_twin_sharing",
    gst_rate_percent: object = None,
    staff_price_confirmed: bool = False,
    inclusions: str = "",
    exclusions: str = "",
    payment_terms: str = "",
) -> Quotation:
    """Add one draft; the caller owns commit/rollback."""
    _check_actor(actor, enquiry)
    _travel_period(travel_start_date, travel_end_date)
    if adults < 1 or adults > 100 or not 0 <= children <= 100 or not 0 <= infants <= 100:
        raise QuotationError("Enter adults (1–100), children (0–100) and infants (0–100).")
    accommodation = accommodation.strip()
    if not accommodation or len(accommodation) > 160:
        raise QuotationError("Enter the accommodation category or arrangement.")
    if len(inclusions) > 6000 or len(exclusions) > 6000 or len(payment_terms) > 4000:
        raise QuotationError("Inclusions, exclusions or payment terms exceed the allowed length.")

    package = session.scalar(
        select(Package).options(selectinload(Package.prices)).where(
            Package.id == package_id, Package.status == "published"
        )
    )
    if package is None:
        raise QuotationError("Choose a currently published package.")

    price = None
    if package_price_id is not None:
        price = session.scalar(
            select(PackagePrice).where(
                PackagePrice.id == package_price_id, PackagePrice.package_id == package.id
            )
        )
        if price is None:
            raise QuotationError("The selected price does not belong to this package.")
        if price.price_basis not in {"per_person", "per_person_twin_sharing"}:
            raise QuotationError("Only verified per-person package prices can seed this quotation.")
        if not all(price.applies_on(date.fromordinal(day)) for day in range(travel_start_date.toordinal(), travel_end_date.toordinal() + 1)):
            raise QuotationError("The selected seasonal price does not cover the entire travel period.")
        adult_price = price.price_per_person
        currency = price.currency
        price_source = "package_price"
        price_basis = price.price_basis
        if manual_adult_price is not None and str(manual_adult_price).strip():
            raise QuotationError("Choose a package price or a staff-entered price, not both.")
    else:
        adult_price = _amount(manual_adult_price, "per-person price", required=True)
        currency = manual_currency.strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise QuotationError("Enter a three-letter currency code.")
        price_source = "manual"
        if manual_price_basis not in {"per_person", "per_person_twin_sharing"}:
            raise QuotationError("Choose a supported per-person price basis.")
        price_basis = manual_price_basis
        if not staff_price_confirmed:
            raise QuotationError("Confirm that the manually entered rate is staff-approved.")

    child_price = _amount(child_unit_price, "child fare")
    infant_price = _amount(infant_unit_price, "infant fare")
    has_manual_fares = child_price is not None or infant_price is not None
    if has_manual_fares and not staff_price_confirmed:
        raise QuotationError("Confirm staff approval of manually entered child or infant fares.")
    gst_rate = _amount(gst_rate_percent, "GST rate")
    if gst_rate is not None and gst_rate > 100:
        raise QuotationError("GST rate must be between 0 and 100 percent.")
    calculation = calculate_quote(
        adults=adults, children=children, infants=infants,
        adult_unit_price=adult_price, child_unit_price=child_price,
        infant_unit_price=infant_price, gst_rate_percent=gst_rate,
    )
    quote = Quotation(
        enquiry_id=enquiry.id,
        package_id=package.id,
        package_price_id=price.id if price else None,
        created_by_id=actor.id,
        status="draft",
        package_code_snapshot=package.package_code,
        package_title_snapshot=package.title,
        customer_name_snapshot=(enquiry.customer_name or "Traveller").strip(),
        travel_start_date=travel_start_date,
        travel_end_date=travel_end_date,
        adults=adults,
        children=children,
        infants=infants,
        accommodation=accommodation,
        currency=currency,
        price_source=price_source,
        price_basis=price_basis,
        adult_unit_price=adult_price,
        child_unit_price=child_price,
        infant_unit_price=infant_price,
        staff_price_confirmed=staff_price_confirmed,
        gst_rate_percent=gst_rate,
        subtotal_amount=calculation.subtotal,
        gst_amount=calculation.gst,
        total_amount=calculation.total,
        inclusions=inclusions.strip() or None,
        exclusions=exclusions.strip() or None,
        payment_terms=payment_terms.strip() or None,
    )
    session.add(quote)
    session.flush()
    return quote


def approve_quotation(session: Session, *, quote_id: UUID, actor: User, expected_updated_at: str) -> Quotation:
    if not actor.is_active or actor.role != "admin":
        raise QuotationError("Administrator approval is required.", 403)
    quote = session.scalar(select(Quotation).where(Quotation.id == quote_id).with_for_update())
    if quote is None:
        raise QuotationError("Quotation not found.", 404)
    if quote.updated_at.isoformat() != expected_updated_at:
        raise QuotationError("Quotation changed. Reload before approving.", 409)
    if quote.status != "draft":
        raise QuotationError("Only a draft quotation can be approved.", 409)
    if quote.total_amount is None:
        raise QuotationError("A complete total requires a confirmed fare for every traveller before approval.", 409)
    if quote.price_source == "manual" and not quote.staff_price_confirmed:
        raise QuotationError("A manually entered package rate must be staff-confirmed before approval.", 409)
    quote.status = "approved"
    quote.approved_by_id = actor.id
    quote.approved_at = datetime.now(timezone.utc)
    session.flush()
    return quote


def get_quotation_for_staff(session: Session, quote_id: UUID, actor: User) -> Quotation | None:
    if not actor.is_active or actor.role not in {"admin", "editor", "consultant"}:
        return None
    statement = (
        select(Quotation)
        .join(Enquiry, Enquiry.id == Quotation.enquiry_id)
        .where(Quotation.id == quote_id)
        .options(selectinload(Quotation.enquiry), selectinload(Quotation.package),
                 selectinload(Quotation.created_by), selectinload(Quotation.approved_by))
    )
    if actor.role == "consultant":
        statement = statement.where(Enquiry.assigned_to_id == actor.id)
    return session.scalar(statement)


def issue_share_token(session: Session, *, quote_id: UUID, actor: User) -> tuple[Quotation, str]:
    if not actor.is_active or actor.role not in {"admin", "editor", "consultant"}:
        raise QuotationError("You are not allowed to share quotations.", 403)
    quote = get_quotation_for_staff(session, quote_id, actor)
    if quote is None:
        raise QuotationError("Quotation not found.", 404)
    if quote.status != "approved":
        raise QuotationError("Only an approved quotation can be shared.", 409)
    token = secrets.token_urlsafe(32)
    quote.share_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    quote.share_expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    session.flush()
    return quote, token


def get_quotation_by_share_token(session: Session, quote_id: UUID, token: str) -> Quotation | None:
    quote = session.get(Quotation, quote_id)
    if quote is None or quote.status != "approved" or not quote.share_token_hash or not quote.share_expires_at:
        return None
    if quote.share_expires_at <= datetime.now(timezone.utc):
        return None
    candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
    if not secrets.compare_digest(candidate, quote.share_token_hash):
        return None
    return quote
