"""Explicit staff account creation; no default credentials are supplied."""

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import User
from app.services.authentication import hash_password


STAFF_ROLES = {"admin", "editor", "consultant"}
MINIMUM_PASSWORD_LENGTH = 12
_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def create_staff_account(
    db: Session,
    *,
    name: str,
    email: str,
    role: str,
    password: str,
) -> User:
    """Validate and add a staff user with an Argon2 password hash."""
    normalized_name = name.strip()
    normalized_email = email.strip().lower()
    normalized_role = role.strip().lower()

    if not normalized_name:
        raise ValueError("Name is required.")
    if len(normalized_email) > 320 or not _EMAIL_RE.fullmatch(normalized_email):
        raise ValueError("Enter a valid email address.")
    if normalized_role not in STAFF_ROLES:
        raise ValueError("Role must be admin, editor, or consultant.")
    if len(password) < MINIMUM_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MINIMUM_PASSWORD_LENGTH} characters.")
    if db.scalar(select(User.id).where(func.lower(User.email) == normalized_email)) is not None:
        raise ValueError("An account with that email already exists.")

    user = User(
        name=normalized_name,
        email=normalized_email,
        role=normalized_role,
        password_hash=hash_password(password),
        is_active=True,
    )
    db.add(user)
    db.flush()
    return user
