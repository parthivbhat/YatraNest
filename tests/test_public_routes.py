from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from xml.etree import ElementTree

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.database import engine
from app.main import app
from app.models import BlogPost, Package, PackagePrice
from app.routes.public import home, package_detail, package_list, sitemap


@pytest.fixture
def db_session():
    """Run route queries in a PostgreSQL transaction rolled back after each test."""
    with engine.connect() as connection:
        outer_transaction = connection.begin()
        session = Session(bind=connection, join_transaction_mode="create_savepoint")
        try:
            yield session
        finally:
            session.close()
            outer_transaction.rollback()


def make_request(path: str = "/") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "headers": [(b"host", b"isocode.test")],
            "query_string": b"",
            "server": ("isocode.test", 80),
            "client": ("test", 1234),
            "scheme": "https",
            "app": app,
        }
    )


def test_public_route_paths_are_registered() -> None:
    route_paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/", "/packages", "/packages/{slug}", "/sitemap.xml"}.issubset(route_paths)


def test_home_page_renders_only_a_published_feature(db_session: Session, supplied_packages) -> None:
    response = home(make_request(), db_session)
    body = response.body.decode()

    assert response.status_code == 200
    assert "YatraNest" in body
    assert "Munnar, Thekkady and Alleppey houseboat" in body
    assert "Madurai, Rameswaram and Kanyakumari" not in body


def test_home_shows_latest_three_published_stories_and_never_drafts(db_session: Session) -> None:
    now = datetime(2099, 5, 1, tzinfo=timezone.utc)
    posts = [
        BlogPost(
            title=f"Synthetic story {index}",
            slug=f"synthetic-story-{index}",
            content=f"Synthetic excerpt for story {index}.",
            category="Planning",
            status="published",
            published_at=now - timedelta(days=index),
            cover_image_path=f"/static/test/story-{index}.webp",
            cover_image_alt=f"Synthetic landscape {index}",
        )
        for index in range(1, 5)
    ]
    draft = BlogPost(
        title="Synthetic unpublished story",
        slug="synthetic-unpublished-story",
        content="This draft must never appear publicly.",
        category="Planning",
        status="draft",
        published_at=None,
    )
    db_session.add_all([*posts, draft])
    db_session.flush()

    body = home(make_request(), db_session).body.decode()

    assert "Travel Stories &amp; Inspiration" in body
    assert body.count('class="blog-card"') == 3
    for index in (1, 2, 3):
        assert f"Synthetic story {index}" in body
        assert f'alt="Synthetic landscape {index}"' in body
        assert f"Synthetic excerpt for story {index}." in body
        assert f'href="/blog/synthetic-story-{index}"' in body
    assert [body.index(f"Synthetic story {index}") for index in (1, 2, 3)] == sorted(
        body.index(f"Synthetic story {index}") for index in (1, 2, 3)
    )
    assert "Synthetic story 4" not in body
    assert "Synthetic unpublished story" not in body
    assert 'href="/blog">View all stories' in body


def test_home_hides_stories_section_when_no_published_posts(monkeypatch, db_session: Session) -> None:
    monkeypatch.setattr("app.routes.public.latest_published_blog_posts", lambda db, *, limit: [])
    body = home(make_request(), db_session).body.decode()

    assert "Travel Stories &amp; Inspiration" not in body
    assert "View all stories" not in body


def test_package_listing_never_shows_draft_package(db_session: Session, supplied_packages) -> None:
    response = package_list(make_request("/packages"), db_session)
    body = response.body.decode()

    assert response.status_code == 200
    assert "Munnar, Thekkady and Alleppey houseboat" in body
    assert "Madurai, Rameswaram and Kanyakumari" not in body
    assert "₹32,500" in body
    assert "₹24,900" in body


@pytest.mark.parametrize("query", ["houseboat", "PKG-KL-07", "Munnar"])
def test_package_text_search_matches_published_package(db_session: Session, supplied_packages, query: str) -> None:
    response = package_list(make_request("/packages"), db_session, q=query)
    body = response.body.decode()

    assert "Munnar, Thekkady and Alleppey houseboat" in body
    assert "Madurai, Rameswaram and Kanyakumari" not in body


def test_package_search_returns_empty_when_only_draft_matches(db_session: Session, supplied_packages) -> None:
    response = package_list(make_request("/packages"), db_session, q="Rameswaram")
    body = response.body.decode()

    assert "No journeys found" in body
    assert "Madurai, Rameswaram and Kanyakumari" not in body


