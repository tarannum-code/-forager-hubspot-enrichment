"""Forager -> HubSpot property mapping. Pure functions, no I/O.

Standard HubSpot properties are used wherever they exist; custom ones (created by
`setup-hubspot`) hold the rest. Values are cleaned by `app.normalize`, and anything empty
or unreliable is simply omitted, so a mapping never blanks out existing data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.forager.models import (
    Organization,
    Person,
    PersonDetail,
    ShortOrganization,
)
from app.normalize import (
    Location,
    clean_job_title,
    clean_text,
    linkedin_company_url,
    linkedin_person_url,
    linkedin_slug,
    normalize_domain,
    parse_location,
    smart_name_case,
    split_full_name,
    website_url,
)

Properties = dict[str, Any]

# HubSpot text properties accept up to 65,536 characters; keep extras readable.
_LONG_TEXT = 5000
_SKILLS_LIMIT = 25


def _date(value: str | None) -> str | None:
    """Forager dates are ISO 'YYYY-MM-DD'; HubSpot date properties accept that form."""
    return value[:10] if value and re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", value) else None


def _drop_empty(props: Properties) -> Properties:
    return {k: v for k, v in props.items() if v not in (None, "", [])}


# ------------------------------------------------------------------------- industry


def _industry_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def build_industry_lookup(options: list[dict[str, Any]]) -> dict[str, str]:
    """Map normalized label/value -> option value for HubSpot's `industry` enumeration."""
    lookup: dict[str, str] = {}
    for option in options:
        value = option.get("value")
        if not value:
            continue
        for text in (option.get("label"), value):
            if text:
                lookup[_industry_key(str(text))] = str(value)
    return lookup


def map_industry(linkedin_industry: str | None, lookup: dict[str, str]) -> str | None:
    if not linkedin_industry:
        return None
    return lookup.get(_industry_key(linkedin_industry))


# ------------------------------------------------------------------------- company


def _hq_postcode(org: Organization, city: str | None) -> str | None:
    for address in org.addresses:
        if address.postcode and (not city or (address.city or "").lower() == city.lower()):
            return clean_text(address.postcode)
    return None


def _hq_street(org: Organization) -> str | None:
    for address in org.addresses:
        if address.street_name:
            return clean_text(f"{address.street_number or ''} {address.street_name}")
    return None


def _hq_summary(org: Organization) -> str | None:
    summaries = [a.summary for a in org.addresses if a.summary]
    # The longest summary is usually the most complete ("San Francisco, CA, 94110, US").
    return clean_text(max(summaries, key=len)) if summaries else None


def company_properties(
    org: Organization,
    *,
    industry_lookup: dict[str, str] | None = None,
    existing: Properties | None = None,
) -> Properties:
    """HubSpot company properties from a Forager organization.

    Name and domain are identity fields: they are only filled when HubSpot has none,
    so enrichment never renames a record a user created.
    """
    existing = existing or {}
    loc = parse_location(org.location)
    domain = normalize_domain(org.domain) or normalize_domain(org.website)
    linkedin = org.linkedin_info
    industry_name = linkedin.industry.name if linkedin and linkedin.industry else None
    keywords = ", ".join(k.name for k in org.keywords if k.name)

    props: Properties = {
        "name": None if existing.get("name") else clean_text(org.name),
        "domain": None if existing.get("domain") else domain,
        "website": website_url(domain),
        "description": clean_text(org.description),
        "numberofemployees": org.employees_amount,
        "annualrevenue": org.finance_info.revenue if org.finance_info else None,
        "city": loc.city,
        "state": loc.state,
        "country": loc.country,
        "zip": _hq_postcode(org, loc.city),
        "address": _hq_street(org),
        "linkedin_company_page": linkedin_company_url(
            linkedin.public_identifier if linkedin else None
        ),
        "industry": map_industry(industry_name, industry_lookup or {}),
        "founded_year": (org.founded_date or "")[:4] or None,
        # Custom (Forager Enrichment group)
        "forager_organization_id": org.id,
        "forager_legal_name": clean_text(org.legal_name),
        "forager_linkedin_industry": clean_text(industry_name),
        "forager_keywords": clean_text(keywords, max_length=_LONG_TEXT),
        "forager_founded_date": _date(org.founded_date),
        "forager_operating_status": clean_text(org.operating_status),
        "forager_domain_rank": org.domain_rank,
        "forager_hq_address": _hq_summary(org),
    }
    return _drop_empty(props)


