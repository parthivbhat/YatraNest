from pathlib import Path
from types import SimpleNamespace

from app.services.destination_media import (
    DESTINATIONS,
    FALLBACK_MEDIA,
    destination_cards_for_packages,
    package_destination_media,
)
from app.templating import templates


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_known_destination_images_are_local_and_present() -> None:
    for destination in DESTINATIONS:
        asset = PROJECT_ROOT / "app" / "static" / destination["image"].removeprefix("/static/")
        assert asset.is_file()
        assert asset.stat().st_size > 0


def test_package_image_mapping_uses_destination_and_title_aliases() -> None:
    package = SimpleNamespace(
        title="Kerala coast journey",
        destinations=["Munnar", "Alappuzha", "Thekkady"],
    )

    media = package_destination_media(package)

    assert media["hero"]["key"] == "munnar"
    assert [item["key"] for item in media["supporting"]] == ["thekkady", "alleppey"]


def test_package_title_is_used_when_destination_field_is_empty() -> None:
    package = SimpleNamespace(
        title="Munnar, Thekkady and Alleppey houseboat",
        destinations=None,
    )

    media = package_destination_media(package)

    assert [item["name"] for item in media["destinations"]] == [
        "Munnar",
        "Thekkady",
        "Alleppey",
    ]


def test_unmatched_package_uses_the_local_illustrated_fallback() -> None:
    package = SimpleNamespace(
        title="An unnamed journey",
        destinations=None,
        slug="unnamed-journey",
        package_code="PKG-TEST-01",
        duration_days=None,
        hotel_category=None,
        summary=None,
        prices=[],
    )
    media = package_destination_media(package)

    assert media["hero"] == FALLBACK_MEDIA
    fallback = PROJECT_ROOT / "app" / "static" / "images" / "destination-placeholder.svg"
    assert fallback.is_file()
    rendered_card = templates.env.get_template("packages/_package_card.html").render(package=package)
    assert "/static/images/destination-placeholder.svg" in rendered_card
    assert "Illustrated landscape representing a journey through southern India" in rendered_card


def test_homepage_destination_cards_include_only_package_destinations() -> None:
    packages = [
        SimpleNamespace(
            title="Munnar, Thekkady and Alleppey houseboat",
            destinations=None,
        )
    ]

    cards = destination_cards_for_packages(packages)

    assert [card["name"] for card in cards] == ["Munnar", "Thekkady", "Alleppey"]
    assert cards[0]["href"] == "/packages?q=Munnar"


def test_modified_jinja_templates_load_and_use_yatranest_branding() -> None:
    for name in templates.env.list_templates():
        templates.env.get_template(name)

    base = (PROJECT_ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    admin_base = (PROJECT_ROOT / "app" / "templates" / "admin" / "base.html").read_text(encoding="utf-8")
    assert "YatraNest" in base
    assert "YatraNest" in admin_base
    assert "Isocode Tours" not in base + admin_base


def test_image_credit_page_records_source_and_license_links() -> None:
    credits = (PROJECT_ROOT / "app" / "static" / "images" / "IMAGE_CREDITS.html").read_text(encoding="utf-8")

    for destination in ("Munnar", "Thekkady", "Alleppey", "Madurai", "Rameswaram", "Kanyakumari"):
        assert destination in credits
    assert "creativecommons.org" in credits
    assert "WebP" in credits
