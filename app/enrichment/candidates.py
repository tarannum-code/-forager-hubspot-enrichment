"""Decide which discovered people are worth enriching (before any paid contact lookup).

Real Forager data showed that a role search for a company returns some people who should
not end up in a CRM as "works at <company>":

* past roles flagged `is_current: false` even when we asked for current roles only,
* self-reported roles contradicted by the person's own headline ("Sr. Manager at <other>"),
* locations the geocoder could not resolve consistently.

Each candidate gets a hard reject reason or a quality score. The score favours complete,
self-consistent profiles, which are also the ones most likely to have contact data, so
we spend phone/email credits on them first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.enrichment.title_filter import TitleFilter
from app.forager.models import Organization, PersonRoleSearchResult
from app.normalize import Location, linkedin_slug, parse_location

_EMPLOYER_IN_HEADLINE = re.compile(r"(?:\bat\b|@)\s*(.+)$", re.IGNORECASE)


@dataclass
class Candidate:
    row: PersonRoleSearchResult
    location: Location
    linkedin_slug: str | None
    score: int = 0
    reject_reason: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.reject_reason is None


def _headline_names_other_employer(headline: str | None, company: Organization) -> bool:
    if not headline:
        return False
    match = _EMPLOYER_IN_HEADLINE.search(headline)
    if not match:
        return False
    employer = match.group(1).lower()
    names = {n.lower() for n in (company.name, company.legal_name) if n}
    domain_root = (company.domain or "").split(".")[0].lower()
    return not any(name in employer for name in names) and not (
        domain_root and domain_root in employer
    )


def _mentions_company(text: str | None, company: Organization) -> bool:
    return bool(text and company.name and company.name.lower() in text.lower())


def assess(
    row: PersonRoleSearchResult, company: Organization, title_filter: TitleFilter
) -> Candidate:
    person = row.person
    url = person.linkedin_info.public_profile_url if person.linkedin_info else None
    slug = linkedin_slug(url) or (
        person.linkedin_info.public_identifier if person.linkedin_info else None
    )
    candidate = Candidate(row=row, location=parse_location(person.location), linkedin_slug=slug)

    org_id = row.organization.id if row.organization else None
    if org_id != company.id:
        candidate.reject_reason = "different organization"
    elif not row.is_current:
        candidate.reject_reason = "role is not current"
    elif not title_filter.matches(row.role_title):
        candidate.reject_reason = "title filter"
    elif not slug:
        candidate.reject_reason = "no LinkedIn profile"
    elif _headline_names_other_employer(person.headline, company):
        candidate.reject_reason = "headline names another employer"
    if candidate.reject_reason:
        return candidate

    loc = candidate.location
    if _mentions_company(person.headline, company):
        candidate.score += 3
        candidate.notes.append("headline confirms employer")
    if loc.city and loc.country:
        candidate.score += 3
    elif loc.country:
        candidate.score += 1
        candidate.notes.append("no city")
    else:
        candidate.score -= 3
        candidate.notes.append("location unknown" if loc.reliable else "location contradictory")
    if person.description:
        candidate.score += 1
    if len(person.skills) >= 5:
        candidate.score += 1
    if person.photo:
        candidate.score += 1
    return candidate


def rank_candidates(
    rows: list[PersonRoleSearchResult], company: Organization, title_filter: TitleFilter
) -> tuple[list[Candidate], list[Candidate]]:
    """Return (accepted best-first, rejected). Duplicate people are collapsed."""
    seen: set[int] = set()
    accepted: list[Candidate] = []
    rejected: list[Candidate] = []
    for row in rows:
        if row.person.id in seen:
            continue
        seen.add(row.person.id)
        candidate = assess(row, company, title_filter)
        (accepted if candidate.accepted else rejected).append(candidate)
    accepted.sort(key=lambda c: (-c.score, title_filter.rank(c.row.role_title)))
    return accepted, rejected
