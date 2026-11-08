"""Interactively create one staff account without default credentials.

Run with ``python -m app.create_staff``. Passwords are prompted without echo
and are never accepted as command-line arguments or printed.
"""

from getpass import getpass

from sqlalchemy.exc import IntegrityError

from app.database import SessionLocal
from app.services.staff_accounts import STAFF_ROLES, create_staff_account


def main() -> None:
    print("Create a staff account")
    name = input("Name: ")
    email = input("Email: ")
    role = input(f"Role ({', '.join(sorted(STAFF_ROLES))}): ").strip().lower()
    password = getpass("Password (minimum 12 characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords did not match; no account was created.")

    try:
        with SessionLocal.begin() as db:
            user = create_staff_account(
                db,
                name=name,
                email=email,
                role=role,
                password=password,
            )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    except IntegrityError as exc:
        raise SystemExit("An account with that email already exists.") from exc

    print(f"Created {user.role} account for {user.email}.")


if __name__ == "__main__":
    main()
