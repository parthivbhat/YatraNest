"""PostgreSQL-backed access and presentation tests for private package review."""

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Package, PackageDraft, PackagePrice, PackageVersion, User, UserSession
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


@pytest.fixture
def db_session():
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def staff_client(db: Session, role: str = "admin"):
    staff = User(name="Review Tester", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(staff)
    db.flush()
    auth_session, raw_token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client, staff, auth_session


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def add_published_with_pending(db: Session, *, updated_by: User | None = None) -> Package:
    package = Package(
        package_code=f"REVIEW-{uuid4().hex[:8].upper()}",
        slug=f"review-{uuid4().hex[:8]}",
        title="Live package title",
        summary="Live summary",
        description="Live description",
        destinations=["Munnar"],
        duration_days=6,
        hotel_category="4-star",
        status="published",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        prices=[
            PackagePrice(
                season_name="Peak season",
                start_month=10,
                end_month=3,
                valid_from=date(2026, 10, 1),
                valid_until=date(2027, 3, 31),
                price_per_person=Decimal("32500.00"),
            ),
            PackagePrice(
                season_name="Monsoon season",
                start_month=6,
                end_month=9,
                price_per_person=Decimal("24900.00"),
            ),
        ],
    )
    db.add(package)
    db.flush()
    pending_payload = package.to_snapshot()
    pending_payload["title"] = "Proposed package title"
    pending_payload["summary"] = "Proposed summary"
    pending_payload["description"] = "Proposed description"
    peak = next(price for price in pending_payload["prices"] if price["season_name"] == "Peak season")
    peak["price_per_person"] = "35000.00"
    peak["valid_from"] = "2026-10-10"
    peak["valid_until"] = "2027-03-03"
    package.pending_draft = PackageDraft(
        draft_payload=pending_payload,
        base_updated_at=package.updated_at,
        created_by_id=updated_by.id if updated_by else None,
        updated_by_id=updated_by.id if updated_by else None,
    )
    db.flush()
    return package


def test_admin_review_compares_live_and_pending_content_and_prices(db_session: Session) -> None:
    client, admin, _ = staff_client(db_session, "admin")
    package = add_published_with_pending(db_session, updated_by=admin)
    db_session.refresh(package)
    package_id = package.id
    original_live = package.to_snapshot()
    original_pending = deepcopy(package.pending_draft.draft_payload)
    original_version_count = db_session.scalar(
        select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == package_id)
    )
    try:
        response = client.get(f"/admin/packages/{package.id}/review")
    finally:
        close_client(client)

    assert response.status_code == 200
    assert "Live package title" in response.text
    assert "Proposed package title" in response.text
    assert "Live description" in response.text
    assert "Proposed description" in response.text
    assert "₹32,500" in response.text
    assert "₹35,000" in response.text
    assert "01 Oct 2026 – 31 Mar 2027" in response.text
    assert "10 Oct 2026 – 03 Mar 2027" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.to_snapshot() == original_live
    assert package.pending_draft.draft_payload == original_pending
    assert db_session.scalar(
        select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == package_id)
    ) == original_version_count


def test_admin_can_review_new_draft_before_first_publication(db_session: Session) -> None:
    client, _, _ = staff_client(db_session, "admin")
    draft = Package(
        package_code=f"DRAFT-{uuid4().hex[:8].upper()}",
        slug=f"draft-{uuid4().hex[:8]}",
        title="First publication draft",
        status="draft",
        prices=[
            PackagePrice(
                season_name="All year",
                start_month=1,
                end_month=12,
                price_per_person=Decimal("18750.00"),
            )
        ],
    )
    db_session.add(draft)
    db_session.flush()
    try:
        response = client.get(f"/admin/packages/{draft.id}/review")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "First publication review" in response.text
    assert "First publication draft" in response.text
    assert "₹18,750" in response.text
    assert f"/admin/packages/{draft.id}/preview" in response.text
    assert "Publish package" in response.text
    assert "/publish" in response.text
    assert 'name="csrf_token"' in response.text


