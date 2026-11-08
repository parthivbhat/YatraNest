"""Canonical package form parsing and validation without database side effects.

Repeated form controls use these names: ``price_season_name``,
``price_start_month``, ``price_end_month``, ``price_per_person``,
``price_valid_from``, ``price_valid_until``, ``price_note``, ``price_currency``,
and ``price_basis``. Hotel and itinerary controls follow the same repeated-row
pattern. Destinations and inclusions may be multiline strings or lists.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit


PACKAGE_CODE_MAX_LENGTH = 32
PACKAGE_SLUG_MAX_LENGTH = 180
PACKAGE_TITLE_MAX_LENGTH = 240
PACKAGE_SUMMARY_MAX_LENGTH = 500
PACKAGE_HOTEL_CATEGORY_MAX_LENGTH = 80
PACKAGE_MAP_URL_MAX_LENGTH = 500
PACKAGE_DESCRIPTION_MAX_LENGTH = 20_000
PACKAGE_PRICE_NOTE_MAX_LENGTH = 2_000
MAX_REPEATED_FORM_ROWS = 100
PRICE_MAXIMUM = Decimal("9999999999.99")  # Numeric(12, 2)

_PACKAGE_CODE_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_PACKAGE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SLUG_SEPARATOR_RE = re.compile(r"[^a-z0-9]+")


class PackageValidationError(ValueError):
    """Aggregate field errors and cleaned values for safe form re-rendering."""

    def __init__(self, errors: dict[str, list[str]], values: dict[str, Any] | None = None) -> None:
        self.errors = errors
        self.values = values or {}
        super().__init__("Package details need attention.")

    @property
    def first_error(self) -> str:
        for field_errors in self.errors.values():
            if field_errors:
                return field_errors[0]
        return "Package details are invalid."


@dataclass(frozen=True)
class SeasonalPricePayload:
    season_name: str
    start_month: int
    end_month: int
    price_per_person: Decimal
    valid_from: date | None = None
    valid_until: date | None = None
    currency: str = "INR"
    price_basis: str = "per_person"
    price_note: str | None = None

    def to_json(self) -> dict[str, Any]:
        """Return JSON-safe values for a future PackageDraft snapshot."""
        return {
            "season_name": self.season_name,
            "start_month": self.start_month,
            "end_month": self.end_month,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "price_per_person": format(self.price_per_person, ".2f"),
            "currency": self.currency,
            "price_basis": self.price_basis,
            "price_note": self.price_note,
        }


@dataclass(frozen=True)
class PackagePayload:
    """Validated, normalized content for the supported Package model fields."""

    package_code: str
    slug: str
    title: str
    summary: str | None
    description: str | None
    destinations: list[str] | None
    duration_days: int | None
    hotel_category: str | None
    hotel_details: list[dict[str, str]] | None
    itinerary: list[dict[str, Any]] | None
    inclusions: list[str] | None
    map_url: str | None
    prices: tuple[SeasonalPricePayload, ...]

    def package_values(self) -> dict[str, Any]:
        """Return fields that can be assigned to a Package row."""
        return {
            "package_code": self.package_code,
            "slug": self.slug,
            "title": self.title,
            "summary": self.summary,
            "description": self.description,
            "destinations": self.destinations,
            "duration_days": self.duration_days,
            "hotel_category": self.hotel_category,
            "hotel_details": self.hotel_details,
            "itinerary": self.itinerary,
            "inclusions": self.inclusions,
            "map_url": self.map_url,
        }

    def to_json(self) -> dict[str, Any]:
        """Serialize complete supported package content and prices for JSONB."""
        return {
            **self.package_values(),
            "prices": [price.to_json() for price in self.prices],
        }


def validate_package_code(value: Any) -> str:
    """Normalize and validate the staff-entered package code syntax."""
    code = _text(value).upper()
    if not code:
        raise ValueError("Enter a package code.")
    if len(code) > PACKAGE_CODE_MAX_LENGTH:
        raise ValueError(f"Package code must be {PACKAGE_CODE_MAX_LENGTH} characters or fewer.")
    if not _PACKAGE_CODE_RE.fullmatch(code):
        raise ValueError("Use only letters, numbers, and single hyphens in the package code.")
    return code


def slugify_title(title: str) -> str:
    """Generate a stable lowercase URL slug from a package title."""
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_SEPARATOR_RE.sub("-", ascii_title.lower()).strip("-")
    return slug[:PACKAGE_SLUG_MAX_LENGTH].rstrip("-")


def validate_package_slug(value: Any) -> str:
    """Validate a generated or persisted canonical package slug."""
    slug = _text(value).lower()
    if not slug:
        raise ValueError("A URL slug could not be generated from this title.")
    if len(slug) > PACKAGE_SLUG_MAX_LENGTH:
        raise ValueError(f"Slug must be {PACKAGE_SLUG_MAX_LENGTH} characters or fewer.")
    if not _PACKAGE_SLUG_RE.fullmatch(slug):
        raise ValueError("Slug must contain lowercase letters or numbers separated by single hyphens.")
    return slug


def parse_package_payload(
    form: Mapping[str, Any],
    *,
    existing_package_code: str | None = None,
    existing_slug: str | None = None,
) -> PackagePayload:
    """Parse a form-like mapping into canonical package content.

    The package code is mandatory on creation. When editing, both immutable
    identity fields can be supplied by the caller and are preserved instead of
    trusting submitted form values. Status and publication fields are never
    accepted from the form.
    """
    errors: dict[str, list[str]] = {}
    values = _capture_form_values(form)

    if (existing_package_code is None) != (existing_slug is None):
        raise ValueError("Existing package code and slug must be provided together.")

    title = _required_text(form.get("title"), "title", errors, maximum=PACKAGE_TITLE_MAX_LENGTH)
    if existing_package_code is None:
        try:
            package_code = validate_package_code(form.get("package_code"))
        except ValueError as exc:
            _add_error(errors, "package_code", str(exc))
            package_code = ""
    else:
        package_code = validate_package_code(existing_package_code)

    try:
        slug = validate_package_slug(existing_slug if existing_slug is not None else slugify_title(title))
    except ValueError as exc:
        _add_error(errors, "title", str(exc))
        slug = ""

    summary = _optional_text(form.get("summary"), "summary", errors, maximum=PACKAGE_SUMMARY_MAX_LENGTH)
    description = _optional_text(
        form.get("description"), "description", errors, maximum=PACKAGE_DESCRIPTION_MAX_LENGTH
    )
    hotel_category = _optional_text(
        form.get("hotel_category"),
        "hotel_category",
        errors,
        maximum=PACKAGE_HOTEL_CATEGORY_MAX_LENGTH,
    )
    duration_days = _parse_positive_integer(form.get("duration_days"), "duration_days", errors)
    destinations = _parse_text_list(form, "destinations", errors)
    inclusions = _parse_text_list(form, "inclusions", errors)
    hotel_details = _parse_hotels(form, errors)
    itinerary = _parse_itinerary(form, errors, duration_days=duration_days)
    map_url = _parse_map_url(form.get("map_url"), errors)
    prices = _parse_prices(form, errors)
    _validate_price_windows(prices, errors)

    values["_normalized"] = {
        "package_code": package_code,
        "slug": slug,
        "title": title,
        "summary": summary,
        "description": description,
        "destinations": destinations,
        "duration_days": duration_days,
        "hotel_category": hotel_category,
        "hotel_details": hotel_details,
        "itinerary": itinerary,
        "inclusions": inclusions,
        "map_url": map_url,
        "prices": [price.to_json() for price in prices],
    }
    if errors:
        raise PackageValidationError(errors, values)

    return PackagePayload(
        package_code=package_code,
        slug=slug,
        title=title,
        summary=summary,
        description=description,
        destinations=destinations,
        duration_days=duration_days,
        hotel_category=hotel_category,
        hotel_details=hotel_details,
        itinerary=itinerary,
        inclusions=inclusions,
        map_url=map_url,
        prices=tuple(prices),
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def _capture_form_values(form: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only supported user fields so invalid input can be re-rendered safely."""
    field_names = (
        "package_code",
        "title",
        "summary",
        "description",
        "destinations",
        "duration_days",
        "hotel_category",
        "map_url",
        "inclusions",
        "hotel_details",
        "hotel_name",
        "hotel_location",
        "itinerary",
        "itinerary_day",
        "itinerary_title",
        "itinerary_description",
        "prices",
        "price_season_name",
        "price_start_month",
        "price_end_month",
        "price_per_person",
        "price_valid_from",
        "price_valid_until",
        "price_note",
        "price_currency",
        "price_basis",
    )
    captured: dict[str, Any] = {}
    getlist = getattr(form, "getlist", None)
    for name in field_names:
        if callable(getlist):
            items = list(getlist(name))
            if items:
                captured[name] = items if len(items) > 1 else items[0]
        elif name in form:
            captured[name] = form[name]
    return captured


