"""PostgreSQL-backed read-only package history route tests."""

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Package, PackagePrice, PackageVersion, User
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


def staff_client(db: Session, role: str = "admin"):
    staff = User(name="History Tester", email=f"{uuid4()}@example.test", role=role, is_active=True)
    db.add(staff)
    db.flush()
    auth_session, raw_token = issue_session(db, user_id=staff.id)
    db.flush()
    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, raw_token)
    return client, staff


def close_client(client: TestClient) -> None:
    client.close()
    app.dependency_overrides.pop(get_db, None)


def add_package(db: Session) -> Package:
    package = Package(
        package_code=f"HISTORY-{uuid4().hex[:8].upper()}",
        slug=f"history-{uuid4().hex[:8]}",
        title="Historical package title",
        summary="Snapshot summary",
        description="Snapshot description",
        destinations=["Munnar", "Alleppey"],
        duration_days=6,
        hotel_category="4-star",
        hotel_details=[{"name": "Sample hotel", "location": "Alleppey"}],
        itinerary=[{"day": 1, "title": "Arrival", "description": "Sample day"}],
        inclusions=["Breakfast"],
        map_url="https://maps.example.test/history",
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
                currency="INR",
                price_basis="per_person",
                price_note="Twin sharing",
            )
        ],
    )
    db.add(package)
    db.flush()
    return package


def test_admin_history_orders_versions_and_renders_metadata_without_mutation(db_session: Session) -> None:
    package = add_package(db_session)
    client, staff = staff_client(db_session)
    first = append_package_version(db_session, package, created_by_id=staff.id, change_note="Initial publication")
    package.title = "Current live title"
    package.prices[0].price_per_person = Decimal("34000.00")
    db_session.flush()
    second = append_package_version(db_session, package, created_by_id=staff.id, change_note="Updated seasonal rate")
    db_session.refresh(package)
    original_snapshot = deepcopy(second.snapshot)
    original_updated_at = package.updated_at
    try:
        response = client.get(f"/admin/packages/{package.id}/versions")
    finally:
        close_client(client)

    assert response.status_code == 200
    assert response.text.index("Version 2") < response.text.index("Version 1")
    assert "Updated seasonal rate" in response.text
    assert "Initial publication" in response.text
    assert "History Tester" in response.text
    assert second.created_at.strftime("%d %b %Y") in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    db_session.refresh(package)
    assert package.updated_at == original_updated_at
    assert second.snapshot == original_snapshot
    stored_versions = db_session.scalars(
        select(PackageVersion)
        .where(PackageVersion.package_id == package.id)
        .order_by(PackageVersion.version_number)
    ).all()
    assert [item.id for item in stored_versions] == [first.id, second.id]


def test_empty_history_has_clear_empty_state(db_session: Session) -> None:
    package = add_package(db_session)
    client, _ = staff_client(db_session)
    try:
        response = client.get(f"/admin/packages/{package.id}/versions")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "No version history yet" in response.text
    assert "Package snapshots will appear here" in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_non_admin_roles_cannot_access_history_or_detail(db_session: Session, role: str) -> None:
    package = add_package(db_session)
    version = append_package_version(db_session, package, change_note="Recorded")
    client, _ = staff_client(db_session, role)
    try:
        history = client.get(f"/admin/packages/{package.id}/versions")
        detail = client.get(f"/admin/packages/{package.id}/versions/{version.id}")
    finally:
        close_client(client)
    assert history.status_code == detail.status_code == 403


def test_unauthenticated_history_and_detail_redirect_to_login(db_session: Session) -> None:
    package = add_package(db_session)
    version = append_package_version(db_session, package)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        history = client.get(f"/admin/packages/{package.id}/versions")
        detail = client.get(f"/admin/packages/{package.id}/versions/{version.id}")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert history.status_code == detail.status_code == 303
    assert history.headers["location"] == detail.headers["location"] == "/admin/login"


