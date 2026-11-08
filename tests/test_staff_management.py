from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import User, UserSession
from app.services.authentication import verify_password
from app.services.session_store import SESSION_COOKIE_NAME, issue_session


def _user(db: Session, role: str, *, active: bool = True) -> User:
    user = User(name=f"Staff {role} {uuid4().hex[:6]}", email=f"{uuid4()}@example.test", role=role,
                is_active=active, password_hash="$argon2id$not-a-real-hash")
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def staff_management_client_factory(db_session: Session):
    app.dependency_overrides[get_db] = lambda: db_session

    def make_client(user: User | None):
        client = TestClient(app, follow_redirects=False)
        csrf = None
        if user is not None:
            _, raw = issue_session(db_session, user_id=user.id)
            db_session.flush()
            csrf = db_session.scalar(select(UserSession.csrf_token).where(UserSession.user_id == user.id))
            client.cookies.set(SESSION_COOKIE_NAME, raw)
        return client, csrf

    try:
        yield make_client
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_admin_can_list_create_role_change_activate_deactivate_and_reset_password(
    db_session: Session, staff_management_client_factory
):
    admin = _user(db_session, "admin")
    existing = _user(db_session, "editor")
    existing_session, _ = issue_session(db_session, user_id=existing.id)
    db_session.flush()
    client, csrf = staff_management_client_factory(admin)
    try:
        listing = client.get("/admin/staff")
        assert listing.status_code == 200
        assert listing.headers["cache-control"] == "no-store"
        assert "Staff management" in listing.text
        assert existing.email in listing.text
        assert "password_hash" not in listing.text
        assert "staff-account-card" in listing.text
        assert "Change role" in listing.text and "Account status" in listing.text and "Reset password" in listing.text
        assert f'action="/admin/staff/{existing.id}/role"' in listing.text
        assert f'action="/admin/staff/{existing.id}/active"' in listing.text
        assert f'action="/admin/staff/{existing.id}/password"' in listing.text
        assert 'name="confirm_action"' in listing.text

        created = client.post("/admin/staff", data={
            "csrf_token": csrf, "name": "New Consultant", "email": "New.Consultant@example.test",
            "role": "consultant", "password": "a-secure-new-passphrase", "password_confirm": "a-secure-new-passphrase",
            "confirm_action": "yes",
        })
        assert created.status_code == 303
        new_staff = db_session.scalar(select(User).where(User.email == "new.consultant@example.test"))
        assert new_staff is not None and new_staff.role == "consultant" and new_staff.is_active
        assert new_staff.password_hash.startswith("$argon2id$")
        assert verify_password("a-secure-new-passphrase", new_staff.password_hash)
        assert "a-secure-new-passphrase" not in created.text

        preauth, raw_preauth = issue_session(db_session, user_id=None)
        db_session.flush()
        login_client = TestClient(app, follow_redirects=False)
        login_client.cookies.set(SESSION_COOKIE_NAME, raw_preauth)
        try:
            login_page = login_client.get("/admin/login")
            assert login_page.status_code == 200
            login = login_client.post("/admin/login", data={
                "email": new_staff.email,
                "password": "a-secure-new-passphrase",
                "csrf_token": preauth.csrf_token,
            })
            assert login.status_code == 303
            assert login_client.get("/admin/packages").status_code == 200
        finally:
            login_client.close()

        role_changed = client.post(f"/admin/staff/{existing.id}/role", data={
            "csrf_token": csrf, "role": "consultant", "confirm_action": "yes",
        })
        assert role_changed.status_code == 303
        assert existing.role == "consultant"
        assert existing_session.revoked_at is not None

        deactivated = client.post(f"/admin/staff/{existing.id}/active", data={
            "csrf_token": csrf, "active": "false", "confirm_action": "yes",
        })
        assert deactivated.status_code == 303
        assert existing.is_active is False
        reactivated = client.post(f"/admin/staff/{existing.id}/active", data={
            "csrf_token": csrf, "active": "true", "confirm_action": "yes",
        })
        assert reactivated.status_code == 303
        assert existing.is_active is True

        target_session, _ = issue_session(db_session, user_id=existing.id)
        db_session.flush()
        reset = client.post(f"/admin/staff/{existing.id}/password", data={
            "csrf_token": csrf, "new_password": "another-secure-passphrase", "password_confirm": "another-secure-passphrase",
            "confirm_action": "yes",
        })
        assert reset.status_code == 303
        assert verify_password("another-secure-passphrase", existing.password_hash)
        assert target_session.revoked_at is not None
    finally:
        client.close()