def _add_error(errors: dict[str, list[str]], field: str, message: str) -> None:
    errors.setdefault(field, []).append(message)


def _required_text(value: Any, field: str, errors: dict[str, list[str]], *, maximum: int) -> str:
    normalized = _text(value)
    if not normalized:
        _add_error(errors, field, "This field is required.")
    elif len(normalized) > maximum:
        _add_error(errors, field, f"Must be {maximum} characters or fewer.")
    return normalized


def _optional_text(
    value: Any,
    field: str,
    errors: dict[str, list[str]],
    *,
    maximum: int,
) -> str | None:
    normalized = _text(value)
    if not normalized:
        return None
    if len(normalized) > maximum:
        _add_error(errors, field, f"Must be {maximum} characters or fewer.")
    return normalized


def _list_values(form: Mapping[str, Any], key: str) -> list[Any]:
    getlist = getattr(form, "getlist", None)
    if callable(getlist):
        values = list(getlist(key))
        if len(values) > MAX_REPEATED_FORM_ROWS:
            return values[: MAX_REPEATED_FORM_ROWS + 1]
        return values
    value = form.get(key)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value[: MAX_REPEATED_FORM_ROWS + 1])
    if isinstance(value, str):
        return value.splitlines()
    return [value]


def _check_row_limit(rows: list[Any], field: str, errors: dict[str, list[str]]) -> None:
    if len(rows) > MAX_REPEATED_FORM_ROWS:
        _add_error(errors, field, f"Use no more than {MAX_REPEATED_FORM_ROWS} rows.")


