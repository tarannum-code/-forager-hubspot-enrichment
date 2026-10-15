"""Company matching, modelled on the real Forager response for openai.com, which
returns 37 organizations (custom GPT pages share the domain)."""

from __future__ import annotations

from app.enrichment.matching import best_organization
from app.forager.models import Organization
from app.normalize import parse_location


def _org(
    id_: int, name: str, website: str, employees: int | None, linkedin: str | None
) -> Organization:
    return Organization.model_validate(
        {
            "id": id_,
            "name": name,
            "website": website,
            "domain": "openai.com",
            "employees_amount": employees,
            "linkedin_info": {"public_identifier": linkedin} if linkedin else None,
        }
    )


CANDIDATES = [
    _org(16928532, "Wordwise", "https://chat.openai.com/g/g-xkHZWzlA4-wordwise", 2, "wordwisegpt"),
    _org(1, "OpenAI ChatGPT", "https://openai.com/chatgpt", 12, None),
    _org(12988797, "OpenAI", "https://openai.com/", 10480, "openai"),
    _org(2, "Drip Street", "https://chat.openai.com/", 1, None),
]


def test_picks_the_real_company_among_gpt_pages() -> None:
    best = best_organization(CANDIDATES, domain="openai.com")
    assert best is not None and best.id == 12988797


def test_linkedin_match_wins() -> None:
    best = best_organization(
        CANDIDATES, domain="openai.com", linkedin_url="https://www.linkedin.com/company/wordwisegpt"
    )
    assert best is not None and best.id == 16928532


def test_no_candidates() -> None:
    assert best_organization([], domain="openai.com") is None


def test_parse_location_real_forager_shape() -> None:
    # Exact shape returned by Forager for OpenAI (names are hierarchical, display lowercase).
    loc = parse_location(
        {
            "id": 213709,
            "name": "san francisco, united states",
            "osm_locations": [
                {"id": 10, "name": "United States", "place_type": "country"},
                {"id": 275, "name": "California, United States", "place_type": "state"},
                {
                    "id": 7746,
                    "name": "San Francisco, California, United States",
                    "place_type": "city",
                },
                {"id": 394868, "name": "North America", "place_type": "continent"},
            ],
        }
    )
    assert (loc.city, loc.state, loc.country) == ("San Francisco", "California", "United States")


def test_parse_location_lowercase_display_fallback() -> None:
    loc = parse_location({"name": "san francisco, united states", "osm_locations": []})
    assert (loc.city, loc.country) == ("San Francisco", "United States")


def test_company_name_beats_acquired_company_sharing_the_domain() -> None:
    acquired = Organization(
        id=1,
        name="Octane (We've joined Stripe!)",
        domain="stripe.com",
        website="https://stripe.com",
        employees_amount=2,
    )
    parent = Organization(
        id=2,
        name="Stripe",
        domain="stripe.com",
        website="https://stripe.com/payments",
        employees_amount=2,
    )
    best = best_organization([acquired, parent], domain="stripe.com", name="Stripe")
    assert best is not None and best.id == 2
