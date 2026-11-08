from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import (
    Enquiry,
    EnquiryActivity,
    EnquiryNote,
    EnquiryReply,
    Package,
    PackagePrice,
    User,
)
from app.services.enquiry_activity import append_enquiry_activity
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


@pytest.fixture
def staff_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None) -> TestClient:
        client = TestClient(app, follow_redirects=False)
        if user is not None:
            _, token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def add_user(db: Session, role: str, label: str | None = None) -> User:
    user = User(
        name=label or f"{role.title()} {uuid4().hex[:5]}",
        email=f"{uuid4()}@example.test",
        role=role,
    )
    db.add(user)
    db.flush()
    return user


def add_package(db: Session, *, status: str = "published") -> Package:
    suffix = uuid4().hex[:8]
    package = Package(
        package_code=f"INBOX-{suffix}",
        slug=f"inbox-package-{suffix}",
        title="Munnar and Alleppey houseboat journey",
        destinations=["Munnar", "Alleppey"],
        duration_days=6,
        status=status,
    )
    package.prices.append(
        PackagePrice(
            season_name="Peak season",
            start_month=10,
            end_month=3,
            price_per_person=Decimal("32500.00"),
        )
    )
    db.add(package)
    db.flush()
    return package


def add_enquiry(
    db: Session,
    *,
    package: Package | None,
    assigned_to: User | None,
    status: str = "new",
    name: str = "Nila Traveller",
    message: str = "Please plan a Kerala houseboat holiday.",
    created_at: datetime | None = None,
) -> Enquiry:
    enquiry = Enquiry(
        package=package,
        assigned_to=assigned_to,
        customer_name=name,
        customer_email=("nila@example.test" if name == "Nila Traveller" else f"{uuid4().hex[:8]}@example.test"),
        customer_phone=("+91 98765 43210" if name == "Nila Traveller" else "+91 90000 00000"),
        original_message=message,
        status=status,
        travel_start_date=date(2027, 12, 10),
        travel_end_date=date(2027, 12, 17),
        travel_dates_flexible=True,
        adults=2,
        children=1,
        infants=0,
        budget_amount_inr=Decimal("120000.00"),
        response_notice_version="m6-v1",
        response_notice_accepted_at=datetime(2026, 11, 6, 10, 0, tzinfo=UTC),
        whatsapp_consent=True,
        whatsapp_consent_at=datetime(2026, 11, 6, 10, 1, tzinfo=UTC),
    )
    if created_at:
        enquiry.created_at = created_at
    db.add(enquiry)
    db.flush()
    return enquiry


def test_admin_and_editor_can_view_all_including_unassigned(
    db_session: Session, staff_client_factory, supplied_packages
) -> None:
    admin = add_user(db_session, "admin")
    editor = add_user(db_session, "editor")
    consultant = add_user(db_session, "consultant")
    assigned = add_enquiry(db_session, package=supplied_packages["kerala"], assigned_to=consultant)
    unassigned = add_enquiry(db_session, package=None, assigned_to=None, name="Unassigned Guest")

    for staff in (admin, editor):
        client = staff_client_factory(staff)
        try:
            response = client.get("/admin/enquiries")
        finally:
            client.close()
        assert response.status_code == 200
        assert "Nila Traveller" in response.text
        assert "Unassigned Guest" in response.text
        assert str(assigned.id) in response.text
        assert str(unassigned.id) in response.text


def test_consultant_only_sees_assigned_enquiries_in_list_and_detail(
    db_session: Session, staff_client_factory, supplied_packages
) -> None:
    consultant = add_user(db_session, "consultant", "Assigned Consultant")
    other = add_user(db_session, "consultant", "Other Consultant")
    own = add_enquiry(db_session, package=supplied_packages["kerala"], assigned_to=consultant)
    add_enquiry(db_session, package=supplied_packages["kerala"], assigned_to=other, name="Other Traveller")
    unassigned = add_enquiry(db_session, package=None, assigned_to=None, name="Queue Traveller")
    client = staff_client_factory(consultant)
    try:
        inbox = client.get("/admin/enquiries")
        own_detail = client.get(f"/admin/enquiries/{own.id}")
        denied_other = client.get(f"/admin/enquiries/{unassigned.id}")
        other_id = db_session.scalar(select(Enquiry.id).where(Enquiry.customer_name == "Other Traveller"))
        denied_other_assigned = client.get(f"/admin/enquiries/{other_id}")
    finally:
        client.close()

    assert inbox.status_code == 200
    assert "Nila Traveller" in inbox.text
    assert "Other Traveller" not in inbox.text
    assert "Queue Traveller" not in inbox.text
    assert own_detail.status_code == 200
    assert denied_other.status_code == denied_other_assigned.status_code == 404