def _parse_text_list(form: Mapping[str, Any], field: str, errors: dict[str, list[str]]) -> list[str] | None:
    values = _list_values(form, field)
    _check_row_limit(values, field, errors)
    cleaned = [_text(value) for value in values if _text(value)]
    return cleaned or None


def _parse_positive_integer(value: Any, field: str, errors: dict[str, list[str]]) -> int | None:
    normalized = _text(value)
    if not normalized:
        return None
    try:
        parsed = int(normalized)
    except ValueError:
        _add_error(errors, field, "Enter a whole number greater than zero.")
        return None
    if parsed <= 0:
        _add_error(errors, field, "Enter a whole number greater than zero.")
        return None
    if parsed > 2_147_483_647:
        _add_error(errors, field, "Duration exceeds the supported maximum.")
        return None
    return parsed


def _indexed_rows(
    form: Mapping[str, Any],
    *,
    field: str,
    direct_key: str,
    repeated_fields: tuple[str, ...],
    errors: dict[str, list[str]],
) -> list[dict[str, Any]]:
    direct = form.get(direct_key)
    if direct is not None:
        if not _text(direct):
            return []
        if isinstance(direct, list) and all(isinstance(row, Mapping) for row in direct):
            rows = [dict(row) for row in direct]
            _check_row_limit(rows, field, errors)
            return rows[:MAX_REPEATED_FORM_ROWS]
        _add_error(errors, field, "Submit these details as structured rows.")
        return []

    columns = {name: _list_values(form, name) for name in repeated_fields}
    row_count = max((len(items) for items in columns.values()), default=0)
    if row_count > MAX_REPEATED_FORM_ROWS:
        _add_error(errors, field, f"Use no more than {MAX_REPEATED_FORM_ROWS} rows.")
        row_count = MAX_REPEATED_FORM_ROWS
    return [
        {name: values[index] if index < len(values) else "" for name, values in columns.items()}
        for index in range(row_count)
    ]


