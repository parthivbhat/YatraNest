"""Helpers for appending complete, immutable package history snapshots."""

from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Package, PackageVersion


def append_package_version(
    session: Session,
    package: Package,
    *,
    snapshot: dict[str, Any] | None = None,
    created_by_id: UUID | None = None,
    change_note: str | None = None,
) -> PackageVersion:
    """Append the next version number without changing any earlier version.

    If a historical snapshot is supplied, it is copied into a new immutable
    record. The package restoration service applies validated values to live
    Package rows and then calls this helper within the same transaction.
    """
    session.flush()
    latest_number = session.scalar(
        select(func.max(PackageVersion.version_number)).where(PackageVersion.package_id == package.id)
    )
    version = PackageVersion(
        package=package,
        version_number=(latest_number or 0) + 1,
        snapshot=deepcopy(snapshot if snapshot is not None else package.to_snapshot()),
        created_by_id=created_by_id,
        change_note=change_note,
    )
    session.add(version)
    session.flush()
    return version