def test_unauthenticated_inbox_redirects_and_invalid_record_ids_are_not_found(
    db_session: Session, staff_client_factory
) -> None:
    anonymous = staff_client_factory(None)
    try:
        inbox = anonymous.get("/admin/enquiries")
    finally:
        anonymous.close()
    client = staff_client_factory(add_user(db_session, "admin"))
    try:
        malformed = client.get("/admin/enquiries/not-a-uuid")
        missing = client.get(f"/admin/enquiries/{uuid4()}")
    finally:
        client.close()
    assert inbox.status_code == 303
    assert inbox.headers["location"] == "/admin/login"
    assert malformed.status_code == 404
    assert missing.status_code == 404


@pytest.mark.parametrize(
    "query",
    ["Nila Traveller", "nila@example.test", "98765 43210", "houseboat holiday"],
)
def test_inbox_search_covers_contact_fields_and_original_message(
    db_session: Session, staff_client_factory, query: str
) -> None:
    staff = add_user(db_session, "admin")
    target = add_enquiry(
        db_session,
        package=None,
        assigned_to=None,
        name="Nila Traveller",
        message="Please plan a Kerala houseboat holiday.",
    )
    add_enquiry(db_session, package=None, assigned_to=None, name="Other Traveller", message="Safari request.")
    client = staff_client_factory(staff)
    try:
        response = client.get("/admin/enquiries", params={"q": query})
    finally:
        client.close()

    assert response.status_code == 200
    assert "Nila Traveller" in response.text
    assert "Other Traveller" not in response.text
    assert str(target.id) in response.text


def test_status_assignment_filters_and_stable_pagination(
    db_session: Session, staff_client_factory
) -> None:
    admin = add_user(db_session, "admin")
    consultant = add_user(db_session, "consultant", "Filter Consultant")
    base = datetime(2026, 11, 6, tzinfo=UTC)
    first = add_enquiry(db_session, package=None, assigned_to=None, status="new", name="First", created_at=base)
    second = add_enquiry(
        db_session,
        package=None,
        assigned_to=consultant,
        status="in_progress",
        name="Second",
        created_at=base + timedelta(minutes=1),
    )
    third = add_enquiry(
        db_session,
        package=None,
        assigned_to=consultant,
        status="in_progress",
        name="Third",
        created_at=base + timedelta(minutes=2),
    )
    client = staff_client_factory(admin)
    try:
        unassigned = client.get("/admin/enquiries", params={"assigned_to": "unassigned"})
        by_consultant = client.get("/admin/enquiries", params={"assigned_to": str(consultant.id)})
        by_status = client.get("/admin/enquiries", params={"status": "in_progress"})
        first_page = client.get("/admin/enquiries", params={"page_size": 1})
        second_page = client.get("/admin/enquiries", params={"page_size": 1, "page": 2})
    finally:
        client.close()

    assert "First" in unassigned.text and "Second" not in unassigned.text
    assert "Second" in by_consultant.text and "First" not in by_consultant.text
    assert "Third" in by_status.text and "First" not in by_status.text
    assert third.customer_name in first_page.text and second.customer_name not in first_page.text
    assert second.customer_name in second_page.text and first.customer_name not in second_page.text
    assert f"/admin/enquiries?page_size=1&amp;page=2" in first_page.text