def _parse_hotels(form: Mapping[str, Any], errors: dict[str, list[str]]) -> list[dict[str, str]] | None:
    rows = _indexed_rows(
        form,
        field="hotel_details",
        direct_key="hotel_details",
        repeated_fields=("hotel_name", "hotel_location"),
        errors=errors,
    )
    hotels: list[dict[str, str]] = []
    for index, row in enumerate(rows):
        name = _text(row.get("name", row.get("hotel_name")))
        location = _text(row.get("location", row.get("hotel_location")))
        if not name and not location:
            continue
        if not name:
            _add_error(errors, f"hotel_details.{index}.name", "Enter a hotel name or remove this row.")
            continue
        if len(name) > 240:
            _add_error(errors, f"hotel_details.{index}.name", "Hotel name must be 240 characters or fewer.")
        if len(location) > 240:
            _add_error(errors, f"hotel_details.{index}.location", "Location must be 240 characters or fewer.")
        hotels.append({"name": name, "location": location} if location else {"name": name})
    return hotels or None


def _parse_itinerary(
    form: Mapping[str, Any],
    errors: dict[str, list[str]],
    *,
    duration_days: int | None,
) -> list[dict[str, Any]] | None:
    rows = _indexed_rows(
        form,
        field="itinerary",
        direct_key="itinerary",
        repeated_fields=("itinerary_day", "itinerary_title", "itinerary_description"),
        errors=errors,
    )
    itinerary: list[dict[str, Any]] = []
    seen_days: set[int] = set()
    for index, row in enumerate(rows):
        day_value = _text(row.get("day", row.get("itinerary_day")))
        title = _text(row.get("title", row.get("itinerary_title")))
        description = _text(row.get("description", row.get("itinerary_description")))
        if not day_value and not title and not description:
            continue
        try:
            day_number = int(day_value)
        except ValueError:
            _add_error(errors, f"itinerary.{index}.day", "Enter a whole day number greater than zero.")
            continue
        if day_number <= 0:
            _add_error(errors, f"itinerary.{index}.day", "Enter a whole day number greater than zero.")
        if day_number in seen_days:
            _add_error(errors, f"itinerary.{index}.day", "Each itinerary day must have a unique number.")
        seen_days.add(day_number)
        if duration_days is not None and day_number > duration_days:
            _add_error(errors, f"itinerary.{index}.day", "Day number cannot exceed the package duration.")
        if not title:
            _add_error(errors, f"itinerary.{index}.title", "Enter a title or remove this row.")
        elif len(title) > 240:
            _add_error(errors, f"itinerary.{index}.title", "Title must be 240 characters or fewer.")
        if len(description) > 5_000:
            _add_error(errors, f"itinerary.{index}.description", "Description must be 5,000 characters or fewer.")
        entry: dict[str, Any] = {"day": day_number, "title": title}
        if description:
            entry["description"] = description
        itinerary.append(entry)
    return itinerary or None


def _parse_map_url(value: Any, errors: dict[str, list[str]]) -> str | None:
    url = _text(value)
    if not url:
        return None
    if len(url) > PACKAGE_MAP_URL_MAX_LENGTH:
        _add_error(errors, "map_url", f"Map URL must be {PACKAGE_MAP_URL_MAX_LENGTH} characters or fewer.")
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        _add_error(errors, "map_url", "Enter a complete http or https map URL.")
        return url
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _add_error(errors, "map_url", "Enter a complete http or https map URL.")
    return url


