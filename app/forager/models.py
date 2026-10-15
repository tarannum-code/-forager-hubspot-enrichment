"""Typed views of Forager responses, following the published OpenAPI spec
(https://docs.forager.ai/_bundle/openapi.yaml).

Models are deliberately lenient: every field is optional and unknown keys are kept
(`extra="allow"`), so a new field from Forager never breaks enrichment. It shows up
in the raw cache and can be reviewed for EXTRA_FIELDS.md.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class OSMLocation(_Model):
    id: int | None = None
    name: str | None = None
    place_type: str | None = None


class InnerLocation(_Model):
    id: int | None = None
    name: str | None = None
    osm_locations: list[OSMLocation] = Field(default_factory=list)


class LinkedinIndustry(_Model):
    id: int | None = None
    name: str | None = None


class LinkedinInfo(_Model):
    public_identifier: str | None = None
    industry: LinkedinIndustry | None = None
    public_profile_url: str | None = None


class OrganizationKeyword(_Model):
    id: int | None = None
    name: str | None = None


class OrganizationFinanceInfo(_Model):
    revenue: int | None = None


class OrganizationAddress(_Model):
    street_number: str | None = None
    street_name: str | None = None
    city: str | None = None
    state: str | None = None
    postcode: str | None = None
    country: str | None = None
    summary: str | None = None


class Organization(_Model):
    """`OrganizationSearchResult`; also embedded in person-role search results."""

    id: int
    name: str | None = None
    legal_name: str | None = None
    website: str | None = None
    domain: str | None = None
    domain_rank: int | None = None
    logo: str | None = None
    description: str | None = None
    founded_date: str | None = None
    operating_status: str | None = None
    employees_range: str | None = None
    employees_amount: int | None = None
    keywords: list[OrganizationKeyword] = Field(default_factory=list)
    location: InnerLocation | None = None
    finance_info: OrganizationFinanceInfo | None = None
    linkedin_info: LinkedinInfo | None = None
    addresses: list[OrganizationAddress] = Field(default_factory=list)
    date_updated: str | None = None


class ShortOrganization(_Model):
    id: int | None = None
    name: str | None = None
    domain: str | None = None
    linkedin_info: LinkedinInfo | None = None


class PersonSkill(_Model):
    name: str | None = None


class Person(_Model):
    """`PersonChild` (search results) - the base of `PersonDetail`."""

    id: int
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    photo: str | None = None
    gender: str | None = None
    headline: str | None = None
    description: str | None = None
    skills: list[PersonSkill] = Field(default_factory=list)
    location: InnerLocation | None = None
    linkedin_info: LinkedinInfo | None = None
    date_updated: str | None = None


class PersonRole(_Model):
    """`PersonRoleChild` - one entry of a person's work history."""

    id: int | None = None
    role_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration: str | None = None
    description: str | None = None
    is_current: bool = False
    organization: ShortOrganization | None = None
    organization_name: str | None = None


class PersonEducation(_Model):
    school_name: str | None = None
    degree: str | None = None
    field_of_study: str | None = None
    start_date: str | None = None
    end_date: str | None = None


class PersonLanguage(_Model):
    name: str | None = None
    proficiency: str | None = None


class PersonDetail(Person):
    roles: list[PersonRole] = Field(default_factory=list)
    educations: list[PersonEducation] = Field(default_factory=list)
    languages: list[PersonLanguage] = Field(default_factory=list)
    certifications: list[dict[str, Any]] = Field(default_factory=list)
    courses: list[dict[str, Any]] = Field(default_factory=list)
    honors: list[dict[str, Any]] = Field(default_factory=list)
    organizations: list[dict[str, Any]] = Field(default_factory=list)
    patents: list[dict[str, Any]] = Field(default_factory=list)
    publications: list[dict[str, Any]] = Field(default_factory=list)
    test_scores: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    volunteering: list[dict[str, Any]] = Field(default_factory=list)

    def current_role(self) -> PersonRole | None:
        current = [r for r in self.roles if r.is_current]
        # Most recent start date first; roles without dates last.
        current.sort(key=lambda r: r.start_date or "", reverse=True)
        return current[0] if current else None


class PersonRoleSearchResult(_Model):
    """`OrganizationPersonRoleSearchResult`: a role, its person, and the full organization."""

    id: int
    role_title: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    duration: str | None = None
    description: str | None = None
    is_current: bool = False
    organization: Organization | None = None
    person: Person


class PersonRoleSearchResponse(_Model):
    search_results: list[PersonRoleSearchResult] = Field(default_factory=list)
    total_search_results: int = 0


class OrganizationSearchResponse(_Model):
    search_results: list[Organization] = Field(default_factory=list)
    total_search_results: int = 0


class PersonRoleSearchTotals(_Model):
    total_search_results: int = 0
    total_persons: int = 0
    total_organizations: int = 0


class EmailAddress(_Model):
    """`PersonEmailDetail`."""

    email: str
    email_type: str | None = None
    validation_status: str | None = None  # valid | risky | invalid | unknown


class PhoneNumber(_Model):
    """`PersonPhoneDetail`."""

    phone_number: str


_EMAIL_IN_TEXT = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# "invalid" must be tested before "valid" (substring).
_VERDICTS = ("invalid", "risky", "unknown", "valid")


class WorkEmailJob(_Model):
    """`WorkEmailLookupJob` - async work email lookup state."""

    job_id: str
    status: str  # queued | running | awaiting_provider | completed | failed
    emails: list[Any] = Field(default_factory=list)
    error_code: str | None = None
    person_id: int | None = None
    result: str | None = None  # found | not_found (once terminal)
    date_created: str | None = None
    date_completed: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {"completed", "failed"}

    @property
    def found(self) -> bool:
        return self.result == "found" and bool(self.addresses())

    def addresses(self) -> list[EmailAddress]:
        """The spec types `emails` as strings "each with its verdict"; accept either an
        object ({email, validation_status}) or a string containing the address + verdict."""
        out: list[EmailAddress] = []
        for item in self.emails:
            if isinstance(item, dict) and item.get("email"):
                out.append(EmailAddress.model_validate(item))
            elif isinstance(item, str):
                match = _EMAIL_IN_TEXT.search(item)
                if not match:
                    continue
                rest = item.replace(match.group(0), "").lower()
                verdict = next((v for v in _VERDICTS if v in rest), None)
                out.append(EmailAddress(email=match.group(0), validation_status=verdict))
        return out


class AutocompleteItem(_Model):
    id: str
    text: str  # "Organization name - organization domain"

    @property
    def name(self) -> str:
        return self.text.rsplit(" - ", 1)[0].strip()

    @property
    def domain(self) -> str | None:
        parts = self.text.rsplit(" - ", 1)
        return parts[1].strip() if len(parts) == 2 else None
