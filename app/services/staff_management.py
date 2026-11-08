"""Admin-only staff account listing and access management."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import User, UserSession
from app.services.authentication import hash_password
from app.services.session_store import utc_now
from app.services.staff_accounts import MINIMUM_PASSWORD_LENGTH, STAFF_ROLES, create_staff_account


class StaffManagementError(ValueError):
    def __init__(self, message: str, status_code: int = 422):
        self.status_code = status_code
        super().__init__(message)


def list_staff_accounts(session: Session):
    """Select display fields only; password hashes never enter the view model."""
    return list(session.execute(
        select(User.id, User.name, User.email, User.role, User.is_active, User.created_at)
        .order_by(func.lower(User.name), func.lower(User.email))
    ).all())


def _lock_active_admins(session: Session, actor_id: UUID) -> list[User]:
    """Serialize access-changing operations and validate the current actor from DB."""
    admins = list(session.scalars(
        select(User)
        .where(User.role == "admin", User.is_active.is_(True))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ))
    if not any(admin.id == actor_id for admin in admins):
        raise StaffManagementError("Administrator access is required.", 403)
    return admins


def _target_for_update(session: Session, staff_id: UUID) -> User:
    target = session.scalar(
        select(User).where(User.id == staff_id).with_for_update().execution_options(populate_existing=True)
    )
    if target is None:
        raise StaffManagementError("Staff account not found.", 404)
    return target


def _revoke_sessions(session: Session, staff_id: UUID) -> None:
    session.execute(
        update(UserSession)
        .where(UserSession.user_id == staff_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utc_now())
    )


def create_managed_staff(
    session: Session, *, actor_id: UUID, name: str, email: str, role: str, password: str
) -> User:
    _lock_active_admins(session, actor_id)
    try:
        return create_staff_account(session, name=name, email=email, role=role, password=password)
    except ValueError as exc:
        raise StaffManagementError(str(exc), 422) from None


def change_staff_role(session: Session, *, actor_id: UUID, staff_id: UUID, role: str) -> User:
    if role not in STAFF_ROLES:
        raise StaffManagementError("Choose Admin, Editor or Consultant.")
    active_admins = _lock_active_admins(session, actor_id)
    target = _target_for_update(session, staff_id)
    if target.role == "admin" and target.is_active and role != "admin" and len(active_admins) <= 1:
        raise StaffManagementError("The last active Admin cannot be demoted.", 409)
    if target.role != role:
        target.role = role
        _revoke_sessions(session, target.id)
        session.flush()
    return target


def set_staff_active(
    session: Session, *, actor_id: UUID, staff_id: UUID, is_active: bool
) -> User:
    active_admins = _lock_active_admins(session, actor_id)
    target = _target_for_update(session, staff_id)
    if target.id == actor_id and not is_active:
        raise StaffManagementError("You cannot deactivate your own account.", 409)
    if target.is_active and not is_active and target.role == "admin" and len(active_admins) <= 1:
        raise StaffManagementError("The last active Admin cannot be deactivated.", 409)
    if target.is_active != is_active:
        target.is_active = is_active
        _revoke_sessions(session, target.id)
        session.flush()
    return target


def reset_staff_password(
    session: Session, *, actor_id: UUID, staff_id: UUID, new_password: str
) -> User:
    if len(new_password) < MINIMUM_PASSWORD_LENGTH:
        raise StaffManagementError(f"Password must be at least {MINIMUM_PASSWORD_LENGTH} characters.")
    _lock_active_admins(session, actor_id)
    target = _target_for_update(session, staff_id)
    target.password_hash = hash_password(new_password)
    _revoke_sessions(session, target.id)
    session.flush()
    return target
