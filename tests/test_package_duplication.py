"""PostgreSQL-backed package duplication behavior and access control."""

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


def staff_client(db: Session, role: str = "admin", *, follow_redirects: bool = False):
    staff = User(name="Duplicate Tester", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(staff)
    db.flush()
    auth_session, token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=follow_redirects)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return client, staff, auth_session


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def add_package(db: Session, status: str) -> Package:
    package = Package(
        package_code=f"SRC-{uuid4().hex[:10].upper()}",
        slug=f"original-tour-{uuid4().hex[:8]}",
        title="Munnar and Alleppey Escape",
        summary="Staff supplied short summary",
        description="Staff supplied package description",
        destinations=["Munnar", "Alleppey"],
        duration_days=6,
        hotel_category="4-star",
        hotel_details=[{"name": "Confirmed hotel", "location": "Alleppey"}],
        itinerary=[{"day": 1, "title": "Arrival", "description": "Staff supplied itinerary"}],
        inclusions=["Breakfast"],
        map_url="https://maps.example.test/tour",
        status=status,
        published_at=datetime(2026, 1, 1, tzinfo=UTC) if status == "published" else None,
        scheduled_publish_at=datetime(2026, 2, 1, tzinfo=UTC) if status == "draft" else None,
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
            ),
            PackagePrice(
                season_name="Monsoon season",
                start_month=6,
                end_month=9,
                price_per_person=Decimal("24900.00"),
                currency="INR",
                price_basis="per_person",
            ),
        ],
    )
    db.add(package)
    db.flush()
    return package


@pytest.mark.parametrize("status", ["draft", "published", "archived"])
def test_staff_can_duplicate_each_package_status_with_content_prices_and_new_identity(db_session: Session, status: str) -> None:
    client, staff, auth = staff_client(db_session, "editor")
    source = add_package(db_session, status)
    source_id = source.id
    original_code, original_slug = source.package_code, source.slug
    db_session.refresh(source)
    original_snapshot = source.to_snapshot()
    try:
        response = client.post(
            f"/admin/packages/{source.id}/duplicate",
            data={"csrf_token": auth.csrf_token},
        )
    finally:
        close_client(client)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/admin/packages/")
    duplicate_id = response.headers["location"].split("/")[3]
    duplicate = db_session.get(Package, duplicate_id)
    db_session.refresh(source)
    assert duplicate is not None
    assert duplicate.id != source_id
    assert duplicate.package_code.startswith("DUP-") and duplicate.package_code != original_code
    assert duplicate.slug != original_slug and duplicate.slug.endswith("-copy")
    assert duplicate.status == "draft"
    assert duplicate.published_at is None and duplicate.scheduled_publish_at is None
    assert duplicate.created_by_id == duplicate.updated_by_id == staff.id
    assert duplicate.created_at >= source.created_at
    assert duplicate.title == source.title
    assert duplicate.summary == source.summary
    assert duplicate.description == source.description
    assert duplicate.destinations == source.destinations
    assert duplicate.hotel_details == source.hotel_details
    assert duplicate.itinerary == source.itinerary
    assert duplicate.inclusions == source.inclusions
    assert duplicate.map_url == source.map_url
    assert [price.to_snapshot() for price in duplicate.prices] == [price.to_snapshot() for price in source.prices]
    assert duplicate.versions == []
    assert duplicate.pending_draft is None
    assert source.to_snapshot() == original_snapshot


