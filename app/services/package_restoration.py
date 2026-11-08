"""Transactional restoration of validated immutable package snapshots."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Package, PackageDraft, PackageVersion, User
from app.services.package_management import price_rows_from_payload
from app.services.package_validation import PackageValidationError, parse_package_payload
from app.services.package_versions import append_package_version


class PackageRestoreError(Exception):
    """Safe error for a restore request that cannot be applied."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class PackageRestoreValidationError(PackageRestoreError):
    """Historical snapshot validation error suitable for a staff page."""

    def __init__(self, errors: dict[str, list[str]]) -> None:
        self.errors = errors
        super().__init__("This historical snapshot is incomplete or invalid. No changes were saved.", status_code=422)


def restore_package_version(
    session: Session,
    *,
    package_id: UUID,
    version_id: UUID,
    staff: User,
    expected_updated_at: str | None,
) -> tuple[Package, PackageVersion, PackageVersion]:
    """Restore snapshot content and prices while retaining live workflow identity.

    The caller owns the transaction. Package is locked before PackageDraft so
    restoration serializes with edits, publish, unpublish, and other restores.
    """
    package = session.scalar(
        select(Package)
        .options(selectinload(Package.prices))
        .where(Package.id == package_id)
        .with_for_update()
    )
    if package is None:
        raise PackageRestoreError("Package not found.", status_code=404)

    pending = session.scalar(
        select(PackageDraft)
        .where(PackageDraft.package_id == package_id)
        .with_for_update()
    )

    if _parse_timestamp(expected_updated_at) != package.updated_at:
        raise PackageRestoreError("The package changed after this page was opened. Reload and review it again.")
    if package.status == "archived":
        raise PackageRestoreError("Archived packages cannot be restored.")
    if package.status not in {"draft", "published"}:
        raise PackageRestoreError("This package is not in a restorable state.")
    if pending is not None:
        raise PackageRestoreError("Resolve the pending package changes before restoring a version.")

    source = session.scalar(
        select(PackageVersion)
        .where(PackageVersion.id == version_id, PackageVersion.package_id == package_id)
    )
    if source is None:
        raise PackageRestoreError("Package version not found.", status_code=404)
    if not isinstance(source.snapshot, dict):
        raise PackageRestoreValidationError({"snapshot": ["The historical snapshot is not a valid object."]})

    try:
        payload = parse_package_payload(
            source.snapshot,
            existing_package_code=package.package_code,
            existing_slug=package.slug,
        )
    except PackageValidationError as exc:
        raise PackageRestoreValidationError(exc.errors) from exc
    except (AttributeError, TypeError, ValueError) as exc:
        raise PackageRestoreValidationError({"snapshot": ["The historical snapshot could not be parsed."]}) from exc
    if not payload.prices:
        raise PackageRestoreValidationError(
            {"prices": ["The historical snapshot has no valid seasonal prices and cannot be restored."]}
        )

    # Snapshot the complete current state before changing anything. Status,
    # publication timestamps, schedule, identity, and creator are not restored.
    before_version = append_package_version(
        session,
        package,
        created_by_id=staff.id,
        change_note=f"Before restoring version {source.version_number}",
    )

    package_values = payload.package_values()
    package_values.pop("package_code")
    package_values.pop("slug")
    for field, value in package_values.items():
        setattr(package, field, value)

    package.prices.clear()
    session.flush()
    package.prices.extend(price_rows_from_payload(payload))
    package.updated_by_id = staff.id
    now = datetime.now(UTC)
    if now <= package.updated_at:
        now = package.updated_at + timedelta(microseconds=1)
    package.updated_at = now
    session.flush()

    restored_version = append_package_version(
        session,
        package,
        created_by_id=staff.id,
        change_note=f"Restored from version {source.version_number}",
    )
    return package, before_version, restored_version


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None
