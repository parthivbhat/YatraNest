"""Idempotently seed the two packages supplied in the assignment.

Run with ``python -m app.seed_data`` after applying Alembic migrations.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import Package, PackagePrice, PackageVersion
from app.services.package_versions import append_package_version


SAMPLE_PACKAGES = (
    {
        "package_code": "PKG-KL-07",
        "slug": "munnar-thekkady-alleppey-houseboat",
        "title": "Munnar, Thekkady and Alleppey houseboat",
        "duration_days": 6,
        "hotel_category": "4-star",
        "status": "published",
        "prices": (
            {"season_name": "Peak season", "start_month": 10, "end_month": 3, "price_per_person": Decimal("32500.00")},
            {"season_name": "Monsoon season", "start_month": 6, "end_month": 9, "price_per_person": Decimal("24900.00")},
        ),
    },
    {
        "package_code": "PKG-SI-12",
        "slug": "madurai-rameswaram-kanyakumari",
        "title": "Madurai, Rameswaram and Kanyakumari",
        "duration_days": 5,
        "hotel_category": "3-star",
        "status": "draft",
        "prices": (
            {"season_name": "All year", "start_month": 1, "end_month": 12, "price_per_person": Decimal("18750.00")},
        ),
    },
)


def seed_packages(session: Session) -> list[Package]:
    """Insert or synchronize the supplied packages without creating duplicates."""
    seeded: list[Package] = []

    for fixture in SAMPLE_PACKAGES:
        package = session.scalar(select(Package).where(Package.package_code == fixture["package_code"]))
        if package is None:
            package = Package(
                package_code=fixture["package_code"],
                slug=fixture["slug"],
                title=fixture["title"],
                duration_days=fixture["duration_days"],
                hotel_category=fixture["hotel_category"],
                status=fixture["status"],
            )
            session.add(package)
            session.flush()
        else:
            package.slug = fixture["slug"]
            package.title = fixture["title"]
            package.duration_days = fixture["duration_days"]
            package.hotel_category = fixture["hotel_category"]
            package.status = fixture["status"]

        if package.status == "published" and package.published_at is None:
            from datetime import UTC, datetime

            package.published_at = datetime.now(UTC)

        for price_fixture in fixture["prices"]:
            price = next(
                (
                    item
                    for item in package.prices
                    if item.season_name == price_fixture["season_name"]
                    and item.start_month == price_fixture["start_month"]
                    and item.end_month == price_fixture["end_month"]
                ),
                None,
            )
            if price is None:
                price = PackagePrice(package=package, **price_fixture)
                session.add(price)
            else:
                price.price_per_person = price_fixture["price_per_person"]
                price.currency = "INR"
                price.price_basis = "per_person"

        session.flush()
        latest_version = session.scalar(
            select(PackageVersion)
            .where(PackageVersion.package_id == package.id)
            .order_by(PackageVersion.version_number.desc())
            .limit(1)
        )
        current_snapshot = package.to_snapshot()
        if latest_version is None or latest_version.snapshot != current_snapshot:
            append_package_version(
                session,
                package,
                change_note="Initial sample data" if latest_version is None else "Sample data synchronized",
            )
        seeded.append(package)

    session.flush()
    return seeded


def main() -> None:
    """Run the seed in one transaction so partial data is never left behind."""
    with SessionLocal.begin() as session:
        packages = seed_packages(session)
    print(f"Seeded {len(packages)} supplied packages.")
    for package in packages:
        print(f"{package.package_code}: {package.status}, {len(package.prices)} seasonal price(s)")


if __name__ == "__main__":
    main()