def test_admin_version_detail_converts_dates_and_prices_and_is_read_only(db_session: Session) -> None:
    package = add_package(db_session)
    version = append_package_version(db_session, package, change_note="Historical package state")
    client, _ = staff_client(db_session)
    db_session.refresh(package)
    package_before = package.to_snapshot()
    version_before = deepcopy(version.snapshot)
    try:
        response = client.get(f"/admin/packages/{package.id}/versions/{version.id}")
    finally:
        close_client(client)

    assert response.status_code == 200
    assert "Historical snapshot · Read only" in response.text
    assert "Historical package title" in response.text
    assert "Munnar" in response.text and "Sample hotel" in response.text
    assert "October–March" in response.text
    assert "₹32,500 per person" in response.text
    assert "01 Oct 2026" in response.text and "31 Mar 2027" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    db_session.refresh(package)
    assert package.to_snapshot() == package_before
    assert version.snapshot == version_before


def test_version_from_another_package_and_missing_version_return_404(db_session: Session) -> None:
    package = add_package(db_session)
    other = add_package(db_session)
    foreign_version = append_package_version(db_session, other)
    client, _ = staff_client(db_session)
    try:
        foreign = client.get(f"/admin/packages/{package.id}/versions/{foreign_version.id}")
        missing = client.get(f"/admin/packages/{package.id}/versions/{uuid4()}")
        missing_package = client.get(f"/admin/packages/{uuid4()}/versions")
    finally:
        close_client(client)
    assert foreign.status_code == missing.status_code == missing_package.status_code == 404


def test_malformed_snapshot_detail_renders_warning_without_crashing(db_session: Session) -> None:
    package = add_package(db_session)
    version = append_package_version(
        db_session,
        package,
        snapshot={"title": "Partial snapshot", "prices": [{"price_per_person": "not-a-price"}]},
        change_note="Incomplete import",
    )
    client, _ = staff_client(db_session)
    try:
        response = client.get(f"/admin/packages/{package.id}/versions/{version.id}")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "Partial snapshot" in response.text
    assert "Some snapshot data could not be fully interpreted" in response.text
    assert "raw values are shown" in response.text


def test_dashboard_and_review_expose_history_only_to_admins(db_session: Session) -> None:
    package = add_package(db_session)
    for role in ("admin", "editor", "consultant"):
        client, _ = staff_client(db_session, role)
        try:
            dashboard = client.get("/admin/packages")
            detail = client.get(f"/admin/packages/{package.id}/review") if role == "admin" else None
        finally:
            close_client(client)
        history_link = f"/admin/packages/{package.id}/versions"
        assert (history_link in dashboard.text) == (role == "admin")
        if detail is not None:
            assert history_link in detail.text


def test_public_package_routes_and_sitemap_do_not_expose_history(db_session: Session) -> None:
    package = add_package(db_session)
    version = append_package_version(db_session, package, change_note="Private snapshot note")
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        public_detail = client.get(f"/packages/{package.slug}")
        guessed_history = client.get(f"/packages/{package.slug}/versions/{version.id}")
        sitemap = client.get("/sitemap.xml")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert public_detail.status_code == 200
    assert guessed_history.status_code == 404
    assert package.id.hex not in sitemap.text
    assert version.id.hex not in sitemap.text
    assert "Private snapshot note" not in sitemap.text


def test_admin_can_compare_changed_added_removed_and_unchanged_fields_and_prices(db_session: Session) -> None:
    package = add_package(db_session)
    package.inclusions = ["Breakfast"]
    package.prices.append(
        PackagePrice(
            season_name="Monsoon season",
            start_month=6,
            end_month=9,
            price_per_person=Decimal("24900.00"),
        )
    )
    db_session.flush()
    client, staff = staff_client(db_session)
    older = append_package_version(db_session, package, created_by_id=staff.id, change_note="Older snapshot")

    package.title = "Updated package title"
    package.summary = None
    package.map_url = "https://maps.example.test/updated"
    package.inclusions = None
    peak = next(price for price in package.prices if price.season_name == "Peak season")
    peak.price_per_person = Decimal("35000.25")
    peak.valid_from = date(2026, 10, 15)
    package.prices[:] = [peak]
    package.prices.append(
        PackagePrice(
            season_name="Special season",
            start_month=4,
            end_month=5,
            price_per_person=Decimal("29000.00"),
        )
    )
    db_session.flush()
    newer = append_package_version(db_session, package, created_by_id=staff.id, change_note="Newer snapshot")
    db_session.refresh(package)
    package_before = package.to_snapshot()
    older_before = deepcopy(older.snapshot)
    newer_before = deepcopy(newer.snapshot)
    try:
        response = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={older.id}&to={newer.id}"
        )
    finally:
        close_client(client)

    assert response.status_code == 200
    assert "Version 1" in response.text and "Version 2" in response.text
    assert "Older snapshot" in response.text and "Newer snapshot" in response.text
    assert "History Tester" in response.text
    assert "Changed" in response.text and "Added" in response.text and "Removed" in response.text and "Unchanged" in response.text
    assert "Updated package title" in response.text
    assert "https://maps.example.test/updated" in response.text
    assert "Not specified" in response.text
    assert "₹35,000.25" in response.text
    assert "15 Oct 2026" in response.text
    assert "Monsoon season" in response.text
    assert "Special season" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-robots-tag"] == "noindex, nofollow"
    db_session.refresh(package)
    assert package.to_snapshot() == package_before
    assert older.snapshot == older_before and newer.snapshot == newer_before
    assert db_session.query(PackageVersion).filter_by(package_id=package.id).count() == 2


