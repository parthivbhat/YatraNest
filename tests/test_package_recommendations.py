import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from app.models import AISuggestion, Enquiry, Package, PackagePrice, User
from app.schemas.ai_extraction import EnquiryExtraction
from app.services.package_recommendations import recommend_packages


def make_extraction(**changes) -> EnquiryExtraction:
    value = {
        "schema_version": "1.0",
        "destination": "Alleppey",
        "travel_start_date": "2027-10-30",
        "travel_end_date": "2027-11-04",
        "travel_dates_flexible": False,
        "original_travel_period_text": "30 October to 4 November 2027",
        "adults": 2,
        "children": 1,
        "infants": 0,
        "budget_amount": "40000.00",
        "budget_currency": "INR",
        "budget_scope": "per_person",
        "preferences": [],
        "missing_information": [],
        "ambiguous_information": [],
    }
    value.update(changes)
    return EnquiryExtraction.model_validate_json(json.dumps(value), strict=True)


def add_package(
    db: Session,
    *,
    title: str,
    destinations: list[str] | None = None,
    status: str = "published",
    duration_days: int | None = 6,
    price: str | None = "32500.00",
    season_name: str = "Peak",
    start_month: int = 10,
    end_month: int = 3,
    valid_from: date | None = None,
    valid_until: date | None = None,
    currency: str = "INR",
    price_basis: str = "per_person",
    price_note: str | None = None,
) -> Package:
    suffix = uuid4().hex[:8]
    package = Package(
        package_code=f"REC-{suffix.upper()}",
        slug=f"rec-{suffix}",
        title=title,
        destinations=destinations,
        duration_days=duration_days,
        status=status,
    )
    if price is not None:
        package.prices.append(PackagePrice(
            season_name=season_name,
            start_month=start_month,
            end_month=end_month,
            valid_from=valid_from,
            valid_until=valid_until,
            price_per_person=Decimal(price),
            currency=currency,
            price_basis=price_basis,
            price_note=price_note,
        ))
    db.add(package)
    db.flush()
    return package


def test_exact_destination_normalization_and_unrelated_destination_filter(db_session):
    exact = add_package(db_session, title="Alleppey Backwater Holiday", destinations=["  ALLÉPPEY "])
    add_package(db_session, title="Munnar Hills", destinations=["Munnar"])
    result = recommend_packages(db_session, make_extraction(destination="alleppey"))
    assert [item.package_id for item in result.recommendations] == [str(exact.id)]
    assert "requested destination" in result.recommendations[0].match_reasons[0]


def test_normalization_supports_ampersand_and_punctuation_without_generic_overlap(db_session):
    package = add_package(db_session, title="Munnar & Thekkady", destinations=["Munnar and Thekkady"])
    result = recommend_packages(db_session, make_extraction(destination="MUNNAR & THEKKADY"))
    assert [item.package_id for item in result.recommendations] == [str(package.id)]
    no_match = recommend_packages(db_session, make_extraction(destination="Kerala"))
    assert not no_match.recommendations
    assert "No published package" in no_match.explanation


def test_cross_year_season_and_absolute_validity_are_checked_for_every_travel_day(db_session):
    package = add_package(
        db_session,
        title="Alleppey October to March",
        destinations=["Alleppey"],
        valid_from=date(2027, 10, 1),
        valid_until=date(2028, 3, 31),
    )
    good = recommend_packages(db_session, make_extraction())
    assert good.recommendations[0].package_id == str(package.id)
    assert good.recommendations[0].verified_price.season_name == "Peak"
    assert good.recommendations[0].verified_price.valid_until == date(2028, 3, 31)

    april_enquiry = make_extraction(
        travel_start_date="2028-03-30",
        travel_end_date="2028-04-02",
        original_travel_period_text="30 March to 2 April 2028",
    )
    outside = recommend_packages(db_session, april_enquiry)
    assert outside.recommendations[0].verified_price is None
    assert outside.recommendations[0].price_status == "staff_confirmation"


def test_month_only_matching_requires_recurring_unbounded_price_window(db_session):
    recurring = add_package(db_session, title="Alleppey December", destinations=["Alleppey"], start_month=10, end_month=3)
    add_package(
        db_session, title="Alleppey absolute season", destinations=["Alleppey"],
        start_month=10, end_month=3, valid_from=date(2027, 10, 1), valid_until=date(2028, 3, 31),
        season_name="Bounded",
    )
    month_only = make_extraction(
        destination="Alleppey", travel_start_date=None, travel_end_date=None,
        travel_dates_flexible=None, original_travel_period_text="December",
        missing_information=["travel_start_date", "travel_end_date"],
        ambiguous_information=["travel_year"],
    )
    result = recommend_packages(db_session, month_only)
    assert result.recommendations[0].package_id == str(recurring.id)
    assert result.recommendations[0].season_match == "Peak"
    bounded = next(item for item in result.recommendations if item.title == "Alleppey absolute season")
    assert bounded.verified_price is None
    assert bounded.price_status == "staff_confirmation"