def _parse_month(value: Any, field: str, errors: dict[str, list[str]]) -> int | None:
    normalized = _text(value)
    if not normalized:
        _add_error(errors, field, "Choose a month from 1 to 12.")
        return None
    try:
        month = int(normalized)
    except ValueError:
        _add_error(errors, field, "Choose a whole month number from 1 to 12.")
        return None
    if not 1 <= month <= 12:
        _add_error(errors, field, "Month must be between 1 and 12.")
        return None
    return month


def _parse_date(value: Any, field: str, errors: dict[str, list[str]]) -> date | None:
    normalized = _text(value)
    if not normalized:
        return None
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        _add_error(errors, field, "Enter a valid date in YYYY-MM-DD format.")
        return None


def _parse_price_amount(value: Any, field: str, errors: dict[str, list[str]]) -> Decimal | None:
    normalized = _text(value)
    if not normalized:
        _add_error(errors, field, "Enter a price greater than zero.")
        return None
    if len(normalized) > 64:
        _add_error(errors, field, "Price input is too long.")
        return None
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        _add_error(errors, field, "Enter a valid amount with no more than two decimal places.")
        return None
    if not amount.is_finite():
        _add_error(errors, field, "Enter a finite price amount.")
        return None
    if amount <= 0:
        _add_error(errors, field, "Price must be greater than zero.")
    if amount > PRICE_MAXIMUM:
        _add_error(errors, field, "Price exceeds the supported maximum amount.")
    if 0 < amount <= PRICE_MAXIMUM:
        try:
            exceeds_precision = amount.quantize(Decimal("0.01")) != amount
        except InvalidOperation:
            exceeds_precision = True
        if exceeds_precision:
            _add_error(errors, field, "Price can have no more than two decimal places.")
    return amount


def _parse_prices(form: Mapping[str, Any], errors: dict[str, list[str]]) -> list[SeasonalPricePayload]:
    direct = form.get("prices")
    if direct is not None:
        if not _text(direct):
            rows = []
        elif isinstance(direct, list) and all(isinstance(row, Mapping) for row in direct):
            rows = [dict(row) for row in direct]
        else:
            _add_error(errors, "prices", "Submit prices as structured rows.")
            rows = []
    else:
        fields = (
            "price_season_name",
            "price_start_month",
            "price_end_month",
            "price_per_person",
            "price_valid_from",
            "price_valid_until",
            "price_note",
            "price_currency",
            "price_basis",
        )
        columns = {field: _list_values(form, field) for field in fields}
        row_count = max((len(items) for items in columns.values()), default=0)
        if row_count > MAX_REPEATED_FORM_ROWS:
            _add_error(errors, "prices", f"Use no more than {MAX_REPEATED_FORM_ROWS} price rows.")
            row_count = MAX_REPEATED_FORM_ROWS
        rows = [
            {field: values[index] if index < len(values) else "" for field, values in columns.items()}
            for index in range(row_count)
        ]

    _check_row_limit(rows, "prices", errors)
    prices: list[SeasonalPricePayload] = []
    for index, row in enumerate(rows[:MAX_REPEATED_FORM_ROWS]):
        field_prefix = f"prices.{index}"
        season_name = _text(row.get("season_name", row.get("price_season_name")))
        start_value = row.get("start_month", row.get("price_start_month"))
        end_value = row.get("end_month", row.get("price_end_month"))
        amount_value = row.get("price_per_person")
        from_value = row.get("valid_from", row.get("price_valid_from"))
        until_value = row.get("valid_until", row.get("price_valid_until"))
        note_value = row.get("price_note")
        currency_value = row.get("currency", row.get("price_currency"))
        basis_value = row.get("price_basis")
        currency = _text(currency_value or "INR").upper()
        basis = _text(basis_value or "per_person").lower().replace(" ", "_")
        row_values = (season_name, _text(start_value), _text(end_value), _text(amount_value), _text(from_value), _text(until_value), _text(note_value))
        if not any(row_values):
            continue  # A blank optional row is not a price and does not invalidate an incomplete draft.

        if not season_name:
            _add_error(errors, f"{field_prefix}.season_name", "Enter a season name.")
        elif len(season_name) > 100:
            _add_error(errors, f"{field_prefix}.season_name", "Season name must be 100 characters or fewer.")
        start_month = _parse_month(start_value, f"{field_prefix}.start_month", errors)
        end_month = _parse_month(end_value, f"{field_prefix}.end_month", errors)
        amount = _parse_price_amount(amount_value, f"{field_prefix}.price_per_person", errors)
        valid_from = _parse_date(from_value, f"{field_prefix}.valid_from", errors)
        valid_until = _parse_date(until_value, f"{field_prefix}.valid_until", errors)
        if bool(_text(from_value)) != bool(_text(until_value)):
            _add_error(errors, f"{field_prefix}.valid_from", "Enter both date bounds or leave both blank.")
        if valid_from is not None and valid_until is not None and valid_from > valid_until:
            _add_error(errors, f"{field_prefix}.valid_until", "End date must be on or after the start date.")

        if currency != "INR":
            _add_error(errors, f"{field_prefix}.currency", "Package prices must use INR.")
        if basis != "per_person":
            _add_error(errors, f"{field_prefix}.price_basis", "Package prices must use the per-person basis.")
        price_note = _optional_text(
            note_value,
            f"{field_prefix}.price_note",
            errors,
            maximum=PACKAGE_PRICE_NOTE_MAX_LENGTH,
        )
        if season_name and start_month and end_month and amount is not None and amount > 0:
            prices.append(
                SeasonalPricePayload(
                    season_name=season_name,
                    start_month=start_month,
                    end_month=end_month,
                    price_per_person=amount,
                    valid_from=valid_from,
                    valid_until=valid_until,
                    currency="INR",
                    price_basis="per_person",
                    price_note=price_note,
                )
            )
    return prices


