from __future__ import annotations

import json

import respx

from app.hubspot.client import HubSpotClient
from app.hubspot.properties import (
    COMPANY_PROPERTIES,
    CONTACT_PROPERTIES,
    GROUP_NAME,
    ensure_properties,
)
from tests.conftest import HUBSPOT_BASE

V = "2026-09"


def _mock_object(router: respx.MockRouter, obj: str, groups: list[str], props: list[dict]) -> tuple:  # type: ignore[type-arg]
    router.get(f"{HUBSPOT_BASE}/crm/properties/{V}/{obj}/groups").respond(
        json={"results": [{"name": g} for g in groups]}
    )
    router.get(f"{HUBSPOT_BASE}/crm/properties/{V}/{obj}").respond(json={"results": props})
    create_group = router.post(f"{HUBSPOT_BASE}/crm/properties/{V}/{obj}/groups").respond(json={})
    create = router.post(f"{HUBSPOT_BASE}/crm/properties/{V}/{obj}").respond(json={})
    return create_group, create


async def test_fresh_portal_creates_group_and_all_properties(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    c_group, c_create = _mock_object(mock_router, "contacts", ["contactinformation"], [])
    _, co_create = _mock_object(mock_router, "companies", [], [])

    report = await ensure_properties(hubspot)

    assert c_group.call_count == 1
    assert c_create.call_count == len(CONTACT_PROPERTIES)
    assert co_create.call_count == len(COMPANY_PROPERTIES)
    status = json.loads(c_create.calls[0].request.content)
    assert status["name"] == "forager_enrichment_status"
    assert status["groupName"] == GROUP_NAME
    assert {o["value"] for o in status["options"]} >= {"enriched", "in_progress"}
    assert all(action == "created" for *_, action in report)


async def test_existing_properties_are_left_alone(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    def as_existing(specs: tuple) -> list[dict]:  # type: ignore[type-arg]
        return [s.definition() | {"fieldType": s.field_type} for s in specs]

    _, c_create = _mock_object(
        mock_router, "contacts", [GROUP_NAME], as_existing(CONTACT_PROPERTIES)
    )
    _, co_create = _mock_object(
        mock_router, "companies", [GROUP_NAME], as_existing(COMPANY_PROPERTIES)
    )
    report = await ensure_properties(hubspot)
    assert c_create.call_count == co_create.call_count == 0
    assert {action for *_, action in report} == {"ok"}


async def test_type_conflict_is_reported_not_overwritten(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    conflicting = [{"name": "forager_person_id", "type": "string", "label": "x"}]
    _mock_object(mock_router, "contacts", [GROUP_NAME], conflicting)
    _mock_object(mock_router, "companies", [GROUP_NAME], [])
    report = await ensure_properties(hubspot)
    assert ("contacts", "forager_person_id", "CONFLICT: exists as type string") in report
