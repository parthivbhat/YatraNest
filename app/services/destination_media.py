"""Curated local destination photography and package-to-destination mapping.

The package schema has no image field. This small catalog lets public pages reuse
one local image per known destination without adding schema or upload machinery.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any
from urllib.parse import quote_plus


DESTINATIONS: tuple[dict[str, str], ...] = (
    {
        "key": "munnar",
        "name": "Munnar",
        "image": "/static/images/destinations/munnar.webp",
        "alt": "Green tea-covered hills in Munnar, Kerala",
        "description": "Tea-covered hills and highland scenery in Kerala.",
    },
    {
        "key": "thekkady",
        "name": "Thekkady",
        "image": "/static/images/destinations/thekkady.webp",
        "alt": "Lake and forest landscape in Thekkady, Kerala",
        "description": "Forest and lake landscapes around Periyar, Kerala.",
    },
    {
        "key": "alleppey",
        "name": "Alleppey",
        "image": "/static/images/destinations/alleppey.webp",
        "alt": "Houseboats on the backwaters at Alappuzha, Kerala",
        "description": "Backwater waterways and houseboat landscapes in Kerala.",
    },
    {
        "key": "madurai",
        "name": "Madurai",
        "image": "/static/images/destinations/madurai.webp",
        "alt": "Meenakshi Amman Temple towers in Madurai, Tamil Nadu",
        "description": "A historic temple city in Tamil Nadu.",
    },
    {
        "key": "rameswaram",
        "name": "Rameswaram",
        "image": "/static/images/destinations/rameswaram.webp",
        "alt": "Pamban Bridge crossing the sea near Rameswaram",
        "description": "A coastal town connected to the mainland by Pamban Bridge.",
    },
    {
        "key": "kanyakumari",
        "name": "Kanyakumari",
        "image": "/static/images/destinations/kanyakumari.webp",
        "alt": "Sunset over the ocean at Kanyakumari",
        "description": "Ocean views at India's southern coast.",
    },
)

FALLBACK_MEDIA = {
    "key": "southern-india",
    "name": "Southern India",
    "image": "/static/images/destination-placeholder.svg",
    "alt": "Illustrated landscape representing a journey through southern India",
    "description": "Explore journeys through southern India.",
}

_ALIASES = {
    "alappuzha": "alleppey",
    "alleppey": "alleppey",
    "rameshwaram": "rameswaram",
}


def _normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def package_destination_media(package: Any) -> dict[str, Any]:
    """Return a safe local hero image and supporting destinations for a package."""
    raw_destinations = getattr(package, "destinations", None) or []
    if isinstance(raw_destinations, str):
        raw_destinations = [raw_destinations]
    source_values = [str(value) for value in raw_destinations if value]
    source_values.append(str(getattr(package, "title", "") or ""))
    haystack = f" {_normalize(' '.join(source_values))} "
    matched: list[dict[str, str]] = []
    for item in DESTINATIONS:
        variants = [item["name"], item["key"]]
        variants.extend(alias for alias, key in _ALIASES.items() if key == item["key"])
        if any(f" {_normalize(variant)} " in haystack for variant in variants):
            matched.append(item)
    hero = matched[0] if matched else FALLBACK_MEDIA
    return {"hero": hero, "supporting": matched[1:], "destinations": matched}


def destination_cards_for_packages(packages: Iterable[Any]) -> list[dict[str, str | int]]:
    """Build destination cards only from names present in published packages."""
    counts: Counter[str] = Counter()
    for package in packages:
        for item in package_destination_media(package)["destinations"]:
            counts[item["key"]] += 1
    return [
        {
            **item,
            "package_count": counts[item["key"]],
            "href": f"/packages?q={quote_plus(item['name'])}",
        }
        for item in DESTINATIONS
        if counts[item["key"]]
    ]
