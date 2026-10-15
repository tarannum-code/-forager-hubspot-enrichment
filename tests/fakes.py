"""In-memory stand-in for HubSpotClient, so service flows can be tested end to end."""

from __future__ import annotations

import itertools
from typing import Any

from app.hubspot.properties import PROPERTIES_BY_OBJECT


class FakeHubSpot:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, dict[str, Any]]] = {"contacts": {}, "companies": {}}
        self.associations: set[tuple[str, str]] = set()  # (contact_id, company_id)
        self.updates: list[tuple[str, str, dict[str, Any]]] = []
        self.creates: list[tuple[str, dict[str, Any]]] = []
        self._ids = itertools.count(1000)
        self.industry_options = [
            {"label": "Computer Software", "value": "COMPUTER_SOFTWARE"},
            {"label": "Research", "value": "RESEARCH"},
        ]

    # -- helpers for tests
    def add(self, object_type: str, properties: dict[str, Any]) -> str:
        object_id = str(next(self._ids))
        self.objects[object_type][object_id] = dict(properties)
        return object_id

    def props(self, object_type: str, object_id: str) -> dict[str, Any]:
        return self.objects[object_type][object_id]

    # -- HubSpotClient API used by the service
    async def get_object(
        self, object_type: str, object_id: str | int, properties: list[str]
    ) -> dict[str, Any] | None:
        record = self.objects[object_type].get(str(object_id))
        if record is None:
            return None
        return {"id": str(object_id), "properties": {p: record.get(p) for p in properties}}

    async def create_object(self, object_type: str, properties: dict[str, Any]) -> dict[str, Any]:
        clean = {k: v for k, v in properties.items() if v is not None}
        self.creates.append((object_type, clean))
        object_id = self.add(object_type, clean)
        return {"id": object_id, "properties": clean}

    async def update_object(
        self, object_type: str, object_id: str | int, properties: dict[str, Any]
    ) -> dict[str, Any]:
        clean = {k: v for k, v in properties.items() if v is not None}
        self.updates.append((object_type, str(object_id), clean))
        self.objects[object_type][str(object_id)].update(clean)
        return {"id": str(object_id)}

    async def search_objects(
        self,
        object_type: str,
        filters: list[dict[str, Any]],
        properties: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        out = []
        for object_id, record in self.objects[object_type].items():
            ok = True
            for f in filters:
                value = record.get(f["propertyName"])
                if f["propertyName"] == "hs_email_domain":
                    value = (record.get("email") or "").rsplit("@", 1)[-1] or None
                if value != f["value"]:
                    ok = False
            if ok:
                out.append({"id": object_id, "properties": {p: record.get(p) for p in properties}})
        return out[:limit]

    async def find_one(
        self, object_type: str, property_name: str, value: str, properties: list[str]
    ) -> dict[str, Any] | None:
        found = await self.search_objects(
            object_type, [{"propertyName": property_name, "value": value}], properties, 1
        )
        return found[0] if found else None

    async def associated_ids(self, from_type: str, object_id: str | int, to_type: str) -> list[str]:
        if from_type == "contacts":
            return [co for (c, co) in self.associations if c == str(object_id)]
        return [c for (c, co) in self.associations if co == str(object_id)]

    async def associate_default(
        self, from_type: str, from_id: str | int, to_type: str, to_id: str | int
    ) -> None:
        pair = (str(from_id), str(to_id)) if from_type == "contacts" else (str(to_id), str(from_id))
        self.associations.add(pair)

    async def get_property(self, object_type: str, name: str) -> dict[str, Any] | None:
        if object_type == "companies" and name == "industry":
            return {"name": "industry", "options": self.industry_options}
        specs = {s.name: s for s in PROPERTIES_BY_OBJECT[object_type]}
        return specs[name].definition() if name in specs else None
