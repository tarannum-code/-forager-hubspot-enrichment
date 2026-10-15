"""Parse HubSpot webhook payloads into the jobs this service cares about.

HubSpot POSTs a JSON array of up to 100 events. Two formats exist:
* classic ("legacyCrmObjects"): {"subscriptionType": "contact.creation", "objectId": 123, ...}
  (some examples use the key `eventType` instead of `subscriptionType`);
* generic ("crmObjects"): {"subscriptionType": "object.creation", "objectTypeId": "0-1", ...}.
Both are accepted. Anything else is ignored (and still acknowledged with 200).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ObjectKind = Literal["contact", "company"]

_CLASSIC = {"contact.creation": "contact", "company.creation": "company"}
_OBJECT_TYPE_IDS = {"0-1": "contact", "0-2": "company", "contact": "contact", "company": "company"}


@dataclass(frozen=True)
class CreationEvent:
    kind: ObjectKind
    object_id: str
    event_id: str | None
    portal_id: str | None
    attempt: int

    @property
    def dedupe_key(self) -> str:
        # A record is created once, so (kind, id) identifies the work even when HubSpot
        # retries with a new attempt number or (rarely) a non-unique eventId.
        return f"{self.kind}:{self.object_id}"


def parse_events(payload: Any) -> list[CreationEvent]:
    items = payload if isinstance(payload, list) else [payload]
    events: list[CreationEvent] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        subscription = str(item.get("subscriptionType") or item.get("eventType") or "")
        kind = _CLASSIC.get(subscription)
        if kind is None and subscription == "object.creation":
            kind = _OBJECT_TYPE_IDS.get(str(item.get("objectTypeId") or item.get("objectType")))
        object_id = item.get("objectId")
        if kind is None or object_id in (None, ""):
            continue
        events.append(
            CreationEvent(
                kind=kind,  # type: ignore[arg-type]
                object_id=str(object_id),
                event_id=str(item["eventId"]) if item.get("eventId") is not None else None,
                portal_id=str(item["portalId"]) if item.get("portalId") is not None else None,
                attempt=int(item.get("attemptNumber") or 0),
            )
        )
    return events
