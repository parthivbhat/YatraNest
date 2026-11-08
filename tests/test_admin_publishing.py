"""PostgreSQL-backed tests for admin publishing and unpublishing."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Package, PackageDraft, PackagePrice, PackageVersion, User
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


def staff_client(db: Session, role: str = "admin", *, follow_redirects: bool = False):
    staff = User(name="Publish Tester", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(staff)
    db.flush()
    auth, raw_token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=follow_redirects)
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client, staff, auth


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def add_draft(db: Session) -> Package:
    package = Package(
        package_code=f"DRAFT-{uuid4().hex[:8].upper()}",
        slug=f"draft-publish-{uuid4().hex[:8]}",
        title="Draft ready to publish",
        summary="Confirmed summary",
        description="Confirmed description",
        destinations=["Munnar", "Alleppey"],
        duration_days=6,
        hotel_category="4-star",
        hotel_details=[{"name": "Confirmed hotel", "location": "Alleppey"}],
        itinerary=[{"day": 1, "title": "Arrival", "description": "Staff confirmed"}],
        inclusions=["Breakfast"],
        map_url="https://maps.example.test/package",
        status="draft",
        prices=[
            PackagePrice(
                season_name="Peak season",
                start_month=10,
                end_month=3,
                valid_from=date(2026, 10, 1),
                valid_until=date(2027, 3, 31),
                price_per_person=Decimal("32500.00"),
                currency="INR",
                price_basis="per_person",
                price_note="Twin sharing",
            )
        ],
    )
    db.add(package)
    db.flush()
    return package


def add_published(db: Session) -> Package:
    package = add_draft(db)
    package.package_code = f"LIVE-{uuid4().hex[:8].upper()}"
    package.slug = f"live-publish-{uuid4().hex[:8]}"
    package.title = "Published live title"
    package.status = "published"
    package.published_at = datetime.now(UTC)
    db.flush()
    return package


def add_pending(db: Session, package: Package, *, title: str = "Proposed pending title", price: str = "45000.00") -> PackageDraft:
    payload = package.to_snapshot()
    payload["title"] = title
    payload["summary"] = "Pending summary"
    payload["description"] = "Pending description"
    peak = next(item for item in payload["prices"] if item["season_name"] == "Peak season")
    peak["price_per_person"] = price
    peak["valid_from"] = "2026-10-10"
    peak["valid_until"] = "2027-03-03"
    pending = PackageDraft(
        draft_payload=payload,
        base_updated_at=package.updated_at,
    )
    package.pending_draft = pending
    db.flush()
    db.refresh(pending)
    return pending


def version_count(db: Session, package_id) -> int:
    return db.scalar(
        select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == package_id)
    )


def test_admin_publishes_valid_new_draft_with_complete_version_snapshot(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session)
    package = add_draft(db_session)
    package_id, code, slug = package.id, package.package_code, package.slug
    expected_timestamp = package.updated_at.isoformat()
    try:
        review = client.get(f"/admin/packages/{package.id}/review")
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": expected_timestamp},
        )
    finally:
        close_client(client)

    assert review.status_code == 200
    assert 'name="expected_updated_at"' in review.text
    assert response.status_code == 303
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    version = db_session.scalar(select(PackageVersion).where(PackageVersion.package_id == package_id))
    assert package.status == "published"
    assert package.published_at is not None
    assert package.updated_by_id == staff.id
    assert package.package_code == code and package.slug == slug
    assert package.title == "Draft ready to publish"
    assert len(package.prices) == 1
    assert version is not None
    assert version.created_by_id == staff.id
    assert version.snapshot["status"] == "published"
    assert version.snapshot["package_code"] == code
    assert version.snapshot["slug"] == slug
    assert version.snapshot["title"] == package.title
    assert version.snapshot["prices"][0]["price_per_person"] == "32500.00"
    assert version.snapshot["prices"][0]["valid_from"] == "2026-10-01"


def test_admin_publishes_pending_content_prices_and_deletes_pending_atomically(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session)
    package = add_published(db_session)
    pending = add_pending(db_session, package)
    db_session.flush()
    package_id, code, slug = package.id, package.package_code, package.slug
    expected_base = pending.base_updated_at.isoformat()
    expected_pending = pending.updated_at.isoformat()
    try:
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={
                "csrf_token": auth.csrf_token,
                "expected_base_updated_at": expected_base,
                "expected_draft_updated_at": expected_pending,
            },
        )
    finally:
        close_client(client)

    assert response.status_code == 303
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    version = db_session.scalar(select(PackageVersion).where(PackageVersion.package_id == package_id))
    assert package.status == "published"
    assert package.package_code == code and package.slug == slug
    assert package.title == "Proposed pending title"
    assert package.summary == "Pending summary"
    assert package.description == "Pending description"
    assert package.updated_by_id == staff.id
    peak = next(price for price in package.prices if price.season_name == "Peak season")
    assert peak.price_per_person == Decimal("45000.00")
    assert peak.valid_from == date(2026, 10, 10)
    assert peak.valid_until == date(2027, 3, 3)
    assert package.pending_draft is None
    assert version is not None
    assert version.snapshot["title"] == "Proposed pending title"
    assert next(price for price in version.snapshot["prices"] if price["season_name"] == "Peak season")["price_per_person"] == "45000.00"


@pytest.mark.parametrize("kind", ["draft_without_prices", "pending_invalid_month"])
def test_invalid_or_unpriced_content_cannot_be_published(db_session: Session, kind: str) -> None:
    client, auth_user, auth = staff_client(db_session)
    if kind == "draft_without_prices":
        package = add_draft(db_session)
        package.prices.clear()
        db_session.flush()
        data = {"expected_updated_at": package.updated_at.isoformat()}
    else:
        package = add_published(db_session)
        pending = add_pending(db_session, package)
        invalid_payload = pending.draft_payload.copy()
        invalid_payload["prices"] = [dict(price) for price in pending.draft_payload["prices"]]
        invalid_payload["prices"][0]["start_month"] = 13
        pending.draft_payload = invalid_payload
        db_session.flush()
        data = {
            "expected_base_updated_at": pending.base_updated_at.isoformat(),
            "expected_draft_updated_at": pending.updated_at.isoformat(),
        }
    package_id = package.id
    original_title = package.title
    try:
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={"csrf_token": auth.csrf_token, **data},
        )
    finally:
        close_client(client)
    assert response.status_code == 422
    assert "Review the package details and prices" in response.text
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.title == original_title
    assert package.status == ("draft" if kind == "draft_without_prices" else "published")
    assert version_count(db_session, package_id) == 0
    if kind == "pending_invalid_month":
        assert package.pending_draft is not None


def test_stale_draft_timestamp_returns_409_without_publishing(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = add_draft(db_session)
    package_id = package.id
    stale = (package.updated_at - timedelta(seconds=1)).isoformat()
    try:
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": stale},
        )
    finally:
        close_client(client)
    assert response.status_code == 409
    db_session.refresh(package)
    assert package.status == "draft"
    assert version_count(db_session, package_id) == 0


@pytest.mark.parametrize("stale_field", ["expected_base_updated_at", "expected_draft_updated_at"])
def test_stale_pending_timestamp_returns_409_and_preserves_pending(db_session: Session, stale_field: str) -> None:
    client, _, auth = staff_client(db_session)
    package = add_published(db_session)
    pending = add_pending(db_session, package)
    package_id = package.id
    data = {
        "csrf_token": auth.csrf_token,
        "expected_base_updated_at": pending.base_updated_at.isoformat(),
        "expected_draft_updated_at": pending.updated_at.isoformat(),
    }
    data[stale_field] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    try:
        response = client.post(f"/admin/packages/{package.id}/publish", data=data)
    finally:
        close_client(client)
    assert response.status_code == 409
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.status == "published"
    assert package.title == "Published live title"
    assert package.pending_draft is not None
    assert version_count(db_session, package_id) == 0


def test_version_creation_failure_rolls_back_publish_and_keeps_pending(db_session: Session, monkeypatch) -> None:
    import app.services.package_publishing as publishing_service

    client, _, auth = staff_client(db_session)
    package = add_published(db_session)
    pending = add_pending(db_session, package)
    package_id = package.id
    db_session.refresh(package)
    old_snapshot = package.to_snapshot()
    pending_payload = pending.draft_payload.copy()

    def fail_version(*args, **kwargs):
        raise IntegrityError("forced version insert failure", {}, RuntimeError("snapshot failure"))

    monkeypatch.setattr(publishing_service, "append_package_version", fail_version)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={
                "csrf_token": auth.csrf_token,
                "expected_base_updated_at": pending.base_updated_at.isoformat(),
                "expected_draft_updated_at": pending.updated_at.isoformat(),
            },
        )
    finally:
        close_client(client)
    assert response.status_code == 409
    assert "No changes were saved" in response.text
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.status == "published"
    assert package.to_snapshot() == old_snapshot
    assert package.pending_draft is not None
    assert package.pending_draft.draft_payload == pending_payload
    assert version_count(db_session, package_id) == 0


def test_admin_unpublishes_and_records_complete_draft_snapshot(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session)
    package = add_published(db_session)
    db_session.refresh(package)
    package_id = package.id
    before = package.to_snapshot()
    expected_updated_at = package.updated_at.isoformat()
    try:
        response = client.post(
            f"/admin/packages/{package.id}/unpublish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": expected_updated_at},
        )
        public_detail = client.get(f"/packages/{package.slug}")
        public_list = client.get("/packages")
        sitemap = client.get("/sitemap.xml")
    finally:
        close_client(client)

    assert response.status_code == 303
    assert public_detail.status_code == 404
    assert package.slug not in public_list.text
    assert package.slug not in sitemap.text
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    version = db_session.scalar(select(PackageVersion).where(PackageVersion.package_id == package_id))
    assert package.status == "draft"
    assert package.published_at is None
    assert package.updated_by_id == staff.id
    assert package.title == before["title"]
    assert [price.to_snapshot() for price in package.prices] == before["prices"]
    assert version is not None
    assert version.snapshot["status"] == "draft"
    assert version.snapshot["published_at"] is None
    assert version.snapshot["title"] == before["title"]
    assert version.snapshot["prices"] == before["prices"]


def test_unpublish_with_pending_changes_returns_409_and_preserves_everything(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = add_published(db_session)
    pending = add_pending(db_session, package)
    package_id = package.id
    db_session.refresh(package)
    before = package.to_snapshot()
    payload = pending.draft_payload.copy()
    try:
        response = client.post(
            f"/admin/packages/{package.id}/unpublish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert response.status_code == 409
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.to_snapshot() == before
    assert package.pending_draft is not None
    assert package.pending_draft.draft_payload == payload
    assert version_count(db_session, package_id) == 0


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_only_admin_can_publish_or_unpublish(db_session: Session, role: str) -> None:
    package = add_draft(db_session)
    published = add_published(db_session)
    client, _, auth = staff_client(db_session, role)
    try:
        publish = client.post(
            f"/admin/packages/{package.id}/publish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
        unpublish = client.post(
            f"/admin/packages/{published.id}/unpublish",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": published.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert publish.status_code == unpublish.status_code == 403


@pytest.mark.parametrize("endpoint", ["publish", "unpublish"])
def test_unauthenticated_publish_actions_redirect_to_login(db_session: Session, endpoint: str) -> None:
    package = add_published(db_session) if endpoint == "unpublish" else add_draft(db_session)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        response = client.post(f"/admin/packages/{package.id}/{endpoint}", data={"csrf_token": "any"})
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("endpoint", ["publish", "unpublish"])
@pytest.mark.parametrize("csrf", [None, "invalid"])
def test_publish_actions_require_valid_csrf(db_session: Session, endpoint: str, csrf: str | None) -> None:
    package = add_published(db_session) if endpoint == "unpublish" else add_draft(db_session)
    client, _, auth = staff_client(db_session)
    token = csrf or auth.csrf_token
    data = {"csrf_token": token, "expected_updated_at": package.updated_at.isoformat()}
    if csrf is None:
        data.pop("csrf_token")
    try:
        response = client.post(f"/admin/packages/{package.id}/{endpoint}", data=data)
    finally:
        close_client(client)
    assert response.status_code == 403
    db_session.refresh(package)
    assert package.status == ("published" if endpoint == "unpublish" else "draft")
    assert version_count(db_session, package.id) == 0


def test_failed_publish_keeps_existing_public_package_visible(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = add_published(db_session)
    pending = add_pending(db_session, package)
    package_id = package.id
    try:
        response = client.post(
            f"/admin/packages/{package.id}/publish",
            data={
                "csrf_token": auth.csrf_token,
                "expected_base_updated_at": pending.base_updated_at.isoformat(),
                "expected_draft_updated_at": (pending.updated_at - timedelta(seconds=1)).isoformat(),
            },
        )
        public = client.get(f"/packages/{package.slug}")
    finally:
        close_client(client)
    assert response.status_code == 409
    assert public.status_code == 200
    assert "Published live title" in public.text
    assert "Proposed pending title" not in public.text
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.pending_draft is not None
    assert version_count(db_session, package_id) == 0
