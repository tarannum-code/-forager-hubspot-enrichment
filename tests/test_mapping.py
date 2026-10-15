from __future__ import annotations

from app.enrichment.mapping import (
    ContactData,
    build_industry_lookup,
    company_properties,
    contact_properties,
    missing_required,
)
from app.forager.models import Organization, PersonDetail
from tests.conftest import load_fixture

ORG = Organization.model_validate(
    load_fixture("forager/organization_search.json")["search_results"][0]
)
DETAIL = PersonDetail.model_validate(load_fixture("forager/person_detail.json"))
LOOKUP = build_industry_lookup(
    [
        {"label": "Computer Software", "value": "COMPUTER_SOFTWARE"},
        {"label": "Software Development", "value": "SOFTWARE_DEVELOPMENT"},
    ]
)


def test_company_properties_full_mapping() -> None:
    props = company_properties(ORG, industry_lookup=LOOKUP)
    assert props == {
        "name": "Acme AI",
        "domain": "acmeai.com",
        "website": "https://acmeai.com",
        "description": "Acme AI builds safe, general-purpose AI systems for businesses.",
        "numberofemployees": 3200,
        "annualrevenue": 3_700_000_000,
        "city": "San Francisco",
        "state": "California",
        "country": "United States",
        "zip": "94110",
        "address": "100 Example Street",
        "linkedin_company_page": "https://www.linkedin.com/company/acme-ai",
        "industry": "SOFTWARE_DEVELOPMENT",
        "founded_year": "2015",
        "forager_organization_id": 501,
        "forager_legal_name": "Acme AI, Inc.",
        "forager_linkedin_industry": "Software Development",
        "forager_keywords": "artificial intelligence, machine learning",
        "forager_founded_date": "2015-12-11",
        "forager_operating_status": "active",
        "forager_domain_rank": 1520,
        "forager_hq_address": "100 Example Street, San Francisco, CA 94110, United States",
    }


def test_company_identity_fields_are_not_overwritten() -> None:
    props = company_properties(ORG, existing={"name": "Acme (HQ)", "domain": "acmeai.com"})
    assert "name" not in props and "domain" not in props


def test_unknown_industry_is_left_out_but_kept_as_text() -> None:
    props = company_properties(ORG, industry_lookup={})
    assert "industry" not in props
    assert props["forager_linkedin_industry"] == "Software Development"


def _jane(**kw: object) -> ContactData:
    role = DETAIL.current_role()
    assert role is not None
    return ContactData(
        person=DETAIL,
        role_title=role.role_title,
        role_start_date=role.start_date,
        employer=role.organization,
        employer_name=role.organization_name,
        **kw,  # type: ignore[arg-type]
    )


def test_contact_properties_required_fields_and_extras() -> None:
    props = contact_properties(
        _jane(work_email="jane@acmeai.com", email_status="valid", phone="+1 415-555-2671")
    )
    assert props["firstname"] == "Jane"
    assert props["lastname"] == "Doe-Smith"  # ALL CAPS fixed
    assert props["jobtitle"] == "VP of Engineering"
    assert props["hs_linkedin_url"] == "https://www.linkedin.com/in/jane-doe-smith"
    assert props["company"] == "Acme AI"
    assert props["employer_company_domain"] == "acmeai.com"
    assert props["employer_company_linkedin_url"] == "https://www.linkedin.com/company/acme-ai"
    assert props["email"] == "jane@acmeai.com"
    assert props["phone"] == "+1 415-555-2671"
    assert (props["city"], props["state"], props["country"]) == (
        "San Francisco",
        "California",
        "United States",
    )
    assert props["forager_role_start_date"] == "2022-03-01"
    assert props["forager_education"] == "BSc, Computer Science - State University (2008-2012)"
    assert props["forager_languages"] == "English (Native), Spanish"
    assert props["forager_email_status"] == "valid"
    assert missing_required(props) == []


def test_contact_never_overwrites_human_entered_identity() -> None:
    props = contact_properties(
        _jane(work_email="jane@acmeai.com"),
        existing={"firstname": "Janie", "email": "janie@personal.com"},
    )
    assert "firstname" not in props and "email" not in props
    assert props["lastname"] == "Doe-Smith"


def test_missing_required_lists_gaps() -> None:
    assert missing_required({"firstname": "A"})[:2] == ["lastname", "hs_linkedin_url"]