def test_duration_ranking_and_traveller_caveats_do_not_invent_capacity_or_child_fares(db_session):
    near = add_package(db_session, title="Alleppey six days", destinations=["Alleppey"], duration_days=6)
    add_package(db_session, title="Alleppey two weeks", destinations=["Alleppey"], duration_days=14)
    result = recommend_packages(db_session, make_extraction(children=2, infants=1))
    assert result.recommendations[0].package_id == str(near.id)
    assert any("Child/infant fares" in item for item in result.recommendations[0].caveats)
    assert any("availability" in item for item in result.recommendations[0].caveats)


def test_budget_comparison_requires_inr_per_person_scope_and_compatible_basis(db_session):
    under = add_package(db_session, title="Alleppey under budget", destinations=["Alleppey"], price="32000.00")
    add_package(db_session, title="Alleppey above budget", destinations=["Alleppey"], price="45000.00", season_name="High")
    within = recommend_packages(db_session, make_extraction())
    by_id = {item.package_id: item for item in within.recommendations}
    assert by_id[str(under.id)].score > by_id[next(key for key in by_id if key != str(under.id))].score
    assert any("within the stated per-person budget" in reason for reason in by_id[str(under.id)].match_reasons)

    total_budget = recommend_packages(db_session, make_extraction(budget_scope="total"))
    assert any("no group total was calculated" in caveat for item in total_budget.recommendations for caveat in item.caveats)
    foreign = recommend_packages(db_session, make_extraction(budget_currency="EUR"))
    assert any("no conversion was made" in caveat for item in foreign.recommendations for caveat in item.caveats)
    ambiguous = recommend_packages(db_session, make_extraction(budget_scope=None, ambiguous_information=["budget_scope"]))
    assert any("scope is unknown" in caveat for item in ambiguous.recommendations for caveat in item.caveats)


def test_missing_prices_and_gst_are_explicit_and_no_price_is_invented(db_session):
    package = add_package(db_session, title="Alleppey without rate", destinations=["Alleppey"], price=None)
    result = recommend_packages(db_session, make_extraction())
    recommendation = result.recommendations[0]
    assert recommendation.package_id == str(package.id)
    assert recommendation.verified_price is None
    assert "staff must confirm pricing" in " ".join(recommendation.caveats)
    assert "GST treatment" in " ".join(recommendation.caveats)


def test_month_not_in_price_window_has_no_selected_price(db_session):
    add_package(db_session, title="Alleppey summer", destinations=["Alleppey"], start_month=6, end_month=9)
    october = make_extraction(
        travel_start_date=None, travel_end_date=None, travel_dates_flexible=None,
        original_travel_period_text="October", missing_information=["travel_start_date", "travel_end_date"],
        ambiguous_information=["travel_year"],
    )
    result = recommend_packages(db_session, october)
    assert len(result.recommendations) == 1
    assert result.recommendations[0].verified_price is None
    assert result.recommendations[0].price_status == "staff_confirmation"


def test_no_destination_and_no_period_returns_honest_no_match(db_session):
    add_package(db_session, title="Alleppey family stay", destinations=["Alleppey"])
    unknown = make_extraction(
        destination=None,
        travel_start_date=None,
        travel_end_date=None,
        travel_dates_flexible=None,
        original_travel_period_text=None,
        adults=None,
        children=None,
        infants=None,
        budget_amount=None,
        budget_currency=None,
        budget_scope=None,
        missing_information=["destination", "travel_start_date", "travel_end_date", "adults", "children", "infants", "budget_amount"],
    )
    result = recommend_packages(db_session, unknown)
    assert not result.recommendations
    assert "not enough validated" in result.explanation


def test_ranking_is_stable_limited_to_three_and_draft_packages_are_excluded(db_session):
    packages = [
        add_package(db_session, title=f"Alleppey option {i}", destinations=["Alleppey"])
        for i in range(4)
    ]
    draft = add_package(db_session, title="Alleppey draft", destinations=["Alleppey"], status="draft")
    first = recommend_packages(db_session, make_extraction(), limit=10)
    second = recommend_packages(db_session, make_extraction(), limit=10)
    assert len(first.recommendations) == 3
    assert tuple(item.package_id for item in first.recommendations) == tuple(item.package_id for item in second.recommendations)
    ids = {item.package_id for item in first.recommendations}
    assert str(draft.id) not in ids
    assert len(ids) == 3


