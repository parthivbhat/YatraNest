from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Package, PackagePrice, PackageVersion, User, UserSession
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


@pytest.fixture
def db_session():
    """Use a PostgreSQL transaction that is rolled back after each route test."""
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer_transaction.rollback()


def add_staff(db_session: Session, role: str = "admin") -> tuple[User, UserSession, str]:
    staff = User(
        name=f"Draft Test {role.title()}",
        email=f"{uuid4()}@example.test",
        role=role,
        is_active=True,
    )
    db_session.add(staff)
    db_session.flush()
    session, raw_token = issue_session(db_session, user_id=staff.id)
    db_session.flush()
    return staff, session, raw_token


def add_package(
    db_session: Session,
    *,
    status: str = "draft",
    title: str | None = None,
    code: str | None = None,
    slug: str | None = None,
    prices: list[PackagePrice] | None = None,
) -> Package:
    package_code = code or f"T-{uuid4().hex[:12].upper()}"
    package = Package(
        package_code=package_code,
        slug=slug or f"test-{uuid4().hex[:12]}",
        title=title or "Test package",
        status=status,
        destinations=["Kochi"],
        duration_days=5,
        prices=prices or [],
    )
    db_session.add(package)
    db_session.flush()
    return package


def as_staff(db_session: Session, role: str = "admin", *, follow_redirects: bool = False):
    staff, auth_session, raw_token = add_staff(db_session, role)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=follow_redirects)
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client, staff, auth_session