@pytest.mark.parametrize(
    "params",
    [
        {"status": "sent"},
        {"page_size": "101"},
        {"page": "0"},
        {"assigned_to": "not-a-uuid"},
    ],
)
def test_inbox_rejects_invalid_filters_and_pagination(db_session: Session, staff_client_factory, params) -> None:
    admin = add_user(db_session, "admin")
    client = staff_client_factory(admin)
    try:
        response = client.get("/admin/enquiries", params=params)
    finally:
        client.close()
    assert response.status_code == 422


def test_consultant_cannot_supply_another_assignment_filter(db_session: Session, staff_client_factory) -> None:
    consultant = add_user(db_session, "consultant")
    other = add_user(db_session, "consultant")
    client = staff_client_factory(consultant)
    try:
        response = client.get("/admin/enquiries", params={"assigned_to": str(other.id)})
    finally:
        client.close()
    assert response.status_code == 403


def test_workspace_shows_private_details_current_prices_activity_notes_and_replies(
    db_session: Session, staff_client_factory
) -> None:
    admin = add_user(db_session, "admin", "Audit Admin")
    consultant = add_user(db_session, "consultant", "Assigned Staff")
    package = add_package(db_session)
    enquiry = add_enquiry(
        db_session,
        package=package,
        assigned_to=consultant,
        message="Original request <script>alert('private')</script>\nKeep these exact words.",
    )
    note = EnquiryNote(enquiry=enquiry, author=admin, note="Internal only follow-up context.")
    reply = EnquiryReply(enquiry=enquiry, author=consultant, body="A reviewed reply draft.")
    db_session.add_all([note, reply])
    db_session.flush()
    append_enquiry_activity(
        db_session,
        enquiry,
        actor_id=admin.id,
        event_type="status_changed",
        from_status="new",
        to_status="in_progress",
    )
    client = staff_client_factory(admin)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()

    assert response.status_code == 200
    assert "Original request &lt;script&gt;alert(&#39;private&#39;)&lt;/script&gt;" in response.text
    assert "Original request <script>alert('private')</script>" not in response.text
    assert "10 Dec 2027" in response.text and "17 Dec 2027" in response.text
    assert "120000" not in response.text
    assert "₹1,20,000" in response.text
    assert "m6-v1" in response.text and "Consent recorded" in response.text
    assert "Peak season" in response.text and "₹32,500" in response.text
    assert "Published" in response.text
    assert "Internal only follow-up context." in response.text
    assert "A reviewed reply draft." in response.text and "Draft" in response.text
    assert "Status changed" in response.text and "Audit Admin" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_workspace_handles_package_set_null_and_private_pages_do_not_leak_publicly(
    db_session: Session, staff_client_factory
) -> None:
    admin = add_user(db_session, "admin")
    package = add_package(db_session)
    enquiry = add_enquiry(db_session, package=package, assigned_to=None, message="SECRET-ENQUIRY-MESSAGE")
    client = staff_client_factory(admin)
    try:
        public_page_before_delete = client.get(f"/packages/{package.slug}")
        db_session.delete(package)
        db_session.flush()
        assert enquiry.package_id is None
        private = client.get(f"/admin/enquiries/{enquiry.id}")
        public_listing = client.get("/packages")
        public_sitemap = client.get("/sitemap.xml")
    finally:
        client.close()

    assert public_page_before_delete.status_code == 200
    assert "SECRET-ENQUIRY-MESSAGE" not in public_page_before_delete.text
    assert private.status_code == 200
    assert "Package no longer available" in private.text
    assert "SECRET-ENQUIRY-MESSAGE" not in public_listing.text
    assert "SECRET-ENQUIRY-MESSAGE" not in public_sitemap.text


def test_admin_and_editor_can_open_workspace_with_workflow_actions(
    db_session: Session, staff_client_factory
) -> None:
    package = add_package(db_session)
    enquiry = add_enquiry(db_session, package=package, assigned_to=None)
    for role in ("admin", "editor"):
        staff = add_user(db_session, role)
        client = staff_client_factory(staff)
        try:
            response = client.get(f"/admin/enquiries/{enquiry.id}")
        finally:
            client.close()
        assert response.status_code == 200
        assert "Internal notes" in response.text
        assert "Add note" in response.text
        assert "Update status" in response.text
        if role == "admin":
            assert "/assignment" in response.text
        else:
            assert "/assignment" not in response.text
