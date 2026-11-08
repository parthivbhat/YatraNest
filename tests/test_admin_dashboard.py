from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database import engine, get_db
from app.main import app
from app.models import Package, User, UserSession
from app.services.authorization import require_admin, require_editor_or_admin
from app.services.session_store import SESSION_COOKIE_NAME, hash_session_token, issue_session, revoke_session


@pytest.fixture
def db_session():
    """Use a PostgreSQL transaction and roll back all rows created by each test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def add_user(db_session: Session, role: str = "admin", *, active: bool = True) -> User:
    user = User(
        name=f"Test {role.title()}",
        email=f"{uuid4()}@example.test",
        role=role,
        is_active=active,
    )
    db_session.add(user)
    db_session.flush()
    return user


def add_package(db_session: Session, code: str, status: str, *, updated_by: User | None = None) -> Package:
    package = Package(
        package_code=code,
        slug=f"{code.lower()}-{uuid4().hex[:8]}",
        title=f"Journey {code}",
        destinations=["Kerala", "Munnar"],
        duration_days=6,
        status=status,
        updated_by=updated_by,
    )
    db_session.add(package)
    db_session.flush()
    return package


def request_as(db_session: Session, user: User | None, *, follow_redirects: bool = False):
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=follow_redirects)
    token = None
    if user is not None:
        _, token = issue_session(db_session, user_id=user.id)
        db_session.flush()
        client.cookies.set(SESSION_COOKIE_NAME, token)
    return client, token


def request_with_token(db_session: Session, token: str):
    app.dependency_overrides[get_db] = lambda: db_session
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return client


def test_dashboard_allows_admin_editor_and_consultant(db_session: Session) -> None:
    add_package(db_session, "ROLE-001", "draft")
    for role in ("admin", "editor", "consultant"):
        user = add_user(db_session, role)
        client, _ = request_as(db_session, user)
        try:
            response = client.get("/admin/packages")
        finally:
            client.close()
            app.dependency_overrides.pop(get_db, None)

        assert response.status_code == 200
        assert "Your journeys" in response.text
        assert role.title() in response.text


def test_role_dependencies_enforce_edit_and_publish_permissions(db_session: Session) -> None:
    admin = add_user(db_session, "admin")
    editor = add_user(db_session, "editor")
    consultant = add_user(db_session, "consultant")

    assert require_editor_or_admin(admin) is admin
    assert require_editor_or_admin(editor) is editor
    assert require_admin(admin) is admin
    with pytest.raises(HTTPException) as editor_publish_error:
        require_admin(editor)
    assert editor_publish_error.value.status_code == 403
    with pytest.raises(HTTPException) as consultant_edit_error:
        require_editor_or_admin(consultant)
    assert consultant_edit_error.value.status_code == 403


def test_unauthenticated_dashboard_redirects_to_login(db_session: Session) -> None:
    client, _ = request_as(db_session, None)
    try:
        response = client.get("/admin/packages")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_inactive_staff_session_redirects_and_is_revoked(db_session: Session) -> None:
    user = add_user(db_session, active=False)
    client, token = request_as(db_session, user)
    try:
        response = client.get("/admin/packages")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    session = db_session.query(UserSession).filter_by(token_hash=hash_session_token(token)).one()
    assert session.revoked_at is not None


@pytest.mark.parametrize("session_state", ["revoked", "expired", "idle"])
def test_revoked_or_expired_session_redirects_to_login(db_session: Session, session_state: str) -> None:
    user = add_user(db_session)
    now = datetime.now(UTC)
    created_at = now - timedelta(minutes=31) if session_state == "idle" else now
    _, token = issue_session(db_session, user_id=user.id, now=created_at)
    if session_state == "revoked":
        session = db_session.query(UserSession).filter_by(token_hash=hash_session_token(token)).one()
        revoke_session(session)
    elif session_state == "expired":
        session = db_session.query(UserSession).filter_by(token_hash=hash_session_token(token)).one()
        session.expires_at = now - timedelta(seconds=1)
    # For idle, the session is only 31 minutes old and remains inside its 8-hour absolute life.
    db_session.flush()

    client = request_with_token(db_session, token)
    try:
        response = client.get("/admin/packages")
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_dashboard_search_and_status_filter_cover_all_package_states(db_session: Session) -> None:
    staff = add_user(db_session)
    draft = add_package(db_session, "DASH-DRAFT", "draft", updated_by=staff)
    published = add_package(db_session, "DASH-PUB", "published")
    archived = add_package(db_session, "DASH-ARCH", "archived")
    client, _ = request_as(db_session, staff)
    try:
        all_packages = client.get("/admin/packages")
        destination_search = client.get("/admin/packages", params={"q": "Munnar"})
        draft_filter = client.get("/admin/packages", params={"status": "draft"})
        code_search = client.get("/admin/packages", params={"q": "DASH-PUB"})
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)

    assert all_packages.status_code == 200
    for package in (draft, published, archived):
        assert package.package_code in all_packages.text
    assert "by Test Admin" in all_packages.text
    assert destination_search.status_code == 200
    for package in (draft, published, archived):
        assert package.package_code in destination_search.text
    assert draft.package_code in draft_filter.text
    assert published.package_code not in draft_filter.text
    assert archived.package_code not in draft_filter.text
    assert published.package_code in code_search.text
    assert draft.package_code not in code_search.text


def test_dashboard_rejects_unknown_status_filter(db_session: Session) -> None:
    staff = add_user(db_session)
    client, _ = request_as(db_session, staff)
    try:
        response = client.get("/admin/packages", params={"status": "deleted"})
    finally:
        client.close()
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 422