def reset_test_app(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def form_data(csrf_token: str, **overrides) -> dict[str, object]:
    data: dict[str, object] = {
        "csrf_token": csrf_token,
        "package_code": f"PKG-{uuid4().hex[:8].upper()}",
        "title": "Staff supplied Kerala journey",
        "destinations": "Kochi\nMunnar",
        "duration_days": "6",
        "hotel_category": "4-star",
        "description": "Description entered by staff.",
        "price_season_name": ["Peak season", "Monsoon season"],
        "price_start_month": ["10", "6"],
        "price_end_month": ["3", "9"],
        "price_per_person": ["32500.00", "24900.00"],
        "price_valid_from": ["", ""],
        "price_valid_until": ["", ""],
        "price_note": ["", ""],
    }
    data.update(overrides)
    return data


def test_create_draft_with_content_and_multiple_seasonal_prices(db_session: Session) -> None:
    client, staff, auth_session = as_staff(db_session, "editor")
    data = form_data(auth_session.csrf_token)
    code = data["package_code"]
    try:
        response = client.post("/admin/packages/new", data=data)
    finally:
        reset_test_app(client)

    assert response.status_code == 303
    assert response.headers["location"].endswith("/edit")
    package = db_session.scalar(select(Package).where(Package.package_code == code))
    assert package is not None
    assert package.status == "draft"
    assert package.slug == "staff-supplied-kerala-journey"
    assert package.created_by_id == staff.id
    assert package.updated_by_id == staff.id
    assert package.destinations == ["Kochi", "Munnar"]
    assert [(price.season_name, price.start_month, price.end_month) for price in package.prices] == [
        ("Monsoon season", 6, 9),
        ("Peak season", 10, 3),
    ]
    assert [price.price_per_person for price in package.prices] == [Decimal("24900.00"), Decimal("32500.00")]
    assert package.versions == []


def test_create_incomplete_draft_without_prices(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session, "admin")
    data = form_data(
        auth_session.csrf_token,
        package_code=f"PKG-{uuid4().hex[:8].upper()}",
        title="Incomplete draft",
        price_season_name=None,
        price_start_month=None,
        price_end_month=None,
        price_per_person=None,
        price_valid_from=None,
        price_valid_until=None,
        price_note=None,
    )
    code = data["package_code"]
    try:
        response = client.post("/admin/packages/new", data=data)
    finally:
        reset_test_app(client)

    assert response.status_code == 303
    package = db_session.scalar(select(Package).where(Package.package_code == code))
    assert package is not None and package.status == "draft"
    assert package.prices == []


def test_duplicate_package_code_is_reported_and_slug_collision_gets_suffix(db_session: Session) -> None:
    title = "A title with an existing slug"
    code = f"PKG-{uuid4().hex[:8].upper()}"
    add_package(db_session, title=title, code=f"OLD-{uuid4().hex[:8].upper()}", slug="a-title-with-an-existing-slug")
    client, _, auth_session = as_staff(db_session)
    try:
        collision = client.post(
            "/admin/packages/new",
            data=form_data(auth_session.csrf_token, package_code=code, title=title),
        )
        created = client.post(
            "/admin/packages/new",
            data=form_data(auth_session.csrf_token, package_code=code, title=title),
        )
    finally:
        reset_test_app(client)

    assert collision.status_code == 303
    # The second submission uses the same code and must be rejected without partial data.
    assert created.status_code == 422
    assert "already in use" in created.text
    created_package = db_session.scalar(select(Package).where(Package.package_code == code))
    assert created_package is not None
    assert created_package.slug == "a-title-with-an-existing-slug-2"
    assert db_session.scalar(select(func.count()).select_from(Package).where(Package.package_code == code)) == 1


def test_duplicate_package_code_is_rejected_before_writes(db_session: Session) -> None:
    existing_code = f"PKG-{uuid4().hex[:8].upper()}"
    add_package(db_session, code=existing_code)
    client, _, auth_session = as_staff(db_session)
    try:
        response = client.post(
            "/admin/packages/new",
            data=form_data(auth_session.csrf_token, package_code=existing_code),
        )
    finally:
        reset_test_app(client)

    assert response.status_code == 422
    assert "already in use" in response.text
    assert db_session.scalar(select(func.count()).select_from(Package).where(Package.package_code == existing_code)) == 1


def test_edit_replaces_prices_keeps_identity_and_does_not_create_versions(db_session: Session) -> None:
    client, staff, auth_session = as_staff(db_session, "editor")
    original = add_package(
        db_session,
        code=f"PKG-{uuid4().hex[:8].upper()}",
        slug=f"stable-url-{uuid4().hex[:6]}",
        title="Original title",
        prices=[
            PackagePrice(
                season_name="Old season",
                start_month=1,
                end_month=2,
                price_per_person=Decimal("1000.00"),
            )
        ],
    )
    package_id, original_code, original_slug = original.id, original.package_code, original.slug
    try:
        edit_page = client.get(f"/admin/packages/{package_id}/edit")
        data = form_data(
            auth_session.csrf_token,
            package_code="TAMPERED-CODE",
            title="Updated staff title",
            expected_updated_at=original.updated_at.isoformat(),
            price_season_name=["Peak", "Monsoon"],
            price_start_month=["10", "6"],
            price_end_month=["3", "9"],
            price_per_person=["45000.00", "25000.00"],
        )
        response = client.post(f"/admin/packages/{package_id}/edit", data=data)
    finally:
        reset_test_app(client)

    assert edit_page.status_code == 200
    assert response.status_code == 303
    db_session.expire_all()
    updated = db_session.get(Package, package_id)
    assert updated is not None
    assert updated.package_code == original_code
    assert updated.slug == original_slug
    assert updated.title == "Updated staff title"
    assert updated.updated_by_id == staff.id
    assert [(price.season_name, price.price_per_person) for price in updated.prices] == [
        ("Monsoon", Decimal("25000.00")),
        ("Peak", Decimal("45000.00")),
    ]
    assert updated.versions == []


def test_failed_validation_does_not_create_partial_package_or_prices(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session)
    code = f"PKG-{uuid4().hex[:8].upper()}"
    data = form_data(
        auth_session.csrf_token,
        package_code=code,
        price_season_name=["Peak", "Monsoon"],
        price_start_month=["10", "13"],
        price_end_month=["3", "9"],
        price_per_person=["32500.00", "24900.00"],
    )
    try:
        response = client.post("/admin/packages/new", data=data)
    finally:
        reset_test_app(client)

    assert response.status_code == 422
    assert "between 1 and 12" in response.text
    assert db_session.scalar(select(Package).where(Package.package_code == code)) is None


def test_consultant_cannot_open_create_or_mutate_routes(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session, "consultant")
    try:
        page = client.get("/admin/packages/new")
        post = client.post("/admin/packages/new", data=form_data(auth_session.csrf_token))
    finally:
        reset_test_app(client)

    assert page.status_code == 403
    assert post.status_code == 403


def test_unauthenticated_staff_routes_redirect_to_login(db_session: Session) -> None:
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        response = client.get("/admin/packages/new")
    finally:
        reset_test_app(client)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("csrf_token", [None, "incorrect-csrf"])
def test_create_requires_valid_csrf_token(db_session: Session, csrf_token: str | None) -> None:
    client, _, _ = as_staff(db_session)
    code = f"PKG-{uuid4().hex[:8].upper()}"
    data = form_data(csrf_token or "", package_code=code)
    if csrf_token is None:
        data.pop("csrf_token")
    else:
        data["csrf_token"] = csrf_token
    try:
        response = client.post("/admin/packages/new", data=data)
    finally:
        reset_test_app(client)

    assert response.status_code == 403
    assert db_session.scalar(select(Package).where(Package.package_code == code)) is None


@pytest.mark.parametrize("csrf_token", [None, "incorrect-csrf"])
def test_edit_requires_valid_csrf_token(db_session: Session, csrf_token: str | None) -> None:
    client, _, auth_session = as_staff(db_session)
    package = add_package(db_session)
    try:
        data = form_data(
            auth_session.csrf_token,
            expected_updated_at=package.updated_at.isoformat(),
            title="Should not save",
        )
        if csrf_token is None:
            data.pop("csrf_token")
        else:
            data["csrf_token"] = csrf_token
        response = client.post(f"/admin/packages/{package.id}/edit", data=data)
    finally:
        reset_test_app(client)

    assert response.status_code == 403
    db_session.refresh(package)
    assert package.title == "Test package"


def test_archived_package_is_read_only_on_get_and_post(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session)
    package = add_package(db_session, status="archived")
    try:
        page = client.get(f"/admin/packages/{package.id}/edit")
        data = form_data(auth_session.csrf_token, expected_updated_at=package.updated_at.isoformat())
        post = client.post(f"/admin/packages/{package.id}/edit", data=data)
    finally:
        reset_test_app(client)

    assert page.status_code == 409
    assert post.status_code == 409
    db_session.refresh(package)
    assert package.status == "archived"


def test_published_package_edit_creates_private_pending_draft(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session)
    package = add_package(db_session, status="published")
    try:
        page = client.get(f"/admin/packages/{package.id}/edit")
        data = form_data(
            auth_session.csrf_token,
            expected_base_updated_at=package.updated_at.isoformat(),
            expected_draft_updated_at="",
        )
        post = client.post(f"/admin/packages/{package.id}/edit", data=data)
    finally:
        reset_test_app(client)

    assert page.status_code == 200
    assert post.status_code == 303
    db_session.refresh(package)
    assert package.status == "published"
    assert package.title == "Test package"
    assert package.pending_draft is not None
    assert package.pending_draft.draft_payload["title"] == "Staff supplied Kerala journey"


def test_stale_draft_edit_is_rejected(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session)
    package = add_package(db_session)
    stale_timestamp = package.updated_at.isoformat()
    package.title = "Changed by another staff member"
    package.updated_at = datetime.now(UTC)
    db_session.flush()
    try:
        response = client.post(
            f"/admin/packages/{package.id}/edit",
            data=form_data(
                auth_session.csrf_token,
                expected_updated_at=stale_timestamp,
                title="Stale overwrite",
            ),
        )
    finally:
        reset_test_app(client)

    assert response.status_code == 409
    db_session.refresh(package)
    assert package.title == "Changed by another staff member"


def test_created_draft_remains_hidden_from_public_pages(db_session: Session) -> None:
    client, _, auth_session = as_staff(db_session)
    code = f"PKG-{uuid4().hex[:8].upper()}"
    try:
        create_response = client.post(
            "/admin/packages/new",
            data=form_data(auth_session.csrf_token, package_code=code, title="Never public until published"),
        )
        public_response = client.get("/packages")
    finally:
        reset_test_app(client)

    assert create_response.status_code == 303
    assert public_response.status_code == 200
    assert "Never public until published" not in public_response.text
    assert db_session.scalar(select(Package).where(Package.package_code == code)).status == "draft"
    assert db_session.scalar(
        select(func.count()).select_from(PackageVersion).join(Package).where(Package.package_code == code)
    ) == 0