def test_duplicate_code_and_slug_are_unique_when_repeating_a_duplicate(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    source = add_package(db_session, "published")
    try:
        first = client.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": auth.csrf_token})
        second = client.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": auth.csrf_token})
    finally:
        close_client(client)
    assert first.status_code == second.status_code == 303
    one = db_session.get(Package, first.headers["location"].split("/")[3])
    two = db_session.get(Package, second.headers["location"].split("/")[3])
    assert one.package_code != two.package_code
    assert one.slug != two.slug


def test_pending_draft_and_version_history_are_not_copied(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session)
    source = add_package(db_session, "published")
    source.updated_by_id = staff.id
    live_snapshot = source.to_snapshot()
    pending_payload = deepcopy(live_snapshot)
    pending_payload["title"] = "Pending private title"
    pending_peak = next(price for price in pending_payload["prices"] if price["season_name"] == "Peak season")
    pending_peak["price_per_person"] = "35000.00"
    source.pending_draft = PackageDraft(
        draft_payload=pending_payload,
        base_updated_at=source.updated_at,
        created_by_id=staff.id,
        updated_by_id=staff.id,
    )
    source.versions.append(
        PackageVersion(
            version_number=1,
            snapshot=source.to_snapshot(),
            change_note="Existing source history",
            created_by_id=staff.id,
        )
    )
    db_session.flush()
    db_session.refresh(source)
    original_live_snapshot = source.to_snapshot()
    original_pending_payload = deepcopy(source.pending_draft.draft_payload)
    source_id = source.id
    try:
        response = client.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": auth.csrf_token})
    finally:
        close_client(client)
    assert response.status_code == 303
    db_session.expire_all()
    source = db_session.get(Package, source_id)
    duplicate = db_session.get(Package, response.headers["location"].split("/")[3])
    assert source is not None and duplicate is not None
    assert duplicate.pending_draft is None
    assert duplicate.versions == []
    assert duplicate.title == "Munnar and Alleppey Escape"
    duplicate_peak = next(price for price in duplicate.prices if price.season_name == "Peak season")
    assert duplicate_peak.price_per_person == Decimal("32500.00")
    assert duplicate.title != original_pending_payload["title"]
    assert source.to_snapshot() == original_live_snapshot
    assert source.pending_draft is not None
    assert source.pending_draft.draft_payload == original_pending_payload


def test_consultant_is_forbidden_and_unauthenticated_request_redirects(db_session: Session) -> None:
    source = add_package(db_session, "draft")
    original_count = db_session.scalar(select(func.count()).select_from(Package))
    consultant, _, auth = staff_client(db_session, "consultant")
    try:
        denied = consultant.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": auth.csrf_token})
    finally:
        close_client(consultant)
    app.dependency_overrides[get_db] = lambda: db_session
    anonymous = TestClient(app, follow_redirects=False)
    try:
        denied_anonymous = anonymous.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": "anything"})
    finally:
        anonymous.close()
        app.dependency_overrides.pop(get_db, None)
    assert denied.status_code == 403
    assert denied_anonymous.status_code == 303
    assert "/login" in denied_anonymous.headers["location"]
    assert db_session.scalar(select(func.count()).select_from(Package)) == original_count


def test_dashboard_shows_duplicate_action_only_to_admins_and_editors(db_session: Session) -> None:
    package = add_package(db_session, "published")
    for role in ("admin", "editor"):
        client, _, _ = staff_client(db_session, role)
        try:
            response = client.get("/admin/packages")
        finally:
            close_client(client)
        assert f'action="/admin/packages/{package.id}/duplicate"' in response.text

    consultant, _, _ = staff_client(db_session, "consultant")
    try:
        response = consultant.get("/admin/packages")
    finally:
        close_client(consultant)
    assert f'action="/admin/packages/{package.id}/duplicate"' not in response.text


@pytest.mark.parametrize("csrf", [None, "invalid"])
def test_missing_or_invalid_csrf_does_not_duplicate(db_session: Session, csrf: str | None) -> None:
    client, _, auth = staff_client(db_session)
    source = add_package(db_session, "archived")
    original_count = db_session.scalar(select(func.count()).select_from(Package))
    data = {"csrf_token": csrf or auth.csrf_token}
    if csrf is None:
        data.pop("csrf_token")
    try:
        response = client.post(f"/admin/packages/{source.id}/duplicate", data=data)
    finally:
        close_client(client)
    assert response.status_code == 403
    assert db_session.scalar(select(func.count()).select_from(Package)) == original_count


def test_database_failure_rolls_back_new_package_and_all_prices(monkeypatch, db_session: Session) -> None:
    import app.routes.admin as admin_routes

    client, _, auth = staff_client(db_session)
    source = add_package(db_session, "published")
    original_package_count = db_session.scalar(select(func.count()).select_from(Package))
    original_price_count = db_session.scalar(select(func.count()).select_from(PackagePrice))
    original_duplicate = admin_routes.duplicate_package

    def fail_after_duplicate_flush(session, package, staff):
        duplicate = original_duplicate(session, package, staff)
        session.add(
            PackagePrice(
                package_id=duplicate.id,
                season_name="Invalid test row",
                start_month=13,
                end_month=12,
                price_per_person=Decimal("100.00"),
            )
        )
        session.flush()
        return duplicate

    monkeypatch.setattr(admin_routes, "duplicate_package", fail_after_duplicate_flush)
    try:
        response = client.post(f"/admin/packages/{source.id}/duplicate", data={"csrf_token": auth.csrf_token})
    finally:
        close_client(client)
    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(Package)) == original_package_count
    assert db_session.scalar(select(func.count()).select_from(PackagePrice)) == original_price_count
    db_session.refresh(source)
    assert source.title == "Munnar and Alleppey Escape"
