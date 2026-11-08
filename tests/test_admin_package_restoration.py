"""PostgreSQL-backed tests for safe historical package restoration."""

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
from app.models import Package, PackageDraft, PackagePrice, PackageVersion, User
from app.services.package_versions import append_package_version
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


def add_user(db: Session, role: str = "admin") -> User:
    user = User(name=f"Restore {role.title()}", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(user)
    db.flush()
    return user


def staff_client(db: Session, role: str = "admin"):
    staff = add_user(db, role)
    auth_session, raw_token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client, staff, auth_session


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def add_package(db: Session, owner: User, *, status: str = "published") -> Package:
    package = Package(
        package_code=f"RESTORE-{uuid4().hex[:8].upper()}",
        slug=f"restore-{uuid4().hex[:8]}",
        title="Historical title",
        summary="Historical summary",
        description="Historical description",
        destinations=["Munnar", "Alleppey"],
        duration_days=6,
        hotel_category="4-star",
        hotel_details=[{"name": "Historical hotel", "location": "Alleppey"}],
        itinerary=[{"day": 1, "title": "Historical day", "description": "Historical detail"}],
        inclusions=["Breakfast"],
        map_url="https://maps.example.test/historical",
        status=status,
        published_at=datetime(2025, 5, 1, tzinfo=UTC) if status == "published" else None,
        scheduled_publish_at=datetime(2027, 2, 1, tzinfo=UTC),
        created_by_id=owner.id,
        updated_by_id=owner.id,
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
    db.refresh(package)
    return package


def create_old_and_current_versions(db: Session, package: Package, owner: User):
    old_version = append_package_version(db, package, created_by_id=owner.id, change_note="Original published version")
    package.title = "Current title before restore"
    package.summary = "Current summary before restore"
    package.description = "Current description before restore"
    package.prices[0].price_per_person = Decimal("42000.00")
    package.prices[1].price_per_person = Decimal("27000.00")
    db.flush()
    current_version = append_package_version(db, package, created_by_id=owner.id, change_note="Current package snapshot")
    db.refresh(package)
    return old_version, current_version


def version_count(db: Session, package_id) -> int:
    return db.scalar(
        select(func.count()).select_from(PackageVersion).where(PackageVersion.package_id == package_id)
    )


def test_admin_restores_published_package_with_two_complete_audit_snapshots(db_session: Session) -> None:
    owner = add_user(db_session, "editor")
    package = add_package(db_session, owner, status="published")
    old, current = create_old_and_current_versions(db_session, package, owner)
    package_id, source_id = package.id, old.id
    code, slug = package.package_code, package.slug
    published_at, scheduled_at, creator_id = package.published_at, package.scheduled_publish_at, package.created_by_id
    before_restore = deepcopy(package.to_snapshot())
    old_snapshot, current_snapshot = deepcopy(old.snapshot), deepcopy(current.snapshot)
    expected_updated_at = package.updated_at.isoformat()
    client, admin, auth = staff_client(db_session)
    try:
        confirmation = client.get(f"/admin/packages/{package.id}/versions/{old.id}/restore")
        response = client.post(
            f"/admin/packages/{package.id}/versions/{old.id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": expected_updated_at},
        )
        public = client.get(f"/packages/{slug}")
    finally:
        close_client(client)

    assert confirmation.status_code == 200
    assert "current package code, URL slug, publication status" in confirmation.text
    assert 'name="expected_updated_at"' in confirmation.text
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/versions/{source_id}?restored=true")
    assert public.status_code == 200
    assert "Historical title" in public.text
    assert "Current title before restore" not in public.text

    db_session.expire_all()
    package = db_session.get(Package, package_id)
    versions = db_session.scalars(
        select(PackageVersion).where(PackageVersion.package_id == package_id).order_by(PackageVersion.version_number)
    ).all()
    assert package.status == "published"
    assert package.published_at == published_at
    assert package.scheduled_publish_at == scheduled_at
    assert package.package_code == code and package.slug == slug
    assert package.created_by_id == creator_id
    assert package.updated_by_id == admin.id
    assert package.title == "Historical title"
    assert package.summary == "Historical summary"
    assert [(price.season_name, price.price_per_person) for price in package.prices] == [
        ("Monsoon season", Decimal("24900.00")),
        ("Peak season", Decimal("32500.00")),
    ]
    assert len(versions) == 4
    assert versions[0].snapshot == old_snapshot
    assert versions[1].snapshot == current_snapshot
    assert versions[2].snapshot == before_restore
    assert versions[2].change_note == f"Before restoring version {versions[0].version_number}"
    assert versions[3].snapshot["title"] == "Historical title"
    assert versions[3].snapshot["prices"] == old_snapshot["prices"]
    assert versions[3].snapshot["status"] == "published"
    assert versions[3].change_note == f"Restored from version {versions[0].version_number}"


def test_admin_restores_draft_without_publishing_and_keeps_schedule(db_session: Session) -> None:
    owner = add_user(db_session, "editor")
    package = add_package(db_session, owner, status="draft")
    old, _ = create_old_and_current_versions(db_session, package, owner)
    package_id, source_id, slug = package.id, old.id, package.slug
    schedule = package.scheduled_publish_at
    client, _, auth = staff_client(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/versions/{old.id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
        public = client.get(f"/packages/{slug}")
        sitemap = client.get("/sitemap.xml")
    finally:
        close_client(client)
    assert response.status_code == 303
    assert public.status_code == 404
    assert slug not in sitemap.text
    db_session.expire_all()
    restored = db_session.get(Package, package_id)
    assert restored.status == "draft"
    assert restored.published_at is None
    assert restored.scheduled_publish_at == schedule
    assert restored.title == "Historical title"


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_only_admin_can_restore_versions(db_session: Session, role: str) -> None:
    owner = add_user(db_session, "admin")
    package = add_package(db_session, owner)
    source = append_package_version(db_session, package)
    client, _, auth = staff_client(db_session, role)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/versions/{source.id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert response.status_code == 403
    assert version_count(db_session, package.id) == 1


def test_unauthenticated_restore_redirects_to_login(db_session: Session) -> None:
    owner = add_user(db_session)
    package = add_package(db_session, owner)
    source = append_package_version(db_session, package)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        response = client.post(f"/admin/packages/{package.id}/versions/{source.id}/restore", data={})
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


@pytest.mark.parametrize("csrf", [None, "invalid"])
def test_restore_requires_csrf(db_session: Session, csrf: str | None) -> None:
    owner = add_user(db_session)
    package = add_package(db_session, owner)
    source, _ = create_old_and_current_versions(db_session, package, owner)
    count_before = version_count(db_session, package.id)
    client, _, auth = staff_client(db_session)
    data = {"csrf_token": csrf or auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()}
    if csrf is None:
        data.pop("csrf_token")
    try:
        response = client.post(f"/admin/packages/{package.id}/versions/{source.id}/restore", data=data)
    finally:
        close_client(client)
    assert response.status_code == 403
    assert version_count(db_session, package.id) == count_before


@pytest.mark.parametrize("state", ["pending", "archived", "stale"])
def test_restore_conflicts_do_not_change_any_data(db_session: Session, state: str) -> None:
    owner = add_user(db_session, "editor")
    package = add_package(db_session, owner, status="archived" if state == "archived" else "published")
    source, current = create_old_and_current_versions(db_session, package, owner)
    pending_payload = None
    if state == "pending":
        pending_payload = package.to_snapshot()
        pending_payload["title"] = "Pending content stays intact"
        package.pending_draft = PackageDraft(
            draft_payload=pending_payload,
            base_updated_at=package.updated_at,
            created_by_id=owner.id,
            updated_by_id=owner.id,
        )
        db_session.flush()
    db_session.refresh(package)
    before = deepcopy(package.to_snapshot())
    expected = package.updated_at.isoformat()
    if state == "stale":
        expected = datetime.now(UTC).replace(year=package.updated_at.year - 1).isoformat()
    history_count = version_count(db_session, package.id)
    client, _, auth = staff_client(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/versions/{source.id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": expected},
        )
    finally:
        close_client(client)
    assert response.status_code == 409
    db_session.expire_all()
    package = db_session.get(Package, package.id)
    assert package.to_snapshot() == before
    assert version_count(db_session, package.id) == history_count
    if state == "pending":
        assert package.pending_draft.draft_payload == pending_payload
    assert current.snapshot["title"] == "Current title before restore"


@pytest.mark.parametrize("case", ["missing", "cross_package"])
def test_restore_requires_version_to_belong_to_package(db_session: Session, case: str) -> None:
    owner = add_user(db_session)
    package = add_package(db_session, owner)
    other = add_package(db_session, owner)
    source = append_package_version(db_session, package)
    foreign = append_package_version(db_session, other)
    requested_id = uuid4() if case == "missing" else foreign.id
    client, _, auth = staff_client(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/versions/{requested_id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert response.status_code == 404
    assert version_count(db_session, package.id) == 1
    assert source.version_number == 1


@pytest.mark.parametrize(
    "snapshot",
    [
        {"title": "Missing prices"},
        {"title": "No prices", "prices": []},
        {"title": "Bad price", "prices": [{"season_name": "Peak", "start_month": 10, "end_month": 3, "price_per_person": "32500.001"}]},
        {"title": "Malformed prices", "prices": "not-a-list"},
    ],
)
def test_invalid_or_unpriced_snapshot_is_rejected(db_session: Session, snapshot: dict) -> None:
    owner = add_user(db_session)
    package = add_package(db_session, owner)
    source = append_package_version(db_session, package, snapshot=snapshot)
    before = deepcopy(package.to_snapshot())
    client, _, auth = staff_client(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package.id}/versions/{source.id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert response.status_code == 422
    db_session.refresh(package)
    assert package.to_snapshot() == before
    assert version_count(db_session, package.id) == 1


def test_failure_after_pre_snapshot_rolls_back_content_prices_and_both_versions(db_session: Session, monkeypatch) -> None:
    import app.services.package_restoration as restoration

    owner = add_user(db_session, "editor")
    package = add_package(db_session, owner)
    source, current = create_old_and_current_versions(db_session, package, owner)
    package_id, source_id = package.id, source.id
    db_session.refresh(package)
    before = deepcopy(package.to_snapshot())
    history_count = version_count(db_session, package_id)
    original_append = restoration.append_package_version
    calls = 0

    def fail_second_append(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected post-snapshot failure")
        return original_append(*args, **kwargs)

    monkeypatch.setattr(restoration, "append_package_version", fail_second_append)
    client, _, auth = staff_client(db_session)
    try:
        response = client.post(
            f"/admin/packages/{package_id}/versions/{source_id}/restore",
            data={"csrf_token": auth.csrf_token, "expected_updated_at": package.updated_at.isoformat()},
        )
    finally:
        close_client(client)
    assert response.status_code == 500
    assert "No changes were saved" in response.text
    db_session.expire_all()
    package = db_session.get(Package, package_id)
    assert package.to_snapshot() == before
    assert version_count(db_session, package_id) == history_count
    assert current.snapshot["title"] == "Current title before restore"