def test_budget_price_basis_does_not_compare_unsupported_basis(db_session):
    add_package(db_session, title="Alleppey group rate", destinations=["Alleppey"], price_basis="per_person")
    extraction = make_extraction(budget_scope="total")
    result = recommend_packages(db_session, extraction)
    assert result.recommendations[0].verified_price.price_basis == "per_person"
    assert not any("within the stated" in reason for reason in result.recommendations[0].match_reasons)


def test_recommendation_calculation_does_not_mutate_enquiry_or_extraction(db_session, monkeypatch):
    from app.services.ai import orchestrator

    def no_provider_call(*args, **kwargs):
        raise AssertionError("recommendation engine must not invoke an AI provider")

    monkeypatch.setattr(orchestrator, "generate", no_provider_call)
    package = add_package(db_session, title="Alleppey houseboat", destinations=["Alleppey"])
    enquiry = Enquiry(original_message="Synthetic untouched message", customer_name="Synthetic", travel_start_date=date(2027, 10, 30), travel_end_date=date(2027, 11, 4), travel_dates_flexible=False, adults=2)
    suggestion = AISuggestion(enquiry=enquiry, extracted_data=make_extraction().model_dump(mode="json"), accepted_extraction=None)
    db_session.add(suggestion)
    db_session.flush()
    original_message, raw, accepted = enquiry.original_message, dict(suggestion.extracted_data), suggestion.accepted_extraction
    result = recommend_packages(db_session, make_extraction())
    db_session.flush()
    assert result.recommendations[0].package_id == str(package.id)
    assert enquiry.original_message == original_message
    assert suggestion.extracted_data == raw and suggestion.accepted_extraction == accepted
    assert not db_session.is_modified(enquiry) and not db_session.is_modified(suggestion)

@pytest.fixture
def recommendation_staff_client(db_session):
    from fastapi.testclient import TestClient
    from app.database import get_db
    from app.main import app
    from app.services.session_store import SESSION_COOKIE_NAME, issue_session

    app.dependency_overrides[get_db] = lambda: db_session
    def make(user):
        client = TestClient(app, follow_redirects=False)
        if user is not None:
            _, token = issue_session(db_session, user_id=user.id)
            db_session.flush()
            client.cookies.set(SESSION_COOKIE_NAME, token)
        return client
    try:
        yield make
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_workspace_prefers_accepted_extraction_and_marks_raw_only_recommendations_provisional(db_session, recommendation_staff_client):
    admin = User(name="Recommendation Admin", email=f"{uuid4()}@example.test", role="admin")
    db_session.add(admin)
    db_session.flush()
    enquiry = Enquiry(
        customer_name="Synthetic recommendation test",
        original_message="Synthetic source message remains unchanged.",
        status="new",
        assigned_to_id=admin.id,
    )
    raw_package = add_package(db_session, title="Munnar-only raw candidate", destinations=["Munnar"])
    accepted_package = add_package(db_session, title="Alleppey accepted candidate", destinations=["Alleppey"])
    raw_payload = make_extraction(destination="Munnar").model_dump(mode="json")
    accepted_payload = make_extraction(destination="Alleppey").model_dump(mode="json")
    suggestion = AISuggestion(
        enquiry=enquiry,
        generation_status="completed",
        extracted_data=raw_payload,
        accepted_extraction=accepted_payload,
        extraction_review_status="accepted",
        extraction_reviewed_by=admin,
        extraction_reviewed_at=datetime.now(UTC),
    )
    db_session.add(suggestion)
    db_session.flush()
    original, raw_before, accepted_before = enquiry.original_message, dict(suggestion.extracted_data), dict(suggestion.accepted_extraction)
    client = recommendation_staff_client(admin)
    try:
        reviewed_response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    recommendation_section = reviewed_response.text.split("Package recommendations", 1)[1]
    assert "Based on staff-accepted extraction" in recommendation_section
    assert "Alleppey accepted candidate" in recommendation_section
    assert "₹32500.00 INR per person" in recommendation_section
    assert "GST treatment" in recommendation_section
    assert "Munnar-only raw candidate" not in recommendation_section
    assert reviewed_response.headers["cache-control"] == "no-store"
    assert reviewed_response.headers["x-robots-tag"] == "noindex, nofollow"
    assert enquiry.original_message == original
    assert suggestion.extracted_data == raw_before
    assert suggestion.accepted_extraction == accepted_before

    suggestion.accepted_extraction = None
    suggestion.extraction_review_status = "pending"
    suggestion.extraction_reviewed_by = None
    suggestion.extraction_reviewed_at = None
    db_session.flush()
    client = recommendation_staff_client(admin)
    try:
        provisional_response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    provisional_section = provisional_response.text.split("Package recommendations", 1)[1]
    assert "Provisional" in provisional_section
    assert "Munnar-only raw candidate" in provisional_section
    assert enquiry.original_message == original
    assert suggestion.extracted_data == raw_before
    assert suggestion.accepted_extraction is None


