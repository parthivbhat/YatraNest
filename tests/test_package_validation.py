from datetime import date
from decimal import Decimal

import pytest
from starlette.datastructures import FormData

from app.services.package_validation import (
    PACKAGE_CODE_MAX_LENGTH,
    PACKAGE_DESCRIPTION_MAX_LENGTH,
    PACKAGE_SLUG_MAX_LENGTH,
    PACKAGE_SUMMARY_MAX_LENGTH,
    PACKAGE_TITLE_MAX_LENGTH,
    PackageValidationError,
    parse_package_payload,
    slugify_title,
    validate_package_code,
    validate_package_slug,
)


def valid_form(**overrides):
    form = {"package_code": "pkg-kl-07", "title": "Munnar, Thekkady & Alleppey"}
    form.update(overrides)
    return form


def expect_field_error(form, field: str, message_fragment: str | None = None) -> PackageValidationError:
    with pytest.raises(PackageValidationError) as caught:
        parse_package_payload(form)
    error = caught.value
    assert field in error.errors
    if message_fragment:
        assert any(message_fragment in message for message in error.errors[field])
    assert error.first_error
    return error


def test_incomplete_draft_without_prices_is_valid() -> None:
    payload = parse_package_payload(valid_form())

    assert payload.package_code == "PKG-KL-07"
    assert payload.slug == "munnar-thekkady-alleppey"
    assert payload.title == "Munnar, Thekkady & Alleppey"
    assert payload.prices == ()
    assert payload.destinations is None
    assert "status" not in payload.package_values()
    assert payload.to_json()["prices"] == []


def test_supported_content_fields_and_nested_form_rows_normalize() -> None:
    payload = parse_package_payload(
        valid_form(
            summary="  Short summary ",
            description="A description supplied by staff.",
            destinations="Munnar\nThekkady\nAlleppey",
            duration_days="6",
            hotel_category="4-star",
            hotel_name=["Hotel selected by staff", ""],
            hotel_location=["Munnar", ""],
            itinerary_day=["1", "2"],
            itinerary_title=["Arrival", "Journey onward"],
            itinerary_description=["Staff supplied detail", ""],
            inclusions="Breakfast\nHouseboat stay",
            map_url="https://maps.example.test/route",
        )
    )

    assert payload.summary == "Short summary"
    assert payload.destinations == ["Munnar", "Thekkady", "Alleppey"]
    assert payload.duration_days == 6
    assert payload.hotel_details == [{"name": "Hotel selected by staff", "location": "Munnar"}]
    assert payload.itinerary == [
        {"day": 1, "title": "Arrival", "description": "Staff supplied detail"},
        {"day": 2, "title": "Journey onward"},
    ]
    assert payload.inclusions == ["Breakfast", "Houseboat stay"]
    assert payload.map_url == "https://maps.example.test/route"


def test_formdata_repeated_controls_are_parsed_and_status_is_never_accepted() -> None:
    form = FormData(
        [
            ("package_code", "PKG-1"),
            ("title", "A Journey"),
            ("status", "published"),
            ("price_season_name", "Peak"),
            ("price_start_month", "10"),
            ("price_end_month", "3"),
            ("price_per_person", "32500.00"),
        ]
    )
    payload = parse_package_payload(form)

    assert payload.prices[0].start_month == 10
    assert payload.prices[0].end_month == 3
    assert "status" not in payload.to_json()


def test_existing_identity_is_preserved_while_title_changes() -> None:
    payload = parse_package_payload(
        {"package_code": "TAMPERED", "slug": "tampered", "title": "New package name"},
        existing_package_code="PKG-LOCKED",
        existing_slug="existing-public-url",
    )

    assert payload.package_code == "PKG-LOCKED"
    assert payload.slug == "existing-public-url"
    assert payload.title == "New package name"


@pytest.mark.parametrize("field", ["package_code", "title"])
def test_required_identity_fields_are_required(field: str) -> None:
    form = valid_form()
    form[field] = "   "
    expected_message = "package code" if field == "package_code" else "required"
    expect_field_error(form, field, expected_message)


def test_package_code_normalizes_and_validates_syntax_and_length() -> None:
    assert validate_package_code("  pkg-kl-07 ") == "PKG-KL-07"
    with pytest.raises(ValueError, match="single hyphens"):
        validate_package_code("PKG--KL")
    with pytest.raises(ValueError, match="characters or fewer"):
        validate_package_code("A" * (PACKAGE_CODE_MAX_LENGTH + 1))


def test_slug_helpers_generate_lowercase_stable_shape_and_reject_invalid_values() -> None:
    assert slugify_title("  Munnar, Thekkady & Alleppey  ") == "munnar-thekkady-alleppey"
    assert validate_package_slug("munnar-thekkady-alleppey") == "munnar-thekkady-alleppey"
    with pytest.raises(ValueError, match="could not be generated"):
        validate_package_slug(slugify_title("കേരളം"))
    with pytest.raises(ValueError, match="single hyphens"):
        validate_package_slug("Bad--Slug")
    assert len(slugify_title("a " * PACKAGE_SLUG_MAX_LENGTH)) <= PACKAGE_SLUG_MAX_LENGTH


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("title", "T" * (PACKAGE_TITLE_MAX_LENGTH + 1), "characters or fewer"),
        ("summary", "S" * (PACKAGE_SUMMARY_MAX_LENGTH + 1), "characters or fewer"),
        ("description", "D" * (PACKAGE_DESCRIPTION_MAX_LENGTH + 1), "characters or fewer"),
        ("duration_days", "0", "greater than zero"),
        ("duration_days", "six", "whole number"),
        ("duration_days", "2147483648", "supported maximum"),
        ("hotel_category", "H" * 81, "characters or fewer"),
        ("map_url", "javascript:alert(1)", "http or https"),
        ("map_url", "https://[", "http or https"),
    ],
)
def test_package_fields_validate_lengths_and_supported_values(field: str, value: str, message: str) -> None:
    expect_field_error(valid_form(**{field: value}), field, message)