def test_admin_and_editor_can_preview_pending_values_with_typed_dates_and_prices(db_session: Session) -> None:
    package = add_published_with_pending(db_session)
    for role in ("admin", "editor"):
        client, _, _ = staff_client(db_session, role)
        try:
            response = client.get(f"/admin/packages/{package.id}/preview")
        finally:
            close_client(client)
        assert response.status_code == 200
        assert "Proposed package title" in response.text
        assert "₹35,000" in response.text
        assert "10 Oct 2026 – 03 Mar 2027" in response.text
        assert "Live package title" not in response.text
        assert '<meta name="robots" content="noindex, nofollow">' in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_preview_of_new_draft_uses_live_draft_content_without_mutating_it(db_session: Session) -> None:
    client, _, _ = staff_client(db_session, "editor")
    draft = Package(
        package_code=f"DRAFT-{uuid4().hex[:8].upper()}",
        slug=f"draft-{uuid4().hex[:8]}",
        title="Private new draft",
        status="draft",
        prices=[
            PackagePrice(
                season_name="Season",
                start_month=1,
                end_month=2,
                valid_from=date(2027, 1, 1),
                valid_until=date(2027, 2, 28),
                price_per_person=Decimal("12345.00"),
            )
        ],
    )
    db_session.add(draft)
    db_session.flush()
    original_updated_at = draft.updated_at
    try:
        response = client.get(f"/admin/packages/{draft.id}/preview")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "Private new draft" in response.text
    assert "12,345" in response.text
    assert "01 Jan 2027 – 28 Feb 2027" in response.text
    db_session.refresh(draft)
    assert draft.status == "draft"
    assert draft.updated_at == original_updated_at
    assert draft.pending_draft is None


def test_editor_and_consultant_cannot_review_and_consultant_cannot_preview(db_session: Session) -> None:
    package = add_published_with_pending(db_session)
    editor, _, _ = staff_client(db_session, "editor")
    try:
        review = editor.get(f"/admin/packages/{package.id}/review")
    finally:
        close_client(editor)
    consultant, _, _ = staff_client(db_session, "consultant")
    try:
        consultant_review = consultant.get(f"/admin/packages/{package.id}/review")
        preview = consultant.get(f"/admin/packages/{package.id}/preview")
    finally:
        close_client(consultant)
    assert review.status_code == consultant_review.status_code == preview.status_code == 403


@pytest.mark.parametrize("path", ["/review", "/preview"])
def test_unauthenticated_review_and_preview_redirect_to_staff_login(db_session: Session, path: str) -> None:
    package = add_published_with_pending(db_session)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        response = client.get(f"/admin/packages/{package.id}{path}")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_dashboard_shows_review_and_preview_only_to_eligible_staff(db_session: Session) -> None:
    package = add_published_with_pending(db_session)
    for role in ("admin", "editor"):
        client, _, _ = staff_client(db_session, role)
        try:
            response = client.get("/admin/packages")
        finally:
            close_client(client)
        preview_path = f"/admin/packages/{package.id}/preview"
        assert preview_path in response.text
        review_path = f"/admin/packages/{package.id}/review"
        assert (review_path in response.text) == (role == "admin")
        assert "Publish package" not in response.text

    consultant, _, _ = staff_client(db_session, "consultant")
    try:
        response = consultant.get("/admin/packages")
    finally:
        close_client(consultant)
    assert f"/admin/packages/{package.id}/preview" not in response.text
    assert f"/admin/packages/{package.id}/review" not in response.text


def test_public_pages_and_sitemap_continue_to_exclude_pending_values(db_session: Session) -> None:
    client, _, _ = staff_client(db_session, "editor")
    package = add_published_with_pending(db_session)
    try:
        detail = client.get(f"/packages/{package.slug}")
        listing = client.get("/packages")
        sitemap = client.get("/sitemap.xml")
    finally:
        close_client(client)
    assert detail.status_code == listing.status_code == sitemap.status_code == 200
    assert "Live package title" in detail.text
    assert "Proposed package title" not in detail.text
    assert "₹32,500" in detail.text
    assert "₹35,000" not in detail.text
    assert "Live package title" in listing.text
    assert "Proposed package title" not in listing.text
    assert "Proposed package title" not in sitemap.text
