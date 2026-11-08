"""PostgreSQL-backed opaque staff sessions and CSRF tokens."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, UserSession


SESSION_COOKIE_NAME = "isocode_staff_session"
IDLE_TIMEOUT = timedelta(minutes=30)
ABSOLUTE_LIFETIME = timedelta(hours=8)
PREAUTH_LIFETIME = timedelta(minutes=15)


def utc_now() -> datetime:
    return datetime.now(UTC)


def hash_session_token(token: str) -> str:
    """Hash the opaque cookie value before it is persisted in PostgreSQL."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_session(
    db: Session,
    *,
    user_id: UUID | None,
    lifetime: timedelta = ABSOLUTE_LIFETIME,
    now: datetime | None = None,
) -> tuple[UserSession, str]:
    """Create a server-side session and return its one-time raw cookie token."""
    created_at = now or utc_now()
    raw_token = secrets.token_urlsafe(32)
    session_record = UserSession(
        user_id=user_id,
        token_hash=hash_session_token(raw_token),
        csrf_token=secrets.token_urlsafe(32),
        created_at=created_at,
        last_activity_at=created_at,
        expires_at=created_at + lifetime,
    )
    db.add(session_record)
    db.flush()
    return session_record, raw_token


def find_session(db: Session, raw_token: str | None) -> UserSession | None:
    """Look up a session using only the hash of the browser cookie."""
    if not raw_token:
        return None
    return db.scalar(select(UserSession).where(UserSession.token_hash == hash_session_token(raw_token)))


def session_expired(session_record: UserSession, *, now: datetime | None = None) -> bool:
    """Enforce both the absolute lifetime and the 30-minute idle timeout."""
    current_time = now or utc_now()
    return (
        session_record.revoked_at is not None
        or session_record.expires_at <= current_time
        or session_record.last_activity_at <= current_time - IDLE_TIMEOUT
    )


def revoke_session(session_record: UserSession, *, now: datetime | None = None) -> None:
    """Mark a session unusable without deleting its audit record."""
    if session_record.revoked_at is None:
        session_record.revoked_at = now or utc_now()


def verify_csrf_token(session_record: UserSession, submitted_token: str | None) -> bool:
    """Compare the form token in constant time with its server-side value."""
    if not submitted_token:
        return False
    return hmac.compare_digest(session_record.csrf_token, submitted_token)


def require_valid_csrf(session_record: UserSession, submitted_token: str | None) -> None:
    if not verify_csrf_token(session_record, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token.")


def cookie_is_secure() -> bool:
    """Use HTTPS-only cookies outside local development and tests."""
    return settings.app_env.lower() not in {"development", "test"}


def set_session_cookie(response: Response, raw_token: str, *, expires_at: datetime) -> None:
    max_age = max(0, int((expires_at - utc_now()).total_seconds()))
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=max_age,
        httponly=True,
        secure=cookie_is_secure(),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=cookie_is_secure(),
        samesite="lax",
        path="/",
    )


def get_authenticated_session(
    request: Request,
    response: Response,
    db: Session,
) -> UserSession:
    """Resolve, validate and touch the current authenticated staff session."""
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    session_record = find_session(db, raw_token)
    now = utc_now()

    if session_record is None:
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Staff sign-in required.")

    if session_expired(session_record, now=now):
        revoke_session(session_record, now=now)
        db.commit()
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Staff session expired. Sign in again.")

    if session_record.user_id is None:
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Staff sign-in required.")

    user = db.get(User, session_record.user_id)
    if user is None or not user.is_active:
        revoke_session(session_record, now=now)
        db.commit()
        clear_session_cookie(response)
        raise HTTPException(status_code=401, detail="Staff session is no longer valid.")

    session_record.last_activity_at = now
    db.commit()
    return session_record
