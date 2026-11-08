from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import engine
from app.models import AISuggestion, Enquiry, EnquiryNote, Package, PackagePrice, PackageVersion, User
from app.seed_data import seed_packages
from app.services.package_versions import append_package_version


@pytest.fixture
def db_session():
    """Use a PostgreSQL transaction that is rolled back after each test."""
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer_transaction.rollback()


def make_package(code: str = "TEST-001", **kwargs) -> Package:
    attributes = {
        "package_code": code,
        "slug": f"{code.lower()}-{uuid4().hex[:8]}",
        "title": "Test package",
        "duration_days": 4,
        "status": "draft",
    }
    attributes.update(kwargs)
    return Package(**attributes)


def test_model_relationships(db_session: Session) -> None:
    staff = User(name="Test Consultant", email=f"{uuid4()}@example.test", role="consultant")
    package = make_package()
    package.prices.append(
        PackagePrice(
            season_name="Peak",
            start_month=10,
            end_month=3,
            price_per_person=Decimal("32500.00"),
        )
    )
    enquiry = Enquiry(
        package=package,
        assigned_to=staff,
        original_message="Please share details for the package.",
    )
    note = EnquiryNote(enquiry=enquiry, author=staff, note="Follow up requested.")
    suggestion = AISuggestion(
        enquiry=enquiry,
        extracted_data={"destination": "Kerala"},
        package_suggestions=[{"package_code": package.package_code}],
        reviewed_by=staff,
    )
    db_session.add_all([staff, package, enquiry, note, suggestion])
    db_session.flush()

    assert package.prices[0].package is package
    assert package.versions == []
    assert enquiry.package is package
    assert enquiry.assigned_to is staff
    assert enquiry.notes == [note]
    assert enquiry.ai_suggestions == [suggestion]
    assert note.author is staff
    assert suggestion.reviewed_by is staff


def test_seasonal_price_supports_year_boundary_and_absolute_dates() -> None:
    price = PackagePrice(
        season_name="Peak",
        start_month=10,
        end_month=3,
        valid_from=date(2026, 10, 1),
        valid_until=date(2027, 3, 31),
        price_per_person=Decimal("32500.00"),
    )

    assert price.applies_on(date(2026, 10, 1))
    assert price.applies_on(date(2027, 1, 15))
    assert price.applies_on(date(2027, 3, 31))
    assert not price.applies_on(date(2026, 9, 30))
    assert not price.applies_on(date(2027, 4, 1))


def test_snapshot_contains_complete_package_and_seasonal_price_state(db_session: Session) -> None:
    package = make_package(
        code=f"SNAP-{uuid4().hex[:8]}",
        title="Snapshot package",
        hotel_category="4-star",
    )
    package.prices.extend(
        [
            PackagePrice(
                season_name="Peak",
                start_month=10,
                end_month=3,
                price_per_person=Decimal("32500.00"),
            ),
            PackagePrice(
                season_name="Monsoon",
                start_month=6,
                end_month=9,
                price_per_person=Decimal("24900.00"),
            ),
        ]
    )
    db_session.add(package)
    db_session.flush()

    first = append_package_version(db_session, package, change_note="Initial")
    assert first.version_number == 1
    assert first.snapshot["title"] == "Snapshot package"
    assert first.snapshot["hotel_category"] == "4-star"
    first_prices = {price["season_name"]: price for price in first.snapshot["prices"]}
    assert set(first_prices) == {"Peak", "Monsoon"}
    assert first_prices["Peak"]["price_per_person"] == "32500.00"
    assert first_prices["Peak"]["start_month"] == 10
    assert first_prices["Peak"]["end_month"] == 3

    package.prices[0].price_per_person = Decimal("34000.00")
    second = append_package_version(db_session, package, change_note="Updated price")
    assert second.version_number == 2
    assert {price["season_name"]: price for price in first.snapshot["prices"]}["Peak"]["price_per_person"] == "32500.00"
    assert {price["season_name"]: price for price in second.snapshot["prices"]}["Peak"]["price_per_person"] == "34000.00"


def test_package_version_rows_are_immutable(db_session: Session) -> None:
    package = make_package(code=f"IMM-{uuid4().hex[:8]}")
    db_session.add(package)
    db_session.flush()
    version = append_package_version(db_session, package)

    version.change_note = "attempted overwrite"
    with pytest.raises(ValueError, match="append a new version"):
        db_session.flush()
    db_session.rollback()


@pytest.mark.parametrize(
    "record",
    [
        lambda: User(name="Bad Role", email=f"{uuid4()}@example.test", role="owner"),
        lambda: make_package(code=f"BAD-{uuid4().hex[:8]}", status="publishing"),
    ],
)
def test_role_and_package_status_constraints(db_session: Session, record) -> None:
    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(record())
            db_session.flush()


def test_price_constraints_reject_negative_amount_and_invalid_month(db_session: Session) -> None:
    package = make_package(code=f"PRICE-{uuid4().hex[:8]}")
    db_session.add(package)
    db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                PackagePrice(
                    package=package,
                    season_name="Negative",
                    start_month=1,
                    end_month=3,
                    price_per_person=Decimal("-1.00"),
                )
            )
            db_session.flush()

    with pytest.raises(IntegrityError):
        with db_session.begin_nested():
            db_session.add(
                PackagePrice(
                    package=package,
                    season_name="Invalid month",
                    start_month=0,
                    end_month=3,
                    price_per_person=Decimal("100.00"),
                )
            )
            db_session.flush()


def test_seed_is_repeatable_and_contains_only_the_two_supplied_packages(db_session: Session) -> None:
    first_run = seed_packages(db_session)
    db_session.flush()
    package_count_after_first = db_session.scalar(select(func.count()).select_from(Package))
    price_count_after_first = db_session.scalar(select(func.count()).select_from(PackagePrice))
    version_count_after_first = db_session.scalar(select(func.count()).select_from(PackageVersion))

    second_run = seed_packages(db_session)
    db_session.flush()

    assert {package.package_code for package in first_run} == {"PKG-KL-07", "PKG-SI-12"}
    assert {package.package_code for package in second_run} == {"PKG-KL-07", "PKG-SI-12"}
    assert db_session.scalar(select(func.count()).select_from(Package)) == package_count_after_first
    assert db_session.scalar(select(func.count()).select_from(PackagePrice)) == price_count_after_first
    assert db_session.scalar(select(func.count()).select_from(PackageVersion)) == version_count_after_first

    kerala = db_session.scalar(select(Package).where(Package.package_code == "PKG-KL-07"))
    south_india = db_session.scalar(select(Package).where(Package.package_code == "PKG-SI-12"))
    assert kerala is not None and kerala.status == "published" and len(kerala.prices) == 2
    assert south_india is not None and south_india.status == "draft" and len(south_india.prices) == 1
    assert kerala.title == "Munnar, Thekkady and Alleppey houseboat"
    assert kerala.duration_days == 6 and kerala.hotel_category == "4-star"
    assert {(price.start_month, price.end_month) for price in kerala.prices} == {(10, 3), (6, 9)}
    assert sorted(price.price_per_person for price in kerala.prices) == [Decimal("24900.00"), Decimal("32500.00")]
    assert south_india.title == "Madurai, Rameswaram and Kanyakumari"
    assert south_india.duration_days == 5 and south_india.hotel_category == "3-star"
    assert south_india.prices[0].price_per_person == Decimal("18750.00")
    assert (south_india.prices[0].start_month, south_india.prices[0].end_month) == (1, 12)
