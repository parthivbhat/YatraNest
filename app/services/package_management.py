"""Database persistence helpers for draft package creation and editing."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Package, PackageDraft, PackagePrice, User
from app.services.package_validation import PackagePayload, slugify_title, validate_package_slug


def package_code_exists(session: Session, package_code: str) -> bool:
    """Check codes case-insensitively; application-created codes are uppercase."""
    return (
        session.scalar(
            select(Package.id).where(func.lower(Package.package_code) == package_code.lower()).limit(1)
        )
        is not None
    )


def unique_package_slug(session: Session, base_slug: str) -> str:
    """Keep the title-derived slug readable while adding a suffix on collisions."""
    base_slug = validate_package_slug(base_slug)
    candidate = base_slug
    suffix_number = 2
    while session.scalar(select(Package.id).where(Package.slug == candidate).limit(1)) is not None:
        suffix = f"-{suffix_number}"
        candidate = validate_package_slug(f"{base_slug[: 180 - len(suffix)].rstrip('-')}{suffix}")
        suffix_number += 1
    return candidate


def create_draft_package(session: Session, payload: PackagePayload, staff: User) -> Package:
    """Add a new draft and its price rows to the current transaction.

    This function flushes for constraint feedback but intentionally does not
    commit. The route owns the transaction boundary for package plus prices.
    """
    package_values = payload.package_values()
    package_values["slug"] = unique_package_slug(session, payload.slug)
    package = Package(
        **package_values,
        status="draft",
        published_at=None,
        scheduled_publish_at=None,
        created_by_id=staff.id,
        updated_by_id=staff.id,
    )
    session.add(package)
    session.flush()
    package.prices.extend(price_rows_from_payload(payload))
    session.flush()
    return package


def update_draft_package(session: Session, package: Package, payload: PackagePayload, staff: User) -> None:
    """Replace draft content and all prices atomically within the caller transaction."""
    package_values = payload.package_values()
    # Code and slug are stable identity fields and are never taken from edits.
    package_values.pop("package_code")
    package_values.pop("slug")
    for field, value in package_values.items():
        setattr(package, field, value)

    package.prices.clear()
    session.flush()  # Delete old rows before inserting any reused season windows.
    package.prices.extend(price_rows_from_payload(payload))
    package.updated_by_id = staff.id
    # Price-only edits must also advance the package's optimistic concurrency token.
    package.updated_at = datetime.now(UTC)
    session.flush()


def duplicate_package(session: Session, source: Package, staff: User) -> Package:
    """Copy supported package content and prices into a new draft record.

    Package codes have no configured business numbering sequence. Duplicates
    therefore use a clearly marked ``DUP-`` code with a random hexadecimal
    suffix; the database unique constraint remains the final collision guard.
    The title-derived slug follows the existing collision-suffix convention.
    The caller owns the transaction and has loaded the source under a row lock.
    """
    code = f"DUP-{uuid4().hex[:12].upper()}"
    while package_code_exists(session, code):
        code = f"DUP-{uuid4().hex[:12].upper()}"

    base_slug = slugify_title(source.slug or source.title) or f"package-{source.id.hex[:8]}"
    base_slug = validate_package_slug(base_slug)
    slug = unique_package_slug(session, f"{base_slug[:174].rstrip('-')}-copy")

    duplicate = Package(
        package_code=code,
        slug=slug,
        title=source.title,
        summary=source.summary,
        description=source.description,
        destinations=deepcopy(source.destinations),
        duration_days=source.duration_days,
        hotel_category=source.hotel_category,
        hotel_details=deepcopy(source.hotel_details),
        itinerary=deepcopy(source.itinerary),
        inclusions=deepcopy(source.inclusions),
        map_url=source.map_url,
        status="draft",
        published_at=None,
        scheduled_publish_at=None,
        created_by_id=staff.id,
        updated_by_id=staff.id,
    )
    duplicate.prices = [
        PackagePrice(
            season_name=price.season_name,
            start_month=price.start_month,
            end_month=price.end_month,
            valid_from=price.valid_from,
            valid_until=price.valid_until,
            price_per_person=price.price_per_person,
            currency=price.currency,
            price_basis=price.price_basis,
            price_note=price.price_note,
        )
        for price in source.prices
    ]
    session.add(duplicate)
    session.flush()
    return duplicate


def save_pending_package_draft(
    session: Session,
    package: Package,
    payload: PackagePayload,
    staff: User,
    *,
    existing_draft: PackageDraft | None,
) -> PackageDraft:
    """Create or update a published package's pending payload only.

    The caller locks the Package row, checks both optimistic timestamps, and
    owns the database transaction. This function never changes Package or
    PackagePrice rows.
    """
    now = datetime.now(UTC)
    if existing_draft is None:
        pending_draft = PackageDraft(
            package_id=package.id,
            draft_payload=payload.to_json(),
            base_updated_at=package.updated_at,
            created_by_id=staff.id,
            updated_by_id=staff.id,
        )
        session.add(pending_draft)
    else:
        pending_draft = existing_draft
        pending_draft.draft_payload = payload.to_json()
        pending_draft.updated_by_id = staff.id
        # Keep optimistic versions strictly changing even when saves occur in
        # the same database timestamp tick.
        if now <= pending_draft.updated_at:
            now = pending_draft.updated_at + timedelta(microseconds=1)
        pending_draft.updated_at = now
    session.flush()
    return pending_draft


def price_rows_from_payload(payload: PackagePayload) -> list[PackagePrice]:
    """Create new live price rows from a canonical validated package payload."""
    return [
        PackagePrice(
            season_name=price.season_name,
            start_month=price.start_month,
            end_month=price.end_month,
            valid_from=price.valid_from,
            valid_until=price.valid_until,
            price_per_person=price.price_per_person,
            currency=price.currency,
            price_basis=price.price_basis,
            price_note=price.price_note,
        )
        for price in payload.prices
    ]
