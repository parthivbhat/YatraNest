"""Safe read-only presentation helpers for immutable package snapshots."""

import json
from calendar import month_name
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any


PACKAGE_SNAPSHOT_FIELDS = (
    ("Package code at snapshot", "package_code"),
    ("URL slug at snapshot", "slug"),
    ("Title", "title"),
    ("Summary", "summary"),
    ("Description", "description"),
    ("Destinations", "destinations"),
    ("Duration (days)", "duration_days"),
    ("Accommodation category", "hotel_category"),
    ("Hotel details", "hotel_details"),
    ("Itinerary", "itinerary"),
    ("Inclusions", "inclusions"),
    ("Map URL", "map_url"),
    ("Status at snapshot", "status"),
    ("Scheduled publication at snapshot", "scheduled_publish_at"),
    ("Published at snapshot", "published_at"),
)

COMPARISON_FIELDS = (
    ("Title", "title"),
    ("Summary", "summary"),
    ("Description", "description"),
    ("Destinations", "destinations"),
    ("Duration", "duration_days"),
    ("Accommodation category", "hotel_category"),
    ("Hotel details", "hotel_details"),
    ("Itinerary", "itinerary"),
    ("Inclusions", "inclusions"),
    ("Map URL", "map_url"),
)


def snapshot_fields_for_display(snapshot: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Return supported snapshot fields in safe text form and any warnings."""
    warnings: list[str] = []
    if not isinstance(snapshot, dict):
        return [], ["This historical snapshot is not a valid object."]

    fields = []
    for label, key in PACKAGE_SNAPSHOT_FIELDS:
        if key not in snapshot:
            warnings.append(f"The snapshot does not contain {label.lower()}.")
            value = None
        else:
            value = snapshot[key]
        if value is None or value == "":
            rendered = "Not specified"
        elif isinstance(value, (dict, list)):
            rendered = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        else:
            rendered = str(value)
        fields.append({"label": label, "value": rendered})
    return fields, warnings


def snapshot_prices_for_display(snapshot: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Convert JSON date/decimal price values while tolerating damaged rows."""
    warnings: list[str] = []
    if not isinstance(snapshot, dict):
        return [], ["Seasonal prices cannot be read because the snapshot is invalid."]
    raw_prices = snapshot.get("prices")
    if raw_prices is None:
        return [], ["The snapshot does not contain seasonal prices."]
    if not isinstance(raw_prices, list):
        return [], ["Seasonal prices are stored in an invalid format."]

    prices: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_prices, start=1):
        if not isinstance(raw, dict):
            warnings.append(f"Seasonal price {index} is malformed and could not be fully displayed.")
            prices.append({"raw": json.dumps(raw, ensure_ascii=False, default=str), "valid": False})
            continue
        try:
            start_month = int(raw["start_month"])
            end_month = int(raw["end_month"])
            if not 1 <= start_month <= 12 or not 1 <= end_month <= 12:
                raise ValueError("month out of range")
            amount = Decimal(str(raw["price_per_person"]))
            if not amount.is_finite():
                raise ValueError("price is not finite")
            valid_from = _parse_snapshot_date(raw.get("valid_from"))
            valid_until = _parse_snapshot_date(raw.get("valid_until"))
            if valid_from and valid_until and valid_from > valid_until:
                raise ValueError("date range is reversed")
        except (KeyError, TypeError, ValueError, InvalidOperation):
            warnings.append(f"Seasonal price {index} contains invalid data; its raw values are shown.")
            prices.append({"raw": json.dumps(raw, ensure_ascii=False, indent=2, default=str), "valid": False})
            continue

        prices.append(
            {
                "season_name": str(raw.get("season_name") or "Unnamed season"),
                "start_month": start_month,
                "end_month": end_month,
                "season_label": f"{month_name[start_month]}–{month_name[end_month]}",
                "valid_from": valid_from,
                "valid_until": valid_until,
                "price_per_person": amount,
                "currency": str(raw.get("currency") or "INR"),
                "price_basis": str(raw.get("price_basis") or "per_person"),
                "price_note": raw.get("price_note"),
                "valid": True,
            }
        )
    return prices, warnings


def compare_snapshot_fields(left_snapshot: Any, right_snapshot: Any) -> tuple[list[dict[str, str]], list[str]]:
    """Build escaped-text-friendly field rows with explicit change states."""
    left, left_warnings = _as_snapshot_mapping(left_snapshot)
    right, right_warnings = _as_snapshot_mapping(right_snapshot)
    rows = []
    for label, key in COMPARISON_FIELDS:
        left_value = _comparison_value(left, key)
        right_value = _comparison_value(right, key)
        rows.append(
            {
                "label": label,
                "left": _render_comparison_value(left_value),
                "right": _render_comparison_value(right_value),
                "change": _change_kind(left_value, right_value),
            }
        )
    return rows, left_warnings + right_warnings


def compare_snapshot_prices(left_snapshot: Any, right_snapshot: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Pair seasonal rows by season name and occurrence, preserving typed values."""
    left_prices, left_warnings = snapshot_prices_for_display(left_snapshot)
    right_prices, right_warnings = snapshot_prices_for_display(right_snapshot)
    combined: dict[tuple[str, int], dict[str, Any]] = {}

    for side, prices in (("left", left_prices), ("right", right_prices)):
        occurrences: dict[str, int] = {}
        for index, price in enumerate(prices):
            if price.get("valid"):
                group = str(price["season_name"]).casefold()
            else:
                group = f"__malformed_{index}"
            occurrence = occurrences.get(group, 0)
            occurrences[group] = occurrence + 1
            paired = combined.setdefault((group, occurrence), {"left": None, "right": None})
            paired[side] = price

    rows = []
    for paired in combined.values():
        left = paired["left"]
        right = paired["right"]
        if left is None:
            change = "added"
        elif right is None:
            change = "removed"
        elif not left.get("valid") or not right.get("valid"):
            change = "unchanged" if left.get("raw") == right.get("raw") else "changed"
        else:
            change = "unchanged" if _price_comparison_key(left) == _price_comparison_key(right) else "changed"
        rows.append({"left": left, "right": right, "change": change})
    return rows, left_warnings + right_warnings


def _as_snapshot_mapping(snapshot: Any) -> tuple[dict[str, Any], list[str]]:
    if isinstance(snapshot, dict):
        return snapshot, []
    return {}, ["A snapshot is not a valid object; supported fields are shown as not specified."]


def _comparison_value(snapshot: dict[str, Any], key: str) -> Any:
    value = snapshot.get(key)
    if value in (None, "", [], {}):
        return None
    return value


def _render_comparison_value(value: Any) -> str:
    if value is None:
        return "Not specified"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return str(value)


def _change_kind(left: Any, right: Any) -> str:
    if left is None and right is None:
        return "unchanged"
    if left is None:
        return "added"
    if right is None:
        return "removed"
    return "unchanged" if left == right else "changed"


def _price_comparison_key(price: dict[str, Any]) -> tuple[Any, ...]:
    return (
        price["season_name"],
        price["start_month"],
        price["end_month"],
        price["valid_from"],
        price["valid_until"],
        price["price_per_person"],
        price["currency"],
        price["price_basis"],
        price["price_note"],
    )


def _parse_snapshot_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