def _month_set(start_month: int, end_month: int) -> set[int]:
    if start_month <= end_month:
        return set(range(start_month, end_month + 1))
    return set(range(start_month, 13)) | set(range(1, end_month + 1))


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)


def _effective_windows_overlap(first: SeasonalPricePayload, second: SeasonalPricePayload) -> bool:
    shared_months = _month_set(first.start_month, first.end_month) & _month_set(second.start_month, second.end_month)
    if not shared_months:
        return False

    lower_bounds = [bound for bound in (first.valid_from, second.valid_from) if bound is not None]
    upper_bounds = [bound for bound in (first.valid_until, second.valid_until) if bound is not None]
    lower = max(lower_bounds) if lower_bounds else None
    upper = min(upper_bounds) if upper_bounds else None
    if lower is not None and upper is not None and lower > upper:
        return False
    if lower is None or upper is None:
        # With an unbounded side, every recurring month eventually occurs in the intersection.
        return True

    # Within at most twelve month starts, every month has been considered.
    candidate = date(lower.year, lower.month, 1)
    for _ in range(12):
        if candidate > upper:
            return False
        if candidate.month in shared_months:
            return True
        candidate = _next_month(candidate)
    return False


def _validate_price_windows(prices: list[SeasonalPricePayload], errors: dict[str, list[str]]) -> None:
    seen_windows: set[tuple[str, int, int]] = set()
    for index, price in enumerate(prices):
        exact_window = (price.season_name.casefold(), price.start_month, price.end_month)
        if exact_window in seen_windows:
            _add_error(
                errors,
                f"prices.{index}.season_name",
                "A season with this name and month window already exists.",
            )
        seen_windows.add(exact_window)

        for other_index in range(index):
            if _effective_windows_overlap(price, prices[other_index]):
                _add_error(
                    errors,
                    f"prices.{index}.start_month",
                    f"This season overlaps the effective dates for price row {other_index + 1}.",
                )
