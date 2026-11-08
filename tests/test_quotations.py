from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Enquiry, PackagePrice, Quotation, User, UserSession
from app.services.quotation_pdf import render_quotation_pdf
from app.services.quotations import (
    QuotationError,
    approve_quotation,
    calculate_quote,
    create_quotation,
    get_quotation_by_share_token,
    get_quotation_for_staff,
    issue_share_token,
)
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def _user(db: Session, role: str, *, active: bool = True) -> User:
    user = User(name=f"Quote {role}", email=f"{uuid4()}@example.test", role=role, is_active=active)
    db.add(user)
    db.flush()
    return user


def _enquiry(db: Session, package, assigned: User | None = None) -> Enquiry:
    enquiry = Enquiry(
        package=package,
        assigned_to=assigned,
        customer_name="Synthetic Quote Traveller",
        customer_email="synthetic@example.test",
        original_message="Synthetic quotation test enquiry.",
    )
    db.add(enquiry)
    db.flush()
    return enquiry


def _quote(db: Session, enquiry: Enquiry, actor: User, package, *, child_count: int = 0) -> Quotation:
    return create_quotation(
        db,
        enquiry=enquiry,
        actor=actor,
        package_id=package.id,
        package_price_id=package.prices[0].id,
        travel_start_date=date(2027, 12, 10),
        travel_end_date=date(2027, 12, 16),
        adults=2,
        children=child_count,
        infants=0,
        accommodation="4-star · twin sharing",
        child_unit_price="2500" if child_count else None,
        gst_rate_percent="5",
        staff_price_confirmed=bool(child_count),
    )


@pytest.fixture
def quotation_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None):
        client = TestClient(app, follow_redirects=False)
        csrf = None
        if user is not None:
            _, raw_token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            csrf = db_session.scalar(select(UserSession.csrf_token).where(UserSession.user_id == user.id))
            client.cookies.set(SESSION_COOKIE_NAME, raw_token)
        return client, csrf

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_calculate_quote_uses_decimal_arithmetic_and_explicit_fares():
    calculated = calculate_quote(
        adults=2,
        children=1,
        infants=0,
        adult_unit_price=Decimal("1000.00"),
        child_unit_price=Decimal("500.00"),
        infant_unit_price=None,
        gst_rate_percent=Decimal("5.00"),
    )
    assert calculated.subtotal == Decimal("2500.00")
    assert calculated.gst == Decimal("125.00")
    assert calculated.total == Decimal("2625.00")

    incomplete = calculate_quote(
        adults=2,
        children=1,
        infants=0,
        adult_unit_price=Decimal("1000.00"),
        child_unit_price=None,
        infant_unit_price=None,
        gst_rate_percent=None,
    )
    assert incomplete.total is None


def test_create_quote_snapshots_verified_price_and_requires_admin_approval(
    db_session: Session, supplied_packages
):
    package = supplied_packages["kerala"]
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, package)
    quote = _quote(db_session, enquiry, admin, package)

    assert quote.status == "draft"
    assert quote.created_by_id == admin.id
    assert quote.package_price_id == package.prices[0].id
    assert quote.adult_unit_price == Decimal("32500.00")
    assert quote.price_basis == package.prices[0].price_basis
    assert quote.total_amount == Decimal("68250.00")
    with pytest.raises(QuotationError, match="Administrator approval"):
        approve_quotation(db_session, quote_id=quote.id, actor=_user(db_session, "editor"), expected_updated_at=quote.updated_at.isoformat())


def test_child_fare_is_never_inferred_and_incomplete_quote_cannot_be_approved(
    db_session: Session, supplied_packages
):
    package = supplied_packages["kerala"]
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, package)
    quote = _quote(db_session, enquiry, admin, package, child_count=1)
    assert quote.child_unit_price == Decimal("2500.00")
    assert quote.subtotal_amount == Decimal("67500.00")

    quote.child_unit_price = None
    quote.subtotal_amount = None
    quote.gst_amount = None
    quote.total_amount = None
    db_session.flush()
    with pytest.raises(QuotationError, match="complete total"):
        approve_quotation(db_session, quote_id=quote.id, actor=admin, expected_updated_at=quote.updated_at.isoformat())


