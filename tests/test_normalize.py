from __future__ import annotations

import pytest

from app.normalize import (
    clean_job_title,
    clean_text,
    linkedin_company_url,
    linkedin_person_url,
    linkedin_slug,
    normalize_domain,
    normalize_email,
    normalize_phone,
    parse_location,
    smart_name_case,
    split_full_name,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("JANE", "Jane"),
        ("doe-smith", "Doe-Smith"),
        ("o'neil", "O'Neil"),
        ("MCDONALD", "McDonald"),
        ("ludwig VAN beethoven", "Ludwig van Beethoven"),
        ("DeAndre", "DeAndre"),  # intentional mixed case is preserved
        ("john smith iii", "John Smith III"),
        ("  ", None),
        ("N/A", None),
    ],
)
def test_smart_name_case(raw: str, expected: str | None) -> None:
    assert smart_name_case(raw) == expected


def test_split_full_name() -> None:
    assert split_full_name("JANE DOE-SMITH") == ("Jane", "Doe-Smith")
    assert split_full_name("Cher") == ("Cher", None)


def test_clean_text_collapses_whitespace_and_truncates() -> None:
    assert clean_text("  a   b \n c ") == "a b c"
    assert clean_text("null") is None
    assert clean_text("abcdefghij", max_length=5) == "abcd…"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("VP OF ENGINEERING", "VP Of Engineering"),
        ("head of gtm", "Head Of GTM"),
        ("Chief Technology Officer", "Chief Technology Officer"),
    ],
)
def test_clean_job_title(raw: str, expected: str) -> None:
    assert clean_job_title(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.OpenAI.com/about", "openai.com"),
        ("openai.com", "openai.com"),
        ("http://sub.example.co.uk/", "sub.example.co.uk"),
        ("localhost", None),
        ("", None),
    ],
)
def test_normalize_domain(raw: str, expected: str | None) -> None:
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize(
    ("raw", "kind", "expected"),
    [
        ("https://www.linkedin.com/in/jane-doe-smith/", "in", "jane-doe-smith"),
        ("linkedin.com/in/jos%C3%A9-p", "in", "josé-p"),
        ("https://www.linkedin.com/company/openai/", "company", "openai"),
        ("https://www.linkedin.com/company/openai/", "in", None),
        ("billgates", "in", "billgates"),
        ("not a url at all", "in", None),
    ],
)
def test_linkedin_slug(raw: str, kind: str, expected: str | None) -> None:
    assert linkedin_slug(raw, kind) == expected


def test_linkedin_urls() -> None:
    assert linkedin_person_url("jane") == "https://www.linkedin.com/in/jane"
    assert linkedin_company_url("openai") == "https://www.linkedin.com/company/openai"
    assert linkedin_person_url(None) is None


def test_normalize_email() -> None:
    assert normalize_email(" Jane@AcmeAI.com ") == "jane@acmeai.com"
    assert normalize_email("Jane <jane@acme.io> (valid)") == "jane@acme.io"
    assert normalize_email("not-an-email") is None


@pytest.mark.parametrize(
    ("raw", "country", "expected"),
    [
        ("4155552671", "United States", "+1 415-555-2671"),
        ("+44 20 7946 0958", None, "+44 20 7946 0958"),
        ("n/a", None, None),
        ("12", "United States", None),
    ],
)
def test_normalize_phone(raw: str, country: str | None, expected: str | None) -> None:
    assert normalize_phone(raw, country) == expected


def test_parse_location_prefers_osm_hierarchy() -> None:
    loc = parse_location(
        {
            "name": "SF",
            "osm_locations": [
                {"name": "San Francisco", "place_type": "city"},
                {"name": "California", "place_type": "state"},
                {"name": "United States", "place_type": "country"},
            ],
        }
    )
    assert (loc.city, loc.state, loc.country) == ("San Francisco", "California", "United States")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Austin, Texas, United States", ("Austin", "Texas", "United States")),
        ("San Francisco Bay Area", ("San Francisco", "California", "United States")),
        ("California, United States", (None, "California", "United States")),
        ("London, United Kingdom", ("London", None, "United Kingdom")),
        ("Seattle, WA, USA", ("Seattle", "Washington", "United States")),
        ("Germany", (None, None, "Germany")),
    ],
)
def test_parse_location_from_display_name(name: str, expected: tuple[str | None, ...]) -> None:
    loc = parse_location({"name": name, "osm_locations": []})
    assert (loc.city, loc.state, loc.country) == expected


def test_parse_location_empty() -> None:
    assert parse_location(None).is_empty
