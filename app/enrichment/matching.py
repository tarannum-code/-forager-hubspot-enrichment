"""Pick the right Forager organization when a search returns several candidates.

Real-world finding: searching `domains=["openai.com"]` returns 37 organizations, because
every custom GPT page (chat.openai.com/g/...) is listed with domain openai.com. The real
company is the one whose website *is* the domain (no deep path), that owns the matching
LinkedIn page, and that has by far the most employees.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from urllib.parse import urlparse

from app.forager.models import Organization
from app.normalize import linkedin_slug, normalize_domain


def score_organization(
    org: Organization, *, domain: str | None = None, linkedin_id: str | None = None
) -> float:
    score = 0.0
    org_linkedin = (org.linkedin_info.public_identifier or "") if org.linkedin_info else ""
    if linkedin_id and org_linkedin.lower() == linkedin_id.lower():
        score += 1000
    if domain:
        if normalize_domain(org.domain) == domain:
            score += 50
        if org.website:
            parsed = urlparse(org.website if "://" in org.website else f"https://{org.website}")
            host = normalize_domain(parsed.hostname)
            if host == domain:
                score += 100
                if parsed.path in ("", "/"):
                    score += 100  # homepage, not a sub-page like /g/<gpt-id>
    if org.linkedin_info and org.linkedin_info.public_identifier:
        score += 20
    if org.description:
        score += 5
    # Size dominates ties: log scale so 10k employees >> 2 employees.
    score += 30 * math.log10((org.employees_amount or 0) + 1)
    return score


def best_organization(
    candidates: Sequence[Organization],
    *,
    domain: str | None = None,
    linkedin_url: str | None = None,
) -> Organization | None:
    if not candidates:
        return None
    clean_domain = normalize_domain(domain) if domain else None
    linkedin_id = linkedin_slug(linkedin_url, "company") if linkedin_url else None
    return max(
        candidates,
        key=lambda org: score_organization(org, domain=clean_domain, linkedin_id=linkedin_id),
    )
