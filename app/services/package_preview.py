"""Typed presentation data for private draft and pending-package previews."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models import Package


@dataclass(frozen=True)
class PricePreview:
    season_name: str
    start_month: int
    end_month: int
    valid_from: date | None
    valid_until: date | None
    price_per_person: Decimal
    currency: str
    price_basis: str
    price_note: str | None


@dataclass(frozen=True)
class PackagePreview:
    package_code: str
    slug: str | None
    title: str
    summary: str | None
    description: str | None
    destinations: list[str] | None
    duration_days: int | None
    hotel_category: str | None
    hotel_details: list[dict[str, Any]] | None
    itinerary: list[dict[str, Any]] | None
    inclusions: list[str] | None
    map_url: str | None
    prices: tuple[PricePreview, ...]


def preview_from_package(package: Package) -> PackagePreview:
    """Build public-presentation data from the package's current stored rows."""
    return PackagePreview(
        package_code=package.package_code,
        slug=package.slug,
        title=package.title,
        summary=package.summary,
        description=package.description,
        destinations=package.destinations,
        duration_days=package.duration_days,
        hotel_category=package.hotel_category,
        hotel_details=package.hotel_details,
        itinerary=package.itinerary,
        inclusions=package.inclusions,
        map_url=package.map_url,
        prices=tuple(
            PricePreview(
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
            for price in package.prices
        ),
    )


def preview_from_payload(
    payload: dict[str, Any],
    *,
    package_code: str,
    slug: str | None,
) -> PackagePreview:
    """Convert JSONB string values into date/Decimal types used by Jinja filters."""
    raw_prices = payload.get("prices") or []
    prices = tuple(
        PricePreview(
            season_name=str(price["season_name"]),
            start_month=int(price["start_month"]),
            end_month=int(price["end_month"]),
            valid_from=_parse_date(price.get("valid_from")),
            valid_until=_parse_date(price.get("valid_until")),
            price_per_person=_parse_decimal(price["price_per_person"]),
            currency=str(price.get("currency") or "INR"),
            price_basis=str(price.get("price_basis") or "per_person"),
            price_note=price.get("price_note"),
        )
        for price in raw_prices
    )
    duration = payload.get("duration_days")
    return PackagePreview(
        # Identity always comes from the live row, never the mutable JSON payload.
        package_code=package_code,
        slug=slug,
        title=str(payload["title"]),
        summary=payload.get("summary"),
        description=payload.get("description"),
        destinations=payload.get("destinations"),
        duration_days=int(duration) if duration is not None else None,
        hotel_category=payload.get("hotel_category"),
        hotel_details=payload.get("hotel_details"),
        itinerary=payload.get("itinerary"),
        inclusions=payload.get("inclusions"),
        map_url=payload.get("map_url"),
        prices=prices,
    )


def _parse_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _parse_decimal(value: Any) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Pending package contains an invalid price value.") from exc
    if not parsed.is_finite():
        raise ValueError("Pending package contains an invalid price value.")
    return parsed
