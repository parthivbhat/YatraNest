"""Read-only queries shared by public website routes."""

from calendar import monthrange
from datetime import date

from sqlalchemy import String, and_, cast, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql import Select

from app.models import Package, PackagePrice


def published_packages_statement() -> Select[tuple[Package]]:
    """Start every public package query with the published-only rule."""
    return (
        select(Package)
        .where(Package.status == "published")
        .where(Package.slug.is_not(None))
        .options(selectinload(Package.prices))
    )


def search_published_packages(
    session: Session,
    *,
    query: str | None = None,
    travel_month: int | None = None,
    travel_year: int | None = None,
) -> list[Package]:
    """Search published packages and optionally match a recurring price season."""
    statement = published_packages_statement()

    search_term = (query or "").strip()
    if search_term:
        escaped_term = search_term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_term}%"
        statement = statement.where(
            or_(
                Package.package_code.ilike(pattern, escape="\\"),
                Package.title.ilike(pattern, escape="\\"),
                Package.summary.ilike(pattern, escape="\\"),
                Package.description.ilike(pattern, escape="\\"),
                cast(Package.destinations, String).ilike(pattern, escape="\\"),
            )
        )

    if travel_month is not None:
        month_matches = or_(
            and_(PackagePrice.start_month <= travel_month, PackagePrice.end_month >= travel_month),
            and_(
                PackagePrice.start_month > PackagePrice.end_month,
                or_(PackagePrice.start_month <= travel_month, PackagePrice.end_month >= travel_month),
            ),
        )
        price_conditions = [month_matches]
        if travel_year is not None:
            month_start = date(travel_year, travel_month, 1)
            month_end = date(travel_year, travel_month, monthrange(travel_year, travel_month)[1])
            price_conditions.extend(
                [
                    or_(PackagePrice.valid_from.is_(None), PackagePrice.valid_from <= month_end),
                    or_(PackagePrice.valid_until.is_(None), PackagePrice.valid_until >= month_start),
                ]
            )
        statement = statement.where(Package.prices.any(and_(*price_conditions)))

    return list(session.scalars(statement.order_by(Package.title.asc())).unique())


def get_published_package(session: Session, slug: str) -> Package | None:
    """Fetch a single published package and its prices; drafts resolve as absent."""
    statement = published_packages_statement().where(Package.slug == slug)
    return session.scalar(statement)