def test_consultant_quote_scope_is_limited_to_assigned_enquiry(db_session: Session, supplied_packages):
    package = supplied_packages["kerala"]
    consultant = _user(db_session, "consultant")
    other = _user(db_session, "consultant")
    enquiry = _enquiry(db_session, package, consultant)
    quote = _quote(db_session, enquiry, consultant, package)

    assert get_quotation_for_staff(db_session, quote.id, consultant) is quote
    assert get_quotation_for_staff(db_session, quote.id, other) is None
    with pytest.raises(QuotationError, match="outside your assigned"):
        _quote(db_session, _enquiry(db_session, package, other), consultant, package)


def test_approved_quote_share_token_and_pdf_are_private_and_branded(db_session: Session, supplied_packages):
    package = supplied_packages["kerala"]
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, package)
    quote = _quote(db_session, enquiry, admin, package)
    quote.status = "approved"
    quote.approved_by_id = admin.id
    quote.approved_at = datetime.now(timezone.utc)
    db_session.flush()

    quote, token = issue_share_token(db_session, quote_id=quote.id, actor=admin)
    db_session.flush()
    assert get_quotation_by_share_token(db_session, quote.id, token) is quote
    assert get_quotation_by_share_token(db_session, quote.id, "wrong-token") is None

    pdf = render_quotation_pdf(quote)
    assert pdf.startswith(b"%PDF-1.4")
    assert b"YatraNest" in pdf
    assert b"synthetic@example.test" not in pdf


def test_shareable_quote_requires_valid_bearer_token_and_is_no_store(
    db_session: Session, supplied_packages, quotation_client_factory
):
    package = supplied_packages["kerala"]
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, package)
    quote = _quote(db_session, enquiry, admin, package)
    quote.status = "approved"
    quote.approved_by_id = admin.id
    quote.approved_at = datetime.now(timezone.utc)
    db_session.flush()
    quote, token = issue_share_token(db_session, quote_id=quote.id, actor=admin)
    db_session.flush()

    client, _ = quotation_client_factory(None)
    try:
        invalid = client.get(f"/quotes/{quote.id}/invalid")
        valid = client.get(f"/quotes/{quote.id}/{token}")
        pdf = client.get(f"/quotes/{quote.id}/{token}/pdf")
    finally:
        client.close()
    assert invalid.status_code == 404
    assert valid.status_code == 200
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["x-robots-tag"] == "noindex, nofollow"
    assert valid.headers["referrer-policy"] == "no-referrer"
    assert "not a booking confirmation" in valid.text.lower()
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")


def test_staff_routes_create_quote_with_csrf_and_public_link_is_unlisted(
    db_session: Session, supplied_packages, quotation_client_factory
):
    package = supplied_packages["kerala"]
    peak_price = db_session.scalar(select(PackagePrice).where(
        PackagePrice.package_id == package.id, PackagePrice.season_name == "Peak season"
    ))
    editor = _user(db_session, "editor")
    enquiry = _enquiry(db_session, package)
    client, csrf = quotation_client_factory(editor)
    try:
        form = client.get(f"/admin/enquiries/{enquiry.id}/quotations/new")
        assert form.status_code == 200
        assert 'name="csrf_token"' in form.text
        assert "Synthetic Quote Traveller" in form.text
        workspace = client.get(f"/admin/enquiries/{enquiry.id}")
        assert workspace.status_code == 200
        assert f"/admin/enquiries/{enquiry.id}/quotations/new" in workspace.text

        rejected = client.post(
            f"/admin/enquiries/{enquiry.id}/quotations",
            data={"csrf_token": "wrong", "package_id": str(package.id)},
        )
        assert rejected.status_code in {400, 403}

        response = client.post(
            f"/admin/enquiries/{enquiry.id}/quotations",
            data={
                "csrf_token": csrf,
                "package_id": str(package.id),
                "package_price_id": str(peak_price.id),
                "travel_start_date": "2027-12-10",
                "travel_end_date": "2027-12-16",
                "adults": "2",
                "children": "0",
                "infants": "0",
                "accommodation": "4-star · twin sharing",
                "manual_currency": "INR",
                "manual_price_basis": "per_person_twin_sharing",
            },
        )
        assert response.status_code == 303, response.text
        quote = db_session.scalar(select(Quotation).where(Quotation.enquiry_id == enquiry.id))
        assert quote is not None and quote.status == "draft"
        assert quote.created_by_id == editor.id
    finally:
        client.close()


