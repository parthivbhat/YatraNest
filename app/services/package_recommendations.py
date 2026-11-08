"""Deterministic package matching using only published package and price rows."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Package, PackagePrice
from app.schemas.ai_extraction import EnquiryExtraction
from app.services.public_packages import published_packages_statement


MAX_RECOMMENDATIONS = 3
_MAX_PRICE_MATCH_DAYS = 366
_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_MONTH_PATTERN = re.compile(r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b", re.I)
_GENERIC_TITLE_TERMS = frozenset(
    {
        "hill", "beach", "lake", "retreat", "tour", "holiday", "houseboat", "journey",
        "package", "hotel", "resort", "trip", "stay", "adventure", "escape", "getaway",
    }
)


@dataclass(frozen=True)
class VerifiedPrice:
    price_id: str
    season_name: str
    price_per_person: str
    currency: str
    price_basis: str
    start_month: int
    end_month: int
    valid_from: date | None
    valid_until: date | None
    price_note: str | None


@dataclass(frozen=True)
class PackageRecommendation:
    package_id: str
    package_code: str
    title: str
    destinations: tuple[str, ...]
    duration_days: int | None
    score: int
    match_reasons: tuple[str, ...]
    verified_price: VerifiedPrice | None
    price_status: str
    season_match: str | None
    caveats: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationResult:
    recommendations: tuple[PackageRecommendation, ...]
    explanation: str


def _normalize_destination(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold().replace("&", " and ")
    return " ".join(re.findall(r"[a-z0-9]+", normalized))


def _destination_match(requested: str | None, package: Package) -> bool:
    if not requested:
        return False
    wanted = _normalize_destination(requested)
    if not wanted:
        return False
    candidates = [value for value in (package.destinations or []) if isinstance(value, str)]
    for candidate in candidates:
        normalized = _normalize_destination(candidate)
        if normalized == wanted or re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?![a-z0-9])", normalized):
            return True
    # Package titles are supported factual content; matching the entire
    # normalized destination phrase as a word-bounded phrase avoids generic
    # token overlap (for example, matching only "beach" or "hill").
    if set(wanted.split()).issubset(_GENERIC_TITLE_TERMS):
        return False
    normalized_title = _normalize_destination(package.title)
    return re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?![a-z0-9])", normalized_title) is not None


def _month_matches(price: PackagePrice, month: int) -> bool:
    if price.start_month <= price.end_month:
        return price.start_month <= month <= price.end_month
    return month >= price.start_month or month <= price.end_month


def _explicit_month_window(period_text: str | None) -> tuple[int, ...] | None:
    if not period_text:
        return None
    found = list(_MONTH_PATTERN.finditer(period_text))
    if len(found) == 1:
        return (_MONTHS[found[0].group(1).casefold()],)
    if len(found) != 2 or not re.search(r"(?:\bto\b|\bthrough\b|\buntil\b|[-–—])", period_text[found[0].end():found[1].start()], re.I):
        return None
    start = _MONTHS[found[0].group(1).casefold()]
    end = _MONTHS[found[1].group(1).casefold()]
    months = [start]
    while months[-1] != end and len(months) < 12:
        months.append(months[-1] % 12 + 1)
    return tuple(months)


def _price_covers_dates(price: PackagePrice, start: date, end: date) -> bool:
    days = (end - start).days
    if days < 0 or days > _MAX_PRICE_MATCH_DAYS:
        return False
    return all(price.applies_on(date.fromordinal(ordinal)) for ordinal in range(start.toordinal(), end.toordinal() + 1))


def _verified_price(price: PackagePrice) -> VerifiedPrice:
    return VerifiedPrice(
        price_id=str(price.id),
        season_name=price.season_name,
        price_per_person=format(price.price_per_person, ".2f"),
        currency=price.currency,
        price_basis=price.price_basis,
        start_month=price.start_month,
        end_month=price.end_month,
        valid_from=price.valid_from,
        valid_until=price.valid_until,
        price_note=price.price_note,
    )


def _eligible_price_rows(package: Package, extraction: EnquiryExtraction) -> tuple[list[PackagePrice], str | None, str]:
    if extraction.travel_start_date and extraction.travel_end_date:
        matching = [
            price for price in package.prices
            if _price_covers_dates(price, extraction.travel_start_date, extraction.travel_end_date)
        ]
        if len(matching) == 1:
            return matching, matching[0].season_name, "matched"
        return [], None, "ambiguous" if len(matching) > 1 else "staff_confirmation"

    month_window = _explicit_month_window(extraction.original_travel_period_text)
    if month_window is not None:
        matching = [
            price for price in package.prices
            if all(_month_matches(price, month) for month in month_window)
            and price.valid_from is None and price.valid_until is None
        ]
        if len(matching) == 1:
            return matching, matching[0].season_name, "matched"
        return [], None, "ambiguous" if len(matching) > 1 else "staff_confirmation"

    return [], None, "staff_confirmation"


def _budget_comparison(extraction: EnquiryExtraction, price: PackagePrice | None) -> str:
    if (
        extraction.budget_amount is None
        or extraction.budget_currency != "INR"
        or extraction.budget_scope != "per_person"
        or price is None
        or price.currency != "INR"
        or price.price_basis != "per_person"
    ):
        return "unknown"
    return "within" if price.price_per_person <= extraction.budget_amount else "over"


def recommend_packages(
    session: Session,
    extraction: EnquiryExtraction,
    *,
    limit: int = MAX_RECOMMENDATIONS,
) -> RecommendationResult:
    """Rank published packages with explainable rules and database price evidence.

    Results are calculated from current PostgreSQL package/price rows on demand;
    no stored quote or price snapshot can become stale. No package is considered
    available for booking, and this function has no provider or mutation path.
    """
    if not isinstance(extraction, EnquiryExtraction):
        raise TypeError("recommend_packages requires a validated EnquiryExtraction.")
    result_limit = max(0, min(int(limit), MAX_RECOMMENDATIONS))
    if not extraction.destination:
        return RecommendationResult(
            recommendations=(),
            explanation=(
                "There is not enough validated destination information to recommend a package safely. "
                "Staff can review the original enquiry manually."
            ),
        )
    packages = session.scalars(
        published_packages_statement().order_by(Package.title.asc(), Package.package_code.asc())
    ).unique().all()
    scored: list[PackageRecommendation] = []
    requested_destination = extraction.destination
    explicit_period = bool(extraction.travel_start_date and extraction.travel_end_date) or bool(
        _explicit_month_window(extraction.original_travel_period_text)
    )

    for package in packages:
        destination_match = _destination_match(requested_destination, package)
        if requested_destination and not destination_match:
            continue
        score = 50 if destination_match else 0
        reasons: list[str] = []
        caveats: list[str] = []
        if destination_match:
            reasons.append("The package names the requested destination in its stored destinations or title.")

        price_rows, season_match, price_status = _eligible_price_rows(package, extraction)
        verified_price_row = price_rows[0] if price_rows else None
        if season_match:
            score += 20 if extraction.travel_start_date else 15
            reasons.append(f"The stored {season_match} price window matches the stated travel period.")
        elif explicit_period:
            if price_status == "ambiguous":
                caveats.append("Multiple stored seasonal price rows match; staff must confirm the applicable rate.")
            else:
                caveats.append("No single stored seasonal price covers the full stated period; staff must confirm pricing.")
        else:
            caveats.append("Travel dates or season are not established, so no price is selected; staff must confirm pricing.")

        requested_duration = None
        if extraction.travel_start_date and extraction.travel_end_date:
            requested_duration = (extraction.travel_end_date - extraction.travel_start_date).days
        if package.duration_days is not None and requested_duration is not None:
            difference = abs(package.duration_days - requested_duration)
            if difference <= 1:
                score += 15
                reasons.append("Stored package duration is close to the requested date span.")
            elif difference <= 3:
                score += 8
                reasons.append("Stored package duration is within three days of the requested date span.")
            else:
                score -= 4
                caveats.append("Stored package duration differs from the requested date span.")

        budget_fit = _budget_comparison(extraction, verified_price_row)
        if budget_fit == "within":
            score += 10
            reasons.append("Verified INR per-person price is within the stated per-person budget.")
        elif budget_fit == "over":
            score -= 8
            caveats.append("Verified per-person price is above the stated per-person budget.")
        elif extraction.budget_amount is not None:
            if extraction.budget_scope == "total":
                caveats.append("The budget is total-trip while the stored rate is per-person; no group total was calculated.")
            if extraction.budget_scope is None:
                caveats.append("Budget scope is unknown; no budget comparison was made.")
            if extraction.budget_currency != "INR":
                caveats.append("Budget comparison supports INR only; no conversion was made.")
            elif extraction.budget_scope == "per_person" and (
                verified_price_row is not None
                and (verified_price_row.currency != "INR" or verified_price_row.price_basis != "per_person")
            ):
                caveats.append("Budget comparison requires compatible INR per-person pricing; no conversion was made.")
            elif extraction.budget_scope == "per_person" and verified_price_row is None:
                caveats.append("No compatible verified price is available for budget comparison.")

        if (extraction.children or 0) > 0 or (extraction.infants or 0) > 0:
            caveats.append("Child/infant fares and capacity are not defined in package pricing; staff must confirm them.")
        if extraction.adults is not None or extraction.children is not None or extraction.infants is not None:
            caveats.append("Traveller counts do not establish availability or a group quote.")
        if package.hotel_category is None:
            caveats.append("Accommodation category is not specified in the package record.")

        if not reasons:
            # Without destination evidence, require a meaningful period or duration signal.
            if not season_match and requested_duration is None:
                continue
            reasons.append("Candidate is ranked from available package timing/duration data; destination is unconfirmed.")
        if score < 15:
            continue
        caveats.append("Confirm GST treatment with staff; this engine does not interpret free-text price notes as tax policy.")
        scored.append(
            PackageRecommendation(
                package_id=str(package.id),
                package_code=package.package_code,
                title=package.title,
                destinations=tuple(value for value in (package.destinations or []) if isinstance(value, str)),
                duration_days=package.duration_days,
                score=score,
                match_reasons=tuple(reasons),
                verified_price=_verified_price(verified_price_row) if verified_price_row else None,
                price_status=price_status,
                season_match=season_match,
                caveats=tuple(dict.fromkeys(caveats)),
            )
        )

    scored.sort(key=lambda item: (-item.score, _normalize_destination(item.title), item.package_code, item.package_id))
    recommendations = tuple(scored[:result_limit])
    if recommendations:
        explanation = (
            "Deterministic match points: exact destination +50; matched seasonal price +20 for exact dates or +15 for an explicit month; "
            "close duration up to +15; compatible INR per-person budget +10, or −8 when over budget; duration differing by more than three days −4. "
            "Traveller counts do not imply capacity or child fares. Scores are ranking points, not confidence percentages."
        )
    elif requested_destination:
        explanation = "No published package sufficiently matches the stated destination and available package data. Staff can review the original enquiry manually."
    else:
        explanation = "There is not enough validated destination or trip-period information to recommend a package. Staff can review the original enquiry manually."
    return RecommendationResult(recommendations=recommendations, explanation=explanation)