def test_identical_version_comparison_shows_unchanged_and_missing_values(db_session: Session) -> None:
    package = add_package(db_session)
    package.summary = None
    version = append_package_version(db_session, package, change_note="Single snapshot")
    client, _ = staff_client(db_session)
    try:
        response = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={version.id}&to={version.id}"
        )
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "same snapshot was selected twice" in response.text
    assert "Not specified" in response.text
    assert "Unchanged" in response.text


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_only_admin_can_compare_versions(db_session: Session, role: str) -> None:
    package = add_package(db_session)
    first = append_package_version(db_session, package)
    second = append_package_version(db_session, package, change_note="Second")
    client, _ = staff_client(db_session, role)
    try:
        response = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={first.id}&to={second.id}"
        )
    finally:
        close_client(client)
    assert response.status_code == 403


def test_unauthenticated_comparison_redirects_to_login(db_session: Session) -> None:
    package = add_package(db_session)
    first = append_package_version(db_session, package)
    second = append_package_version(db_session, package)
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    try:
        response = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={first.id}&to={second.id}"
        )
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_comparison_rejects_missing_and_cross_package_versions(db_session: Session) -> None:
    package = add_package(db_session)
    other = add_package(db_session)
    first = append_package_version(db_session, package)
    foreign = append_package_version(db_session, other)
    client, _ = staff_client(db_session)
    try:
        missing = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={first.id}&to={uuid4()}"
        )
        cross_package = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={first.id}&to={foreign.id}"
        )
        malformed_id = client.get(
            f"/admin/packages/{package.id}/versions/compare?from=not-a-uuid&to={first.id}"
        )
    finally:
        close_client(client)
    assert missing.status_code == cross_package.status_code == 404
    assert malformed_id.status_code == 422


def test_malformed_price_snapshots_compare_with_warning_and_escape_text(db_session: Session) -> None:
    package = add_package(db_session)
    old = append_package_version(
        db_session,
        package,
        snapshot={"title": "<script>alert(1)</script>", "prices": "not-an-array"},
        change_note="Malformed old data",
    )
    new = append_package_version(
        db_session,
        package,
        snapshot={"title": "Safe title", "prices": [{"season_name": "Bad", "price_per_person": "NaN"}]},
        change_note="Malformed new data",
    )
    client, _ = staff_client(db_session)
    try:
        response = client.get(
            f"/admin/packages/{package.id}/versions/compare?from={old.id}&to={new.id}"
        )
    finally:
        close_client(client)
    assert response.status_code == 200
    assert "Some snapshot data could not be fully interpreted" in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_history_page_offers_same_package_comparison_selectors(db_session: Session) -> None:
    package = add_package(db_session)
    append_package_version(db_session, package)
    append_package_version(db_session, package)
    client, _ = staff_client(db_session)
    try:
        response = client.get(f"/admin/packages/{package.id}/versions")
    finally:
        close_client(client)
    assert response.status_code == 200
    assert f"/admin/packages/{package.id}/versions/compare" in response.text
    assert 'name="from"' in response.text and 'name="to"' in response.text