def test_text_search_treats_empty_month_and_year_as_absent(db_session: Session, supplied_packages) -> None:
    response = package_list(
        make_request("/packages"),
        db_session,
        q="houseboat",
        month="",
        year="",
    )

    assert response.status_code == 200
    assert "Munnar, Thekkady and Alleppey houseboat" in response.body.decode()


def test_empty_month_and_year_show_all_published_packages(db_session: Session, supplied_packages) -> None:
    response = package_list(make_request("/packages"), db_session, month="", year="")
    body = response.body.decode()

    assert response.status_code == 200
    assert "Munnar, Thekkady and Alleppey houseboat" in body
    assert "Madurai, Rameswaram and Kanyakumari" not in body


@pytest.mark.parametrize(
    ("month", "expected"),
    [("3", True), ("10", True), ("6", True), ("4", False)],
)
def test_travel_month_filter_handles_recurring_seasons(
    db_session: Session,
    supplied_packages,
    month: str,
    expected: bool,
) -> None:
    response = package_list(make_request("/packages"), db_session, month=month)
    contains_kerala_package = "Munnar, Thekkady and Alleppey houseboat" in response.body.decode()

    assert contains_kerala_package is expected


def test_month_only_filter_treats_empty_year_as_absent(db_session: Session, supplied_packages) -> None:
    response = package_list(make_request("/packages"), db_session, month="3", year="")

    assert "Munnar, Thekkady and Alleppey houseboat" in response.body.decode()


def test_month_and_year_filter_still_works(db_session: Session, supplied_packages) -> None:
    response = package_list(make_request("/packages"), db_session, month="3", year="2028")

    assert "Munnar, Thekkady and Alleppey houseboat" in response.body.decode()


@pytest.mark.parametrize("month", ["0", "13", "abc"])
def test_invalid_month_is_rejected_with_422(db_session: Session, month: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        package_list(make_request("/packages"), db_session, month=month)

    assert exc_info.value.status_code == 422


def test_year_without_month_is_rejected_with_422(db_session: Session) -> None:
    with pytest.raises(HTTPException) as exc_info:
        package_list(make_request("/packages"), db_session, month="", year="2028")

    assert exc_info.value.status_code == 422


def test_travel_year_filter_respects_absolute_price_date_window(db_session: Session) -> None:
    package = Package(
        package_code=f"TEST-{uuid4().hex[:8]}",
        slug=f"test-date-window-{uuid4().hex[:8]}",
        title="Date window package",
        status="published",
    )
    package.prices.append(
        PackagePrice(
            season_name="Peak",
            start_month=10,
            end_month=3,
            valid_from=date(2027, 10, 15),
            valid_until=date(2028, 3, 20),
            price_per_person=Decimal("1000.00"),
        )
    )
    db_session.add(package)
    db_session.flush()

    march_2027 = package_list(make_request("/packages"), db_session, month="3", year="2027").body.decode()
    march_2028 = package_list(make_request("/packages"), db_session, month="3", year="2028").body.decode()
    october_2028 = package_list(make_request("/packages"), db_session, month="10", year="2028").body.decode()

    assert "Date window package" not in march_2027
    assert "Date window package" in march_2028
    assert "Date window package" not in october_2028


def test_package_detail_shows_database_season_prices_and_hides_missing_sections(
    db_session: Session, supplied_packages
) -> None:
    response = package_detail(
        make_request("/packages/munnar-thekkady-alleppey-houseboat"),
        "munnar-thekkady-alleppey-houseboat",
        db_session,
    )
    body = response.body.decode()

    assert response.status_code == 200
    assert "₹32,500" in body
    assert "₹24,900" in body
    assert "October–March" in body
    assert "June–September" in body
    assert "per person, twin sharing" in body
    assert "GST treatment is not specified" in body
    assert "Itinerary" not in body
    assert "Hotel details" not in body
    assert "Inclusions" not in body


@pytest.mark.parametrize("slug", ["madurai-rameswaram-kanyakumari", "missing-package"])
def test_draft_or_unknown_package_detail_returns_404(db_session: Session, supplied_packages, slug: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        package_detail(make_request(f"/packages/{slug}"), slug, db_session)

    assert exc_info.value.status_code == 404


def test_sitemap_contains_published_package_slugs_only(db_session: Session, supplied_packages) -> None:
    response = sitemap(make_request("/sitemap.xml"), db_session)
    root = ElementTree.fromstring(response.body)
    locations = [node.text for node in root.iter() if node.tag.endswith("}loc")]

    assert response.media_type == "application/xml"
    assert "https://isocode.test/packages/munnar-thekkady-alleppey-houseboat" in locations
    assert not any("madurai-rameswaram-kanyakumari" in location for location in locations)