def test_quote_form_preselects_unique_verified_rate_and_saves_with_optional_fields_blank(
    db_session: Session, supplied_packages, quotation_client_factory
):
    package = supplied_packages["kerala"]
    peak_price = db_session.scalar(select(PackagePrice).where(
        PackagePrice.package_id == package.id, PackagePrice.season_name == "Peak season"
    ))
    editor = _user(db_session, "editor")
    enquiry = _enquiry(db_session, package)
    enquiry.travel_start_date = date(2027, 12, 10)
    enquiry.travel_end_date = date(2027, 12, 16)
    enquiry.travel_dates_flexible = False
    db_session.flush()

    client, csrf = quotation_client_factory(editor)
    try:
        form = client.get(f"/admin/enquiries/{enquiry.id}/quotations/new")
        assert form.status_code == 200
        assert f'<option value="{peak_price.id}" selected>' in form.text

        response = client.post(
            f"/admin/enquiries/{enquiry.id}/quotations",
            data={
                "csrf_token": csrf,
                "package_id": str(package.id),
                "package_price_id": str(peak_price.id),
                "travel_start_date": "2027-12-10",
                "travel_end_date": "2027-12-16",
                "adults": "2",
                "children": "0",
                "infants": "0",
                "accommodation": "4-star · twin sharing",
                "manual_adult_price": "",
                "child_unit_price": "",
                "infant_unit_price": "",
                "manual_currency": "INR",
                "manual_price_basis": "per_person_twin_sharing",
                "gst_rate_percent": "",
                "staff_price_confirmed": "",
                "inclusions": "",
                "exclusions": "",
                "payment_terms": "",
            },
        )
        assert response.status_code == 303, response.text
        quote = db_session.scalar(select(Quotation).where(Quotation.enquiry_id == enquiry.id))
        assert quote is not None and quote.status == "draft"
        assert quote.package_price_id == peak_price.id
        assert quote.adult_unit_price == peak_price.price_per_person
        assert quote.child_unit_price is None and quote.infant_unit_price is None
        assert quote.gst_rate_percent is None
        assert quote.inclusions is None and quote.exclusions is None and quote.payment_terms is None
    finally:
        client.close()


def test_admin_can_approve_with_csrf_and_stale_form_is_rejected(
    db_session: Session, supplied_packages, quotation_client_factory
):
    package = supplied_packages["kerala"]
    admin = _user(db_session, "admin")
    enquiry = _enquiry(db_session, package)
    quote = _quote(db_session, enquiry, admin, package)
    client, csrf = quotation_client_factory(admin)
    try:
        stale = client.post(
            f"/admin/quotations/{quote.id}/approve",
            data={"csrf_token": csrf, "expected_updated_at": "2000-01-01T00:00:00+00:00"},
        )
        assert stale.status_code == 409
        current = client.post(
            f"/admin/quotations/{quote.id}/approve",
            data={"csrf_token": csrf, "expected_updated_at": quote.updated_at.isoformat()},
        )
        assert current.status_code == 303
        assert quote.status == "approved"
        assert quote.approved_by_id == admin.id
        assert quote.approved_at is not None
    finally:
        client.close()
