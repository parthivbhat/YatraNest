"""Argon2 password hashing and verification helpers."""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError


password_hasher = PasswordHasher()
# Verify a dummy hash when the email is absent so that unknown users and wrong
# passwords take approximately the same password-hashing path.
_DUMMY_PASSWORD_HASH = password_hasher.hash(secrets.token_urlsafe(32))


def hash_password(password: str) -> str:
    """Return an Argon2id hash. The input password is never stored or logged."""
    if not password:
        raise ValueError("Password cannot be empty.")
    return password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password, returning False for missing or malformed hashes."""
    hash_to_check = password_hash or _DUMMY_PASSWORD_HASH
    try:
        valid = password_hasher.verify(hash_to_check, password)
    except (InvalidHashError, VerificationError, VerifyMismatchError):
        return False
    return valid and password_hash is not None


def password_hash_needs_rehash(password_hash: str) -> bool:
    """Check whether stored Argon2 parameters should be upgraded on login."""
    try:
        return password_hasher.check_needs_rehash(password_hash)
    except (InvalidHashError, VerificationError):
        return True
