"""Candidate quality rules, built from patterns seen in real Forager results."""

from __future__ import annotations

from typing import Any

import pytest

from app.enrichment.candidates import assess, rank_candidates
from app.enrichment.title_filter import TitleFilter
from app.forager.models import Organization, PersonRoleSearchResult
from app.normalize import parse_location
from tests.conftest import load_fixture

COMPANY = Organization(id=1, name="OpenAI", domain="openai.com")
FILTER = TitleFilter.parse("Chief,VP,Head of,Director", "Intern")
SF = {
    "name": "san francisco, california, united states",
    "osm_locations": [
        {"name": "United States", "place_type": "country"},
        {"name": "California, United States", "place_type": "state"},
        {"name": "San Francisco, California, United States", "place_type": "city"},
    ],
}


def _row(**overrides: Any) -> PersonRoleSearchResult:
    person = {
        "id": overrides.pop("person_id", 1),
        "full_name": "A B",
        "first_name": "A",
        "last_name": "B",
        "headline": overrides.pop("headline", "Head of Research at OpenAI"),
        "description": "About me",
        "skills": [{"name": str(i)} for i in range(6)],
        "photo": "https://example.com/p.jpg",
        "location": overrides.pop("location", SF),
        "linkedin_info": overrides.pop(
            "linkedin_info", {"public_profile_url": "https://www.linkedin.com/in/a-b"}
        ),
    }
    row = {
        "id": 10,
        "role_title": "Head of Research",
        "is_current": True,
        "organization": {"id": 1, "name": "OpenAI"},
        "person": person,
    }
    row.update(overrides)
    return PersonRoleSearchResult.model_validate(row)


def test_good_profile_scores_high() -> None:
    candidate = assess(_row(), COMPANY, FILTER)
    assert candidate.accepted
    assert candidate.score == 9
    assert candidate.linkedin_slug == "a-b"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_current": False}, "role is not current"),
        ({"organization": {"id": 99, "name": "Wordwise"}}, "different organization"),
        ({"role_title": "Research Intern"}, "title filter"),
        ({"linkedin_info": None}, "no LinkedIn profile"),
        (
            {"headline": "Sr. Manager at Vectras Enprocon Limited."},
            "headline names another employer",
        ),
        (
            {"headline": "Co-Founder and CEO at Thinking Machines Lab"},
            "headline names another employer",
        ),
    ],
)
def test_rejections(overrides: dict[str, Any], reason: str) -> None:
    assert assess(_row(**overrides), COMPANY, FILTER).reject_reason == reason


def test_headline_variants_that_confirm_employer() -> None:
    for headline in ("Deal Desk @ OpenAI", "Head of GTM, EMEA @ OpenAI", "Derivatives / AI"):
        assert assess(_row(headline=headline), COMPANY, FILTER).accepted


def test_contradictory_location_is_penalised() -> None:
    bad = {"name": "united states, india", "osm_locations": []}
    candidate = assess(_row(location=bad), COMPANY, FILTER)
    assert candidate.accepted and "location contradictory" in candidate.notes
    assert candidate.score < assess(_row(), COMPANY, FILTER).score


def test_rank_dedupes_and_orders_best_first() -> None:
    weak = _row(person_id=2, headline=None, location={"name": "turkey", "osm_locations": []})
    accepted, rejected = rank_candidates([weak, _row(), _row()], COMPANY, FILTER)
    assert [c.row.person.id for c in accepted] == [1, 2]
    assert rejected == []


# Real location objects returned by Forager for OpenAI people (names removed).
EXPECTED_REAL = [
    (None, None, None),  # "united states, côte d'ivoire" - contradictory
    ("New York", "New York", "United States"),
    (None, None, None),  # "united states, bangladesh"
    ("San Francisco", "California", "United States"),
    (None, None, "Turkey"),
    ("San Francisco", "California", "United States"),
    ("San Francisco", "California", "United States"),
    ("London", "England", "United Kingdom"),  # geocoded "City of London"
    (None, None, None),  # "united states, india" -> geocoded to India, Tennessee (hamlet)
    ("San Francisco", "California", "United States"),  # "san francisco bay"
]


def test_real_forager_locations() -> None:
    locations = load_fixture("forager/real_person_locations.json")
    parsed = [parse_location(loc) for loc in locations]
    assert [(p.city, p.state, p.country) for p in parsed] == EXPECTED_REAL