def test_consultant_cannot_view_recommendations_for_unassigned_enquiry(db_session, recommendation_staff_client):
    consultant = User(name="Scoped consultant", email=f"{uuid4()}@example.test", role="consultant")
    other = User(name="Other consultant", email=f"{uuid4()}@example.test", role="consultant")
    db_session.add_all([consultant, other])
    db_session.flush()
    enquiry = Enquiry(original_message="Synthetic private request", status="new", assigned_to_id=other.id)
    suggestion = AISuggestion(enquiry=enquiry, generation_status="completed", extracted_data=make_extraction().model_dump(mode="json"))
    db_session.add(suggestion)
    db_session.flush()
    client = recommendation_staff_client(consultant)
    try:
        response = client.get(f"/admin/enquiries/{enquiry.id}")
    finally:
        client.close()
    assert response.status_code == 404
    assert "Package recommendations" not in response.text

def test_overlapping_stored_price_rows_are_not_arbitrarily_selected(db_session):
    package = add_package(db_session, title="Alleppey overlapping rates", destinations=["Alleppey"])
    package.prices.append(PackagePrice(
        season_name="Second peak", start_month=10, end_month=3,
        price_per_person=Decimal("35000.00"), currency="INR", price_basis="per_person",
    ))
    db_session.flush()
    result = recommend_packages(db_session, make_extraction())
    recommendation = result.recommendations[0]
    assert recommendation.verified_price is None
    assert recommendation.price_status == "ambiguous"
    assert any("Multiple stored seasonal price rows" in caveat for caveat in recommendation.caveats)

def test_explicit_cross_year_month_range_matches_recurring_season_without_guessing_year(db_session):
    package = add_package(db_session, title="Alleppey winter", destinations=["Alleppey"], start_month=10, end_month=3)
    winter = make_extraction(
        travel_start_date=None, travel_end_date=None, travel_dates_flexible=None,
        original_travel_period_text="October–March",
        missing_information=["travel_start_date", "travel_end_date"],
        ambiguous_information=["travel_year"],
    )
    result = recommend_packages(db_session, winter)
    assert len(result.recommendations) == 1
    assert result.recommendations[0].package_id == str(package.id)
    assert result.recommendations[0].season_match == "Peak"

def test_non_inr_package_price_is_not_compared_to_inr_budget(db_session):
    add_package(db_session, title="Alleppey foreign rate", destinations=["Alleppey"], currency="EUR")
    result = recommend_packages(db_session, make_extraction())
    recommendation = result.recommendations[0]
    assert recommendation.verified_price.currency == "EUR"
    assert not any("within the stated" in reason for reason in recommendation.match_reasons)
    assert any("requires compatible INR per-person pricing" in caveat for caveat in recommendation.caveats)


def test_non_per_person_price_basis_is_not_compared_to_per_person_budget(db_session):
    add_package(db_session, title="Alleppey room rate", destinations=["Alleppey"], price_basis="per_room")
    result = recommend_packages(db_session, make_extraction())
    recommendation = result.recommendations[0]
    assert recommendation.verified_price.price_basis == "per_room"
    assert not any("within the stated" in reason for reason in recommendation.match_reasons)

def test_title_phrase_matches_exact_named_destination_but_not_generic_shared_terms(db_session):
    sample = add_package(
        db_session,
        title="Munnar, Thekkady and Alleppey houseboat",
        destinations=None,
    )
    add_package(db_session, title="Generic Hill and Lake retreat", destinations=None)
    result = recommend_packages(db_session, make_extraction(destination="Munnar"))
    assert len(result.recommendations) == 1
    assert result.recommendations[0].package_id == str(sample.id)
    no_generic = recommend_packages(db_session, make_extraction(destination="Hill"))
    assert not no_generic.recommendations

