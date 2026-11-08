"""PostgreSQL-backed tests for private edits to published packages."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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
    staff = User(name="Pending Draft Tester", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(staff)
    db.flush()
    auth, token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return client, staff, auth


def published_package(db: Session) -> Package:
    package = Package(
        package_code=f"PUB-{uuid4().hex[:10].upper()}",
        slug=f"published-{uuid4().hex[:10]}",
        title="Live published title",
        status="published",
        destinations=["Kerala"],
        duration_days=6,
        description="Live published description",
        prices=[
            PackagePrice(
                season_name="Peak season",
                start_month=10,
                end_month=3,
                price_per_person=Decimal("32500.00"),
            ),
            PackagePrice(
                season_name="Monsoon",
                start_month=6,
                end_month=9,
                price_per_person=Decimal("24900.00"),
            ),
        ],
    )
    db.add(package)
    db.flush()
    return package


def post_payload(csrf: str, package: Package, **overrides):
    payload = {
        "csrf_token": csrf,
        "expected_base_updated_at": package.updated_at.isoformat(),
        "expected_draft_updated_at": "",
        "title": "Proposed private title",
        "description": "Proposed private description",
        "destinations": "Kerala\nAlleppey",
        "duration_days": "7",
        "price_season_name": ["New peak"],
        "price_start_month": ["10"],
        "price_end_month": ["3"],
        "price_per_person": ["40000.00"],
        "price_valid_from": [""],
        "price_valid_until": [""],
        "price_note": [""],
    }
    payload.update(overrides)
    return payload


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def test_first_save_creates_complete_pending_payload_without_touching_live_rows(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session, "editor")
    package = published_package(db_session)
    package_id, original_updated_at = package.id, package.updated_at
    live_prices = sorted((p.season_name, p.start_month, p.end_month, p.price_per_person) for p in package.prices)
    try:
        response = client.post(f"/admin/packages/{package.id}/edit", data=post_payload(auth.csrf_token, package))
    finally:
        close_client(client)

    assert response.status_code == 303
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    pending = db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package_id))
    assert pending is not None
    assert pending.draft_payload["title"] == "Proposed private title"
    assert len(pending.draft_payload["prices"]) == 1
    assert pending.base_updated_at == original_updated_at
    assert pending.created_by_id == staff.id
    assert package.title == "Live published title"
    assert package.description == "Live published description"
    assert package.updated_at == original_updated_at
    assert sorted((p.season_name, p.start_month, p.end_month, p.price_per_person) for p in package.prices) == live_prices
    assert db_session.scalar(select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == package_id)) == 0


def test_existing_pending_draft_is_loaded_updated_and_keeps_original_base_timestamp(db_session: Session) -> None:
    client, staff, auth = staff_client(db_session)
    package = published_package(db_session)
    try:
        first = client.post(f"/admin/packages/{package.id}/edit", data=post_payload(auth.csrf_token, package))
        assert first.status_code == 303
        db_session.expire_all()
        pending = db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id))
        assert pending is not None
        original_base, original_id, original_pending_updated = pending.base_updated_at, pending.id, pending.updated_at
        page = client.get(f"/admin/packages/{package.id}/edit")
        assert "Proposed private title" in page.text
        update = client.post(
            f"/admin/packages/{package.id}/edit",
            data=post_payload(
                auth.csrf_token,
                package,
                expected_base_updated_at=original_base.isoformat(),
                expected_draft_updated_at=original_pending_updated.isoformat(),
                title="Second private title",
            ),
        )
    finally:
        close_client(client)

    assert update.status_code == 303
    db_session.expire_all()
    pending = db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id))
    assert pending.id == original_id
    assert pending.base_updated_at == original_base
    assert pending.updated_at > original_pending_updated
    assert pending.updated_by_id == staff.id
    assert pending.draft_payload["title"] == "Second private title"
    assert db_session.get(Package, package.id).title == "Live published title"


def test_stale_live_package_change_returns_409_and_preserves_pending_data(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = published_package(db_session)
    original_timestamp = package.updated_at
    try:
        page = client.get(f"/admin/packages/{package.id}/edit")
        data = post_payload(auth.csrf_token, package, expected_base_updated_at=original_timestamp.isoformat(), title="Stale")
        package.title = "Updated by another process"
        package.updated_at = datetime.now(UTC)
        db_session.flush()
        response = client.post(f"/admin/packages/{package.id}/edit", data=data)
    finally:
        close_client(client)
    assert page.status_code == 200
    assert response.status_code == 409
    assert "Reload before saving" in response.text
    assert db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id)) is None
    assert package.title == "Updated by another process"


def test_stale_pending_edit_returns_409_without_overwriting_newer_payload(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = published_package(db_session)
    try:
        first_form = client.get(f"/admin/packages/{package.id}/edit")
        first = client.post(f"/admin/packages/{package.id}/edit", data=post_payload(auth.csrf_token, package, title="First save"))
        assert first.status_code == 303
        db_session.expire_all()
        pending = db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id))
        old_pending_time = pending.updated_at
        stale_form_payload = post_payload(
            auth.csrf_token,
            package,
            expected_base_updated_at=pending.base_updated_at.isoformat(),
            expected_draft_updated_at=old_pending_time.isoformat(),
            title="Stale overwrite",
        )
        pending.draft_payload = {**pending.draft_payload, "title": "Concurrent newer save"}
        pending.updated_at = datetime.now(UTC)
        db_session.flush()
        stale = client.post(f"/admin/packages/{package.id}/edit", data=stale_form_payload)
    finally:
        close_client(client)
    assert first_form.status_code == 200
    assert stale.status_code == 409
    db_session.expire_all()
    pending = db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id))
    assert pending.draft_payload["title"] == "Concurrent newer save"


@pytest.mark.parametrize("role", ["admin", "editor"])
def test_admin_and_editor_can_open_published_package_for_editing(db_session: Session, role: str) -> None:
    client, _, _ = staff_client(db_session, role)
    package = published_package(db_session)
    try:
        response = client.get(f"/admin/packages/{package.id}/edit")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "Live published title" in response.text


def test_consultant_and_unauthenticated_users_cannot_edit_published_package(db_session: Session) -> None:
    package = published_package(db_session)
    consultant, _, auth = staff_client(db_session, "consultant")
    try:
        denied_get = consultant.get(f"/admin/packages/{package.id}/edit")
        denied_post = consultant.post(f"/admin/packages/{package.id}/edit", data=post_payload(auth.csrf_token, package))
    finally:
        close_client(consultant)
    app.dependency_overrides[get_db] = lambda: db_session
    anonymous = TestClient(app, follow_redirects=False)
    try:
        anonymous_get = anonymous.get(f"/admin/packages/{package.id}/edit")
        anonymous_post = anonymous.post(f"/admin/packages/{package.id}/edit", data=post_payload("bad", package))
    finally:
        anonymous.close()
        app.dependency_overrides.pop(get_db, None)
    assert denied_get.status_code == denied_post.status_code == 403
    assert anonymous_get.status_code == anonymous_post.status_code == 303
    assert "/login" in anonymous_get.headers["location"]


@pytest.mark.parametrize("csrf", [None, "invalid"])
def test_missing_or_invalid_csrf_is_rejected(db_session: Session, csrf: str | None) -> None:
    client, _, auth = staff_client(db_session)
    package = published_package(db_session)
    supplied = csrf if csrf is not None else auth.csrf_token
    data = post_payload(supplied, package)
    if csrf is None:
        data.pop("csrf_token")
    try:
        response = client.post(f"/admin/packages/{package.id}/edit", data=data)
    finally:
        close_client(client)
    assert response.status_code == 403
    assert db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id)) is None


def test_public_pages_continue_to_render_only_live_published_content(db_session: Session) -> None:
    client, _, auth = staff_client(db_session)
    package = published_package(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/edit",
            data=post_payload(auth.csrf_token, package, title="Secret pending title", price_per_person=["99999.00"]),
        )
        listing = client.get("/packages")
        detail = client.get(f"/packages/{package.slug}")
    finally:
        close_client(client)
    assert response.status_code == 303
    assert listing.status_code == detail.status_code == 200
    assert "Live published title" in listing.text and "Secret pending title" not in listing.text
    assert "Live published title" in detail.text and "Secret pending title" not in detail.text
    assert "32,500" in detail.text and "99,999" not in detail.text


def test_pending_write_constraint_failure_rolls_back(monkeypatch, db_session: Session) -> None:
    import app.routes.admin as admin_routes

    client, _, auth = staff_client(db_session)
    package = published_package(db_session)
    original_updated_at = package.updated_at

    def broken_save(session, package, payload, staff, *, existing_draft):
        session.add(
            PackageDraft(
                package_id=package.id,
                draft_payload=[],  # Violates the PostgreSQL JSON object check constraint.
                base_updated_at=package.updated_at,
                created_by_id=staff.id,
                updated_by_id=staff.id,
            )
        )
        session.flush()

    monkeypatch.setattr(admin_routes, "save_pending_package_draft", broken_save)
    try:
        response = client.post(f"/admin/packages/{package.id}/edit", data=post_payload(auth.csrf_token, package))
    finally:
        close_client(client)
    assert response.status_code == 409
    db_session.expire_all()
    package = db_session.get(Package, package.id)
    assert db_session.scalar(select(PackageDraft).where(PackageDraft.package_id == package.id)) is None
    assert package.title == "Live published title"
    assert package.updated_at == original_updated_at
    assert len(package.prices) == 2
