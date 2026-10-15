"""Custom HubSpot properties this service writes, and an idempotent installer.

Standard HubSpot properties (firstname, city, annualrevenue, ...) are used wherever they
exist; custom ones are created only for data HubSpot has no field for, all inside a
"Forager Enrichment" property group so they are easy to find (and to remove).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.hubspot.client import HubSpotClient

logger = logging.getLogger(__name__)

GROUP_NAME = "forager_enrichment"
GROUP_LABEL = "Forager Enrichment"

# Values of forager_enrichment_status
STATUS_IN_PROGRESS = "in_progress"
STATUS_ENRICHED = "enriched"
STATUS_PARTIAL = "partial"
STATUS_NOT_FOUND = "not_found"
STATUS_FAILED = "failed"

# Values of forager_source (contacts)
SOURCE_WEBHOOK = "webhook"
SOURCE_COMPANY_FLOW = "company_flow"
SOURCE_CLI = "cli"


def _enum(values: list[tuple[str, str]]) -> list[dict[str, Any]]:
    return [
        {"label": label, "value": value, "displayOrder": i, "hidden": False}
        for i, (value, label) in enumerate(values)
    ]


@dataclass(frozen=True)
class PropertySpec:
    name: str
    label: str
    type: str = "string"
    field_type: str = "text"
    description: str = ""
    options: tuple[tuple[str, str], ...] = ()

    def definition(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "fieldType": self.field_type,
            "groupName": GROUP_NAME,
            "description": self.description,
        }
        if self.options:
            body["options"] = _enum(list(self.options))
        return body


_STATUS = PropertySpec(
    "forager_enrichment_status",
    "Forager Enrichment Status",
    "enumeration",
    "select",
    "Set by the enrichment service. Records with any status are never re-enriched "
    "automatically (prevents loops and double credit spend).",
    (
        (STATUS_IN_PROGRESS, "In progress"),
        (STATUS_ENRICHED, "Enriched"),
        (STATUS_PARTIAL, "Partially enriched"),
        (STATUS_NOT_FOUND, "Not found in Forager"),
        (STATUS_FAILED, "Failed"),
    ),
)
_COMMON = (
    _STATUS,
    PropertySpec("forager_enriched_at", "Forager Enriched At", "datetime", "date"),
    PropertySpec(
        "forager_enrichment_started_at",
        "Forager Enrichment Started At",
        "datetime",
        "date",
        "Used to detect and retry runs that crashed mid-way.",
    ),
    PropertySpec("forager_last_error", "Forager Last Error", "string", "textarea"),
)

CONTACT_PROPERTIES: tuple[PropertySpec, ...] = (
    *_COMMON,
    PropertySpec(
        "forager_source",
        "Forager Source",
        "enumeration",
        "select",
        "How the record entered the enrichment flow.",
        (
            (SOURCE_WEBHOOK, "Contact webhook"),
            (SOURCE_COMPANY_FLOW, "Found via company enrichment"),
            (SOURCE_CLI, "CLI"),
        ),
    ),
    PropertySpec("forager_person_id", "Forager Person ID", "number", "number"),
    # Required fields with no standard HubSpot property. (Person LinkedIn URL uses the
    # standard `hs_linkedin_url`; employer name uses the standard `company`.)
    PropertySpec("employer_company_domain", "Company Domain"),
    PropertySpec("employer_company_linkedin_url", "Company LinkedIn URL"),
    # Extra fields (proposed for discussion - see EXTRA_FIELDS.md):
    PropertySpec("forager_headline", "LinkedIn Headline"),
    PropertySpec("forager_about", "LinkedIn About", "string", "textarea"),
    PropertySpec("forager_skills", "Skills", "string", "textarea"),
    PropertySpec("forager_role_start_date", "Current Role Start Date", "date", "date"),
    PropertySpec("forager_education", "Education", "string", "textarea"),
    PropertySpec("forager_languages", "Languages"),
    PropertySpec("forager_email_status", "Email Verification Status"),
)

COMPANY_PROPERTIES: tuple[PropertySpec, ...] = (
    *_COMMON,
    PropertySpec("forager_organization_id", "Forager Organization ID", "number", "number"),
    # Extra fields (proposed for discussion - see EXTRA_FIELDS.md):
    # (Logo, employee range, founded year use standard hs_logo_url, hs_employee_range,
    # founded_year.)
    PropertySpec("forager_legal_name", "Legal Name"),
    PropertySpec("forager_linkedin_industry", "LinkedIn Industry"),
    PropertySpec("forager_keywords", "Keywords", "string", "textarea"),
    PropertySpec("forager_founded_date", "Founded Date", "date", "date"),
    PropertySpec("forager_operating_status", "Operating Status"),
    PropertySpec("forager_domain_rank", "Domain Rank", "number", "number"),
    PropertySpec("forager_hq_address", "HQ Address (full)", "string", "textarea"),
)

PROPERTIES_BY_OBJECT: dict[str, tuple[PropertySpec, ...]] = {
    "contacts": CONTACT_PROPERTIES,
    "companies": COMPANY_PROPERTIES,
}


async def ensure_properties(hubspot: HubSpotClient) -> list[tuple[str, str, str]]:
    """Create the property group and any missing properties. Safe to run repeatedly.

    Returns (object_type, property_name, action) rows for reporting.
    """
    report: list[tuple[str, str, str]] = []
    for object_type, specs in PROPERTIES_BY_OBJECT.items():
        groups = {g["name"] for g in await hubspot.list_property_groups(object_type)}
        if GROUP_NAME not in groups:
            await hubspot.create_property_group(object_type, GROUP_NAME, GROUP_LABEL)
            report.append((object_type, f"[group] {GROUP_NAME}", "created"))

        existing = {p["name"]: p for p in await hubspot.list_properties(object_type)}
        for spec in specs:
            current = existing.get(spec.name)
            if current is None:
                await hubspot.create_property(object_type, spec.definition())
                report.append((object_type, spec.name, "created"))
            elif current.get("type") != spec.type:
                # Changing a property's type can destroy data; surface it instead.
                report.append(
                    (object_type, spec.name, f"CONFLICT: exists as type {current.get('type')}")
                )
            elif _needs_update(current, spec):
                await hubspot.update_property(object_type, spec.name, spec.definition())
                report.append((object_type, spec.name, "updated"))
            else:
                report.append((object_type, spec.name, "ok"))
    hubspot.invalidate_property_cache()
    return report


def _needs_update(current: dict[str, Any], spec: PropertySpec) -> bool:
    if current.get("label") != spec.label or current.get("fieldType") != spec.field_type:
        return True
    if spec.options:
        have = {o.get("value") for o in current.get("options") or []}
        return have != {value for value, _ in spec.options}
    return False
