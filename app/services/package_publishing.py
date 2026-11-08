"""Transactional package publication and unpublication workflows."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Package, PackageDraft, User
from app.services.package_management import price_rows_from_payload
from app.services.package_validation import PackageValidationError, parse_package_payload
from app.services.package_versions import append_package_version


class PackageWorkflowError(Exception):
    """Safe workflow error that can be shown to staff without database detail."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def publish_package(
    session: Session,
    *,
    package_id: UUID,
    staff: User,
    expected_updated_at: str | None = None,
    expected_base_updated_at: str | None = None,
    expected_draft_updated_at: str | None = None,
) -> Package:
    """Publish a draft or apply pending edits in the caller's transaction.

    The caller owns the transaction. This function locks Package before its
    PackageDraft, validates the whole snapshot, then changes package content,
    prices, publication state, pending state, and version snapshot atomically.
    """
    package = session.scalar(
        select(Package)
        .options(selectinload(Package.prices))
        .where(Package.id == package_id)
        .with_for_update()
    )
    if package is None:
        raise PackageWorkflowError("Package not found.", status_code=404)

    pending = session.scalar(
        select(PackageDraft)
        .where(PackageDraft.package_id == package.id)
        .with_for_update()
    )

    if not package.slug:
        raise PackageWorkflowError("This package has no stable URL slug and cannot be published.")

    if package.status == "draft":
        if pending is not None:
            raise PackageWorkflowError("This draft has an unexpected pending change record. Reload and contact an administrator.")
        _require_fresh_timestamp(expected_updated_at, package.updated_at)
        snapshot = package.to_snapshot()
    elif package.status == "published":
        if pending is None:
            raise PackageWorkflowError("There are no pending changes to publish.")
        _require_fresh_timestamp(expected_base_updated_at, package.updated_at)
        _require_fresh_timestamp(expected_draft_updated_at, pending.updated_at)
        if pending.base_updated_at != package.updated_at:
            raise PackageWorkflowError("The published package changed since these pending edits were started. Reload and review again.")
        if _as_timestamp(expected_base_updated_at) != pending.base_updated_at:
            raise PackageWorkflowError("The package review is stale. Reload before publishing.")
        snapshot = pending.draft_payload
    else:
        raise PackageWorkflowError("Only draft or published packages can be published.")

    payload = parse_package_payload(
        snapshot,
        existing_package_code=package.package_code,
        existing_slug=package.slug,
    )
    if not payload.prices:
        raise PackageValidationError(
            {"prices": ["Add at least one valid seasonal price before publishing."]},
            {"title": payload.title},
        )

    package_values = payload.package_values()
    # Identity remains sourced from the locked Package row.
    package_values.pop("package_code")
    package_values.pop("slug")
    for field, value in package_values.items():
        setattr(package, field, value)

    package.prices.clear()
    session.flush()  # Remove old season keys before inserting validated replacements.
    package.prices.extend(price_rows_from_payload(payload))
    package.status = "published"
    package.scheduled_publish_at = None
    package.published_at = datetime.now(UTC)
    package.updated_by_id = staff.id
    package.updated_at = datetime.now(UTC)
    session.flush()

    if pending is not None:
        session.delete(pending)
        session.flush()

    append_package_version(
        session,
        package,
        created_by_id=staff.id,
        change_note="Package published",
    )
    return package


def unpublish_package(
    session: Session,
    *,
    package_id: UUID,
    staff: User,
    expected_updated_at: str | None,
) -> Package:
    """Move a Published package to Draft without changing its content or prices."""
    package = session.scalar(
        select(Package)
        .options(selectinload(Package.prices))
        .where(Package.id == package_id)
        .with_for_update()
    )
    if package is None:
        raise PackageWorkflowError("Package not found.", status_code=404)

    pending = session.scalar(
        select(PackageDraft)
        .where(PackageDraft.package_id == package.id)
        .with_for_update()
    )
    if package.status != "published":
        raise PackageWorkflowError("Only published packages can be unpublished.")
    if pending is not None:
        raise PackageWorkflowError("Resolve the pending changes before unpublishing this package.")
    _require_fresh_timestamp(expected_updated_at, package.updated_at)

    package.status = "draft"
    package.published_at = None
    package.scheduled_publish_at = None
    package.updated_by_id = staff.id
    package.updated_at = datetime.now(UTC)
    session.flush()

    append_package_version(
        session,
        package,
        created_by_id=staff.id,
        change_note="Package unpublished",
    )
    return package


def _require_fresh_timestamp(submitted: str | None, actual: datetime) -> None:
    if _as_timestamp(submitted) != actual:
        raise PackageWorkflowError("This package changed after review. Reload the review page before continuing.")


def _as_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None