# ------------------------------------------------------------------------- contact


@dataclass
class ContactData:
    """Everything known about one person, gathered from one or more Forager calls."""

    person: Person
    role_title: str | None = None
    role_start_date: str | None = None
    employer: Organization | ShortOrganization | None = None
    employer_name: str | None = None
    work_email: str | None = None
    email_status: str | None = None
    personal_email: str | None = None
    phone: str | None = None

    @property
    def detail(self) -> PersonDetail | None:
        return self.person if isinstance(self.person, PersonDetail) else None

    @property
    def location(self) -> Location:
        return parse_location(self.person.location)

    @property
    def linkedin_slug(self) -> str | None:
        info = self.person.linkedin_info
        if not info:
            return None
        return linkedin_slug(info.public_profile_url) or info.public_identifier

    @property
    def email(self) -> str | None:
        return self.work_email or self.personal_email

    def names(self) -> tuple[str | None, str | None]:
        first = smart_name_case(self.person.first_name)
        last = smart_name_case(self.person.last_name)
        if not first or not last:
            split_first, split_last = split_full_name(self.person.full_name)
            first, last = first or split_first, last or split_last
        return first, last


def _education(detail: PersonDetail | None) -> str | None:
    if not detail or not detail.educations:
        return None
    lines = []
    for edu in detail.educations:
        what = ", ".join(filter(None, [clean_text(edu.degree), clean_text(edu.field_of_study)]))
        years = "-".join(filter(None, [(edu.start_date or "")[:4], (edu.end_date or "")[:4]]))
        line = " - ".join(filter(None, [what, clean_text(edu.school_name)]))
        lines.append(f"{line} ({years})" if years else line)
    return clean_text("; ".join(filter(None, lines)), max_length=_LONG_TEXT)


def _languages(detail: PersonDetail | None) -> str | None:
    if not detail or not detail.languages:
        return None
    parts = [
        f"{lang.name} ({lang.proficiency})" if lang.proficiency else lang.name
        for lang in detail.languages
        if lang.name
    ]
    return clean_text(", ".join(p for p in parts if p))


def contact_properties(data: ContactData, *, existing: Properties | None = None) -> Properties:
    """HubSpot contact properties for the required fields plus the proposed extras.

    Identity fields a human may have typed (first/last name, email, phone) are only
    filled when empty; everything else is refreshed from Forager.
    """
    existing = existing or {}
    first, last = data.names()
    loc = data.location
    employer = data.employer
    employer_linkedin = (
        employer.linkedin_info.public_identifier if employer and employer.linkedin_info else None
    )
    skills = ", ".join(s.name for s in data.person.skills[:_SKILLS_LIMIT] if s.name)

    def fill(name: str, value: Any) -> Any:
        return None if existing.get(name) else value

    props: Properties = {
        "firstname": fill("firstname", first),
        "lastname": fill("lastname", last),
        "email": fill("email", data.email),
        "phone": fill("phone", data.phone),
        "jobtitle": clean_job_title(data.role_title),
        "hs_linkedin_url": linkedin_person_url(data.linkedin_slug),
        "company": clean_text(data.employer_name or (employer.name if employer else None)),
        "employer_company_domain": normalize_domain(employer.domain) if employer else None,
        "employer_company_linkedin_url": linkedin_company_url(employer_linkedin),
        "city": loc.city,
        "state": loc.state,
        "country": loc.country,
        # Custom extras (proposed for discussion, see EXTRA_FIELDS.md)
        "forager_person_id": data.person.id,
        "forager_headline": clean_text(data.person.headline, max_length=250),
        "forager_about": clean_text(data.person.description, max_length=_LONG_TEXT),
        "forager_skills": clean_text(skills, max_length=_LONG_TEXT),
        "forager_role_start_date": _date(data.role_start_date),
        "forager_education": _education(data.detail),
        "forager_languages": _languages(data.detail),
        "forager_email_status": clean_text(data.email_status) if data.work_email else None,
    }
    return _drop_empty(props)


REQUIRED_CONTACT_FIELDS = (
    "firstname",
    "lastname",
    "hs_linkedin_url",
    "jobtitle",
    "company",
    "employer_company_domain",
    "employer_company_linkedin_url",
    "city",
    "state",
    "country",
)


def missing_required(props: Properties) -> list[str]:
    """Required fields (email/phone are 'if available', so not listed) still empty."""
    return [name for name in REQUIRED_CONTACT_FIELDS if not props.get(name)]
