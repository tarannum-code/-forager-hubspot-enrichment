from __future__ import annotations

import json

import httpx
import respx

from app.hubspot.client import HubSpotClient
from tests.conftest import HUBSPOT_BASE

V = "2026-09"


def _props(*names: str) -> dict[str, list[dict[str, str]]]:
    return {"results": [{"name": n} for n in names]}


async def test_update_drops_unknown_and_empty_properties(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    mock_router.get(f"{HUBSPOT_BASE}/crm/properties/{V}/contacts").respond(
        json=_props("firstname", "city")
    )
    patch = mock_router.patch(f"{HUBSPOT_BASE}/crm/objects/{V}/contacts/101").respond(
        json={"id": "101", "properties": {}}
    )
    await hubspot.update_object(
        "contacts", "101", {"firstname": "Jane", "city": "", "made_up": "x", "lastname": None}
    )
    sent = json.loads(patch.calls.last.request.content)
    # None and unknown properties dropped; "" kept (that is how HubSpot clears a field).
    assert sent == {"properties": {"firstname": "Jane", "city": ""}}
    assert patch.calls.last.request.headers["Authorization"] == "Bearer test-token"


async def test_get_object_404_returns_none(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    mock_router.get(f"{HUBSPOT_BASE}/crm/objects/{V}/companies/9").respond(404, json={})
    assert await hubspot.get_object("companies", 9, ["name"]) is None


async def test_associated_ids_paginates(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    route = mock_router.get(f"{HUBSPOT_BASE}/crm/objects/{V}/companies/5/associations/contacts")
    route.side_effect = [
        httpx.Response(
            200, json={"results": [{"toObjectId": 1}], "paging": {"next": {"after": "a"}}}
        ),
        httpx.Response(200, json={"results": [{"toObjectId": 2}]}),
    ]
    assert await hubspot.associated_ids("companies", 5, "contacts") == ["1", "2"]
    assert route.calls[1].request.url.params["after"] == "a"


async def test_associate_default_uses_put(
    hubspot: HubSpotClient, mock_router: respx.MockRouter
) -> None:
    route = mock_router.put(
        f"{HUBSPOT_BASE}/crm/objects/{V}/contacts/1/associations/default/companies/5"
    ).respond(200, json={})
    await hubspot.associate_default("contacts", 1, "companies", 5)
    assert route.called


async def test_search_and_429_retry(hubspot: HubSpotClient, mock_router: respx.MockRouter) -> None:
    route = mock_router.post(f"{HUBSPOT_BASE}/crm/objects/{V}/companies/search")
    route.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "rate limited"}),
        httpx.Response(
            200, json={"results": [{"id": "5", "properties": {"domain": "acmeai.com"}}]}
        ),
    ]
    found = await hubspot.find_one("companies", "domain", "acmeai.com", ["domain"])
    assert found == {"id": "5", "properties": {"domain": "acmeai.com"}}
    body = json.loads(route.calls.last.request.content)
    assert body["filterGroups"][0]["filters"][0] == {
        "propertyName": "domain",
        "operator": "EQ",
        "value": "acmeai.com",
    }
