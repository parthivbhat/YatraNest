"""Reusable staff authentication and server-side role dependencies."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, UserSession
from app.services.session_store import get_authenticated_session
from app.services.staff_accounts import STAFF_ROLES


def get_current_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> UserSession:
    """Resolve an active PostgreSQL-backed staff session or reject the request."""
    return get_authenticated_session(request, response, db)


def get_current_staff(
    session_record: Annotated[UserSession, Depends(get_current_session)],
    db: Session = Depends(get_db),
) -> User:
    """Return the active staff member associated with the authenticated session."""
    user = db.get(User, session_record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Staff session is no longer valid.")
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Staff role is not permitted.")
    return user


def require_roles(*allowed_roles: str) -> Callable[..., User]:
    """Build a dependency that enforces the allowed roles on the server."""
    allowed = frozenset(allowed_roles)
    if not allowed or not allowed.issubset(STAFF_ROLES):
        raise ValueError("At least one valid staff role is required.")

    def role_dependency(
        staff: Annotated[User, Depends(get_current_staff)],
    ) -> User:
        if staff.role not in allowed:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action.")
        return staff

    return role_dependency


require_editor_or_admin = require_roles("admin", "editor")
require_admin = require_roles("admin")


def get_staff_page_user(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    """Resolve a dashboard user, redirecting expired or missing sessions to login."""
    try:
        session_record = get_authenticated_session(request, response, db)
    except HTTPException as exc:
        if exc.status_code == 401:
            raise HTTPException(
                status_code=303,
                headers={"Location": "/admin/login"},
                detail="Redirecting to staff sign in.",
            ) from exc
        raise

    user = db.get(User, session_record.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    if user.role not in STAFF_ROLES:
        raise HTTPException(status_code=403, detail="Staff role is not permitted.")
    request.state.staff_session = session_record
    return user


def get_editor_page_user(
    staff: Annotated[User, Depends(get_staff_page_user)],
) -> User:
    """Require an admin or editor for staff pages that can change packages."""
    if staff.role not in {"admin", "editor"}:
        raise HTTPException(status_code=403, detail="You do not have permission to manage packages.")
    return staff


def get_admin_page_user(
    staff: Annotated[User, Depends(get_staff_page_user)],
) -> User:
    """Require an admin for review and approval pages while retaining login redirects."""
    if staff.role != "admin":
        raise HTTPException(status_code=403, detail="Administrator access is required.")
    return staff
