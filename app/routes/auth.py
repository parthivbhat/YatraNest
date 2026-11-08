"""Staff sign-in and sign-out routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserSession
from app.services.authentication import hash_password, password_hash_needs_rehash, verify_password
from app.services.authorization import get_current_session
from app.services.session_store import (
    ABSOLUTE_LIFETIME,
    PREAUTH_LIFETIME,
    SESSION_COOKIE_NAME,
    clear_session_cookie,
    find_session,
    issue_session,
    require_valid_csrf,
    revoke_session,
    session_expired,
    set_session_cookie,
    utc_now,
)
from app.templating import templates


router = APIRouter(prefix="/admin", tags=["staff authentication"])


def _login_template(
    request: Request,
    *,
    csrf_token: str,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    response = templates.TemplateResponse(
        request=request,
        name="auth/login.html",
        context={
            "page_title": "Staff sign in | YatraNest",
            "meta_description": "Sign in to the YatraNest staff area.",
            "canonical_url": f"{str(request.base_url).rstrip('/')}/admin/login",
            "robots": "noindex, nofollow",
            "csrf_token": csrf_token,
            "error": error,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/login", response_class=HTMLResponse, name="staff_login")
def login_page(request: Request, db: Session = Depends(get_db)) -> Response:
    """Render a CSRF-protected login form, creating a short-lived pre-login session."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    session_record = find_session(db, raw_token)
    now = utc_now()

    if session_record is not None and not session_expired(session_record, now=now):
        if session_record.user_id is not None:
            user = db.get(User, session_record.user_id)
            if user is not None and user.is_active:
                return RedirectResponse(url="/admin/packages", status_code=303)
        else:
            response = _login_template(request, csrf_token=session_record.csrf_token)
            set_session_cookie(response, raw_token, expires_at=session_record.expires_at)
            return response
    elif session_record is not None:
        revoke_session(session_record, now=now)

    session_record, raw_token = issue_session(
        db,
        user_id=None,
        lifetime=PREAUTH_LIFETIME,
        now=now,
    )
    db.commit()

    response = _login_template(request, csrf_token=session_record.csrf_token)
    set_session_cookie(response, raw_token, expires_at=session_record.expires_at)
    return response


@router.post("/login", response_class=HTMLResponse, name="staff_login_submit")
def login_submit(
    request: Request,
    email: Annotated[str, Form(max_length=320)],
    password: Annotated[str, Form(max_length=1024)],
    csrf_token: Annotated[str, Form(max_length=128)],
    db: Session = Depends(get_db),
) -> Response:
    """Validate a staff login, rotate the pre-login token, and set a new session."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    preauth_session = find_session(db, raw_token)
    now = utc_now()
    if (
        preauth_session is None
        or preauth_session.user_id is not None
        or session_expired(preauth_session, now=now)
    ):
        if preauth_session is not None:
            revoke_session(preauth_session, now=now)
            db.commit()
        raise HTTPException(status_code=403, detail="Login form expired. Reload the sign-in page.")

    require_valid_csrf(preauth_session, csrf_token)

    normalized_email = email.strip().lower()
    user = db.scalar(select(User).where(func.lower(User.email) == normalized_email))
    stored_hash = user.password_hash if user is not None else None
    password_matches = verify_password(password, stored_hash)
    if user is None or not user.is_active or not password_matches:
        return _login_template(
            request,
            csrf_token=preauth_session.csrf_token,
            error="Email or password is incorrect.",
            status_code=401,
        )

    if user.password_hash and password_hash_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)

    # Revoke the pre-auth token and create a fresh authenticated token in one
    # transaction to prevent session fixation.
    revoke_session(preauth_session, now=now)
    authenticated_session, new_raw_token = issue_session(
        db,
        user_id=user.id,
        lifetime=ABSOLUTE_LIFETIME,
        now=now,
    )
    db.commit()

    response = RedirectResponse(url="/admin/packages", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    set_session_cookie(response, new_raw_token, expires_at=authenticated_session.expires_at)
    return response


@router.post("/logout", name="staff_logout")
def logout(
    csrf_token: Annotated[str, Form(max_length=128)],
    session_record: UserSession = Depends(get_current_session),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Revoke the server-side session and clear its browser cookie."""
    require_valid_csrf(session_record, csrf_token)
    revoke_session(session_record)
    db.commit()

    response = RedirectResponse(url="/admin/login", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    clear_session_cookie(response)
    return response