def test_duplicate_email_password_mismatch_and_csrf_are_rejected(db_session: Session, staff_management_client_factory):
    admin = _user(db_session, "admin")
    existing = User(name="Existing editor", email="staff.editor@example.test", role="editor")
    db_session.add(existing)
    db_session.flush()
    client, csrf = staff_management_client_factory(admin)
    try:
        duplicate = client.post("/admin/staff", data={
            "csrf_token": csrf, "name": "Duplicate", "email": "STAFF.EDITOR@example.test",
            "role": "editor", "password": "a-secure-new-passphrase", "password_confirm": "a-secure-new-passphrase",
            "confirm_action": "yes",
        })
        assert duplicate.status_code == 422
        assert "already exists" in duplicate.text

        mismatch = client.post("/admin/staff", data={
            "csrf_token": csrf, "name": "Another", "email": "another@example.test",
            "role": "editor", "password": "a-secure-new-passphrase", "password_confirm": "different-secure-passphrase",
            "confirm_action": "yes",
        })
        assert mismatch.status_code == 422
        assert "do not match" in mismatch.text

        no_csrf = client.post("/admin/staff", data={"name": "No CSRF"})
        assert no_csrf.status_code == 403
    finally:
        client.close()


def test_self_deactivation_and_removal_of_last_active_admin_are_blocked(
    db_session: Session, staff_management_client_factory
):
    admin = _user(db_session, "admin")
    client, csrf = staff_management_client_factory(admin)
    try:
        self_deactivate = client.post(f"/admin/staff/{admin.id}/active", data={
            "csrf_token": csrf, "active": "false", "confirm_action": "yes",
        })
        assert self_deactivate.status_code == 409
        assert "cannot deactivate your own" in self_deactivate.text
        assert admin.is_active is True

        demote_last = client.post(f"/admin/staff/{admin.id}/role", data={
            "csrf_token": csrf, "role": "editor", "confirm_action": "yes",
        })
        assert demote_last.status_code == 409
        assert "last active Admin" in demote_last.text
        assert admin.role == "admin"
    finally:
        client.close()


@pytest.mark.parametrize("role", ["editor", "consultant"])
def test_non_admins_receive_403_for_every_staff_management_endpoint(
    db_session: Session, staff_management_client_factory, role: str
):
    user = _user(db_session, role)
    target = _user(db_session, "consultant")
    client, csrf = staff_management_client_factory(user)
    try:
        assert client.get("/admin/staff").status_code == 403
        posts = (
            ("/admin/staff", {"csrf_token": csrf}),
            (f"/admin/staff/{target.id}/role", {"csrf_token": csrf, "role": "editor"}),
            (f"/admin/staff/{target.id}/active", {"csrf_token": csrf, "active": "false"}),
            (f"/admin/staff/{target.id}/password", {"csrf_token": csrf, "new_password": "a-secure-new-passphrase"}),
        )
        for path, data in posts:
            assert client.post(path, data=data).status_code == 403
    finally:
        client.close()


def test_staff_navigation_link_is_admin_only(db_session: Session, staff_management_client_factory):
    admin_client, _ = staff_management_client_factory(_user(db_session, "admin"))
    editor_client, _ = staff_management_client_factory(_user(db_session, "editor"))
    consultant_client, _ = staff_management_client_factory(_user(db_session, "consultant"))
    try:
        assert 'href="/admin/staff"' in admin_client.get("/admin/packages").text
        assert 'href="/admin/staff"' not in editor_client.get("/admin/packages").text
        assert 'href="/admin/staff"' not in consultant_client.get("/admin/packages").text
    finally:
        admin_client.close()
        editor_client.close()
        consultant_client.close()


def test_inactive_admin_cannot_access_staff_management(db_session: Session, staff_management_client_factory):
    inactive_admin = _user(db_session, "admin", active=False)
    client, _ = staff_management_client_factory(inactive_admin)
    try:
        assert client.get("/admin/staff").status_code == 303
        assert client.post("/admin/staff", data={}).status_code == 303
    finally:
        client.close()