def test_existing_code_and_slug_must_be_passed_together() -> None:
    with pytest.raises(ValueError, match="provided together"):
        parse_package_payload(valid_form(), existing_package_code="PKG-1")


def price_row(**overrides):
    row = {
        "season_name": "Peak",
        "start_month": "10",
        "end_month": "3",
        "price_per_person": "32500.00",
        "currency": "INR",
        "price_basis": "per_person",
    }
    row.update(overrides)
    return row


def test_valid_cross_year_price_normalizes_to_decimal_and_json_safe_values() -> None:
    payload = parse_package_payload(valid_form(prices=[price_row(valid_from="2026-10-01", valid_until="2027-03-31")]))
    price = payload.prices[0]

    assert price.start_month == 10 and price.end_month == 3
    assert price.price_per_person == Decimal("32500.00")
    assert price.valid_from == date(2026, 10, 1)
    assert price.to_json()["price_per_person"] == "32500.00"
    assert price.to_json()["valid_until"] == "2027-03-31"


def test_blank_optional_price_rows_are_ignored_but_partial_rows_are_reported() -> None:
    payload = parse_package_payload(
        valid_form(price_season_name=["", "Monsoon"], price_start_month=["", "6"], price_end_month=["", "9"], price_per_person=["", "24900"])
    )
    assert len(payload.prices) == 1

    error = expect_field_error(
        valid_form(price_season_name=[""], price_start_month=["6"], price_end_month=[""], price_per_person=[""]),
        "prices.0.season_name",
    )
    assert error.values["price_start_month"] == ["6"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("price_per_person", "0", "greater than zero"),
        ("price_per_person", "-10", "greater than zero"),
        ("price_per_person", "32500.001", "two decimal places"),
        ("price_per_person", "10000000000.00", "maximum"),
        ("price_per_person", "NaN", "finite"),
        ("price_per_person", "1e-10000", "two decimal places"),
        ("price_per_person", "1" * 65, "too long"),
        ("start_month", "0", "between 1 and 12"),
        ("start_month", "13", "between 1 and 12"),
        ("start_month", "June", "whole month"),
        ("currency", "USD", "INR"),
        ("price_basis", "per_room", "per-person"),
        ("season_name", "S" * 101, "100 characters"),
    ],
)
def test_price_amount_basis_and_month_values_are_validated(field: str, value: str, message: str) -> None:
    error_field = {"start_month": "start_month"}.get(field, field)
    error = expect_field_error(valid_form(prices=[price_row(**{field: value})]), f"prices.0.{error_field}", message)


@pytest.mark.parametrize(
    ("from_value", "until_value", "expected_field", "message"),
    [
        ("2026-10-01", "", "prices.0.valid_from", "both date bounds"),
        ("2026-13-01", "2027-03-31", "prices.0.valid_from", "valid date"),
        ("2027-04-01", "2027-03-31", "prices.0.valid_until", "on or after"),
    ],
)
def test_absolute_date_bounds_are_validated(
    from_value: str, until_value: str, expected_field: str, message: str
) -> None:
    expect_field_error(
        valid_form(prices=[price_row(valid_from=from_value, valid_until=until_value)]),
        expected_field,
        message,
    )


def test_duplicate_season_key_is_rejected_even_if_date_windows_differ() -> None:
    first = price_row(valid_from="2026-10-01", valid_until="2027-03-31")
    second = price_row(valid_from="2027-10-01", valid_until="2028-03-31")
    expect_field_error(valid_form(prices=[first, second]), "prices.1.season_name", "already exists")


def test_genuinely_overlapping_cross_year_effective_windows_are_rejected() -> None:
    peak = price_row(season_name="Peak", start_month="10", end_month="3", valid_from="2026-10-01", valid_until="2027-03-31")
    special = price_row(season_name="Special", start_month="1", end_month="6", valid_from="2027-01-01", valid_until="2027-06-30")
    expect_field_error(valid_form(prices=[peak, special]), "prices.1.start_month", "overlaps")


def test_recurring_month_overlap_is_allowed_when_absolute_windows_are_disjoint() -> None:
    autumn_to_spring = price_row(
        season_name="Season A",
        start_month="10",
        end_month="3",
        valid_from="2026-10-01",
        valid_until="2027-03-31",
    )
    spring_to_summer = price_row(
        season_name="Season B",
        start_month="1",
        end_month="6",
        valid_from="2027-04-01",
        valid_until="2027-06-30",
    )

    payload = parse_package_payload(valid_form(prices=[autumn_to_spring, spring_to_summer]))
    assert len(payload.prices) == 2


def test_seasons_with_disjoint_recurring_months_are_allowed() -> None:
    payload = parse_package_payload(
        valid_form(
            prices=[
                price_row(season_name="Peak", start_month="10", end_month="3"),
                price_row(season_name="Monsoon", start_month="6", end_month="9"),
            ]
        )
    )
    assert {price.season_name for price in payload.prices} == {"Peak", "Monsoon"}
