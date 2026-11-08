from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.database import engine, get_db
from app.main import app
from app.models import User, UserSession
from app.routes.auth import login_page, login_submit, logout
from app.services.authentication import verify_password
from app.services.session_store import (
    ABSOLUTE_LIFETIME,
    IDLE_TIMEOUT,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    find_session,
    hash_session_token,
    issue_session,
    require_valid_csrf,
    revoke_session,
    session_expired,
    set_session_cookie,
)
from app.services.staff_accounts import create_staff_account


@pytest.fixture
def db_session():
    """Use an isolated PostgreSQL transaction for each authentication test."""
    with engine.connect() as connection:
        transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            transaction.rollback()


def make_request(path: str, *, cookies: dict[str, str] | None = None) -> Request:
    cookie_header = "; ".join(f"{key}={value}" for key, value in (cookies or {}).items())
    headers = [(b"cookie", cookie_header.encode())] if cookie_header else []
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": headers,
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
            "scheme": "http",
            "root_path": "",
            "app": None,
        }
    )


def test_staff_provisioning_stores_argon2_hash_and_normalizes_email(db_session: Session) -> None:
    user = create_staff_account(
        db_session,
        name="  Example Admin ",
        email=" Admin@Example.test ",
        role="ADMIN",
        password="a-long-demo-passphrase",
    )
    db_session.flush()

    assert user.name == "Example Admin"
    assert user.email == "admin@example.test"
    assert user.role == "admin"
    assert user.password_hash.startswith("$argon2id$")
    assert verify_password("a-long-demo-passphrase", user.password_hash)
    assert not verify_password("wrong-password", user.password_hash)


@pytest.mark.parametrize(
    ("name", "email", "role", "password"),
    [
        ("", "a@example.test", "admin", "long-enough-password"),
        ("A", "bad-address", "admin", "long-enough-password"),
        ("A", "a@example.test", "owner", "long-enough-password"),
        ("A", "a@example.test", "admin", "short"),
    ],
)
def test_staff_provisioning_rejects_invalid_input(
    db_session: Session, name: str, email: str, role: str, password: str
) -> None:
    with pytest.raises(ValueError):
        create_staff_account(db_session, name=name, email=email, role=role, password=password)


def test_session_stores_only_token_hash_and_enforces_idle_and_absolute_lifetimes(db_session: Session) -> None:
    user = create_staff_account(
        db_session,
        name="Session User",
        email=f"{uuid4()}@example.test",
        role="editor",
        password="another-long-password",
    )
    now = datetime(2026, 1, 1, tzinfo=UTC)
    session, raw_token = issue_session(db_session, user_id=user.id, now=now)
    db_session.flush()

    assert raw_token not in session.token_hash
    assert session.token_hash == hash_session_token(raw_token)
    assert find_session(db_session, raw_token).id == session.id
    assert session.expires_at == now + ABSOLUTE_LIFETIME
    assert not session_expired(session, now=now + IDLE_TIMEOUT - timedelta(seconds=1))
    assert session_expired(session, now=now + IDLE_TIMEOUT)
    assert session_expired(session, now=now + ABSOLUTE_LIFETIME)


def test_csrf_comparison_and_session_revocation(db_session: Session) -> None:
    session, _ = issue_session(db_session, user_id=None)
    assert session.csrf_token
    require_valid_csrf(session, session.csrf_token)
    with pytest.raises(HTTPException) as error:
        require_valid_csrf(session, "incorrect-token")
    assert error.value.status_code == 403

    revoke_session(session)
    assert session_expired(session)


def test_session_cookie_is_http_only_and_deleted_with_matching_attributes() -> None:
    response = Response()
    set_session_cookie(response, "opaque-token", expires_at=datetime.now(UTC) + timedelta(hours=1))
    cookie = response.headers["set-cookie"].lower()
    assert SESSION_COOKIE_NAME in cookie
    assert "httponly" in cookie
    assert "samesite=lax" in cookie

    clear_session_cookie(response)
    assert "max-age=0" in response.headers.getlist("set-cookie")[-1].lower()


def test_login_creates_preauth_session_then_rotates_to_staff_session(db_session: Session) -> None:
    user = create_staff_account(
        db_session,
        name="Login User",
        email=f"{uuid4()}@example.test",
        role="consultant",
        password="valid-long-password",
    )
    db_session.flush()

    page = login_page(make_request("/admin/login"), db_session)
    html = page.body.decode()
    marker = 'name="csrf_token" value="'
    csrf = html.split(marker, 1)[1].split('"', 1)[0]
    preauth_raw = page.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    preauth = find_session(db_session, preauth_raw)

    response = login_submit(
        make_request("/admin/login", cookies={SESSION_COOKIE_NAME: preauth_raw}),
        user.email,
        "valid-long-password",
        csrf,
        db_session,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/packages"
    auth_raw = response.headers["set-cookie"].split(";", 1)[0].split("=", 1)[1]
    authenticated = find_session(db_session, auth_raw)
    assert authenticated.user_id == user.id
    assert authenticated.token_hash != preauth.token_hash
    assert preauth.revoked_at is not None
    assert authenticated.expires_at - authenticated.created_at == ABSOLUTE_LIFETIME

    logout_response = logout(authenticated.csrf_token, authenticated, db_session)
    assert logout_response.status_code == 303
    assert authenticated.revoked_at is not None
    assert db_session.scalar(select(UserSession).where(UserSession.id == authenticated.id)) is not None


def test_login_page_http_response_sets_csrf_and_preauth_cookie(db_session: Session) -> None:
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        with TestClient(app) as client:
            response = client.get("/admin/login")
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert response.status_code == 200
    assert 'name="csrf_token" value="' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert SESSION_COOKIE_NAME in response.cookies
