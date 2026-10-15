"""Async HubSpot CRM client (date-versioned APIs, default 2026-09).

Paths verified against developers.hubspot.com/docs/api-reference/latest:
  objects       /crm/objects/{v}/{objectType}[/{id}]  (+ /search)
  associations  /crm/objects/{v}/{from}/{id}/associations/{to}
                PUT /crm/objects/{v}/{from}/{id}/associations/default/{to}/{toId}
  properties    /crm/properties/{v}/{objectType}[/groups]
  account       /account-info/{v}/details

Rate limits: private apps get 100 requests / 10 s on Free/Starter (190 on Pro+), and the
search endpoints allow 5 requests / s per account. We stay under both client-side and
still honour 429 + Retry-After via the shared retry policy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import Settings
from app.http_utils import ApiError, raise_for_response, retry_policy

logger = logging.getLogger(__name__)

SERVICE = "HubSpot"
DEFAULT_API_VERSION = "2026-09"


class SlidingWindowLimiter:
    """Allow at most `max_calls` per `period` seconds (async, in-process)."""

    def __init__(self, max_calls: int, period: float) -> None:
        self.max_calls = max_calls
        self.period = period
        self._calls: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= self.period:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._calls[0]) + 0.01)


class HubSpotClient:
    def __init__(
        self,
        *,
        access_token: str,
        base_url: str = "https://api.hubapi.com",
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
        requests_per_10s: int = 90,
        search_per_second: int = 4,
    ) -> None:
        self.api_version = api_version
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._limiter = SlidingWindowLimiter(requests_per_10s, 10.0)
        self._search_limiter = SlidingWindowLimiter(search_per_second, 1.0)
        self._property_names: dict[str, set[str]] = {}

    @classmethod
    def from_settings(cls, settings: Settings, http: httpx.AsyncClient | None = None) -> Self:
        return cls(
            access_token=settings.require_hubspot(),
            base_url=settings.hubspot_base_url,
            api_version=settings.hubspot_api_version,
            timeout=settings.hubspot_timeout_seconds,
            http=http,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ transport

    def _objects(self, object_type: str) -> str:
        return f"/crm/objects/{self.api_version}/{object_type}"

    def _properties(self, object_type: str) -> str:
        return f"/crm/properties/{self.api_version}/{object_type}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        search: bool = False,
    ) -> Any:
        # HubSpot writes here are idempotent (PATCH/PUT) or guarded by our own markers,
        # so read timeouts are safe to retry — except creates, handled by the caller.
        idempotent = method != "POST" or path.endswith("/search")
        async for attempt in retry_policy(idempotent=idempotent):
            with attempt:
                await self._limiter.acquire()
                if search:
                    await self._search_limiter.acquire()
                response = await self._http.request(
                    method, path, json=json, params=params, headers=self._headers
                )
                raise_for_response(SERVICE, response)
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # ------------------------------------------------------------------ account

    async def account_details(self) -> dict[str, Any]:
        return dict(await self._request("GET", f"/account-info/{self.api_version}/details"))

    # ------------------------------------------------------------------ objects

    async def get_object(
        self,
        object_type: str,
        object_id: str | int,
        properties: list[str],
    ) -> dict[str, Any] | None:
        try:
            return dict(
                await self._request(
                    "GET",
                    f"{self._objects(object_type)}/{object_id}",
                    params={"properties": ",".join(properties), "archived": "false"},
                )
            )
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def create_object(self, object_type: str, properties: dict[str, Any]) -> dict[str, Any]:
        clean = await self.filter_known_properties(object_type, properties)
        return dict(
            await self._request("POST", self._objects(object_type), json={"properties": clean})
        )

    async def update_object(
        self, object_type: str, object_id: str | int, properties: dict[str, Any]
    ) -> dict[str, Any]:
        clean = await self.filter_known_properties(object_type, properties)
        return dict(
            await self._request(
                "PATCH", f"{self._objects(object_type)}/{object_id}", json={"properties": clean}
            )
        )

    async def search_objects(
        self,
        object_type: str,
        filters: list[dict[str, Any]],
        properties: list[str],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        body = {"filterGroups": [{"filters": filters}], "properties": properties, "limit": limit}
        data = await self._request(
            "POST", f"{self._objects(object_type)}/search", json=body, search=True
        )
        return list((data or {}).get("results", []))

    async def find_one(
        self, object_type: str, property_name: str, value: str, properties: list[str]
    ) -> dict[str, Any] | None:
        results = await self.search_objects(
            object_type,
            [{"propertyName": property_name, "operator": "EQ", "value": value}],
            properties,
            limit=1,
        )
        return results[0] if results else None

    # ------------------------------------------------------------------ associations

    async def associated_ids(self, from_type: str, object_id: str | int, to_type: str) -> list[str]:
        ids: list[str] = []
        after: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 500}
            if after:
                params["after"] = after
            data = await self._request(
                "GET",
                f"{self._objects(from_type)}/{object_id}/associations/{to_type}",
                params=params,
            )
            ids.extend(str(r["toObjectId"]) for r in (data or {}).get("results", []))
            after = ((data or {}).get("paging") or {}).get("next", {}).get("after")
            if not after:
                return ids

    async def associate_default(
        self, from_type: str, from_id: str | int, to_type: str, to_id: str | int
    ) -> None:
        await self._request(
            "PUT",
            f"{self._objects(from_type)}/{from_id}/associations/default/{to_type}/{to_id}",
        )

    # ------------------------------------------------------------------ properties

    async def list_properties(self, object_type: str) -> list[dict[str, Any]]:
        data = await self._request("GET", self._properties(object_type))
        return list((data or {}).get("results", []))

    async def property_names(self, object_type: str) -> set[str]:
        if object_type not in self._property_names:
            props = await self.list_properties(object_type)
            self._property_names[object_type] = {
                p["name"]
                for p in props
                if not (p.get("modificationMetadata") or {}).get("readOnlyValue")
            }
        return self._property_names[object_type]

    async def filter_known_properties(
        self, object_type: str, properties: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop None values and properties this portal lacks (HubSpot would reject the call)."""
        known = await self.property_names(object_type)
        clean: dict[str, Any] = {}
        for name, value in properties.items():
            if value is None:  # "" is kept: it is how HubSpot clears a value
                continue
            if name not in known:
                logger.warning(
                    "Skipping unknown/read-only HubSpot property — run `setup-hubspot`?",
                    extra={"object_type": object_type, "property": name},
                )
                continue
            clean[name] = value
        return clean

    def invalidate_property_cache(self) -> None:
        self._property_names.clear()

    async def get_property(self, object_type: str, name: str) -> dict[str, Any] | None:
        try:
            return dict(await self._request("GET", f"{self._properties(object_type)}/{name}"))
        except ApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    async def create_property(self, object_type: str, definition: dict[str, Any]) -> dict[str, Any]:
        return dict(await self._request("POST", self._properties(object_type), json=definition))

    async def update_property(
        self, object_type: str, name: str, definition: dict[str, Any]
    ) -> dict[str, Any]:
        body = {k: v for k, v in definition.items() if k not in {"name", "type"}}
        return dict(
            await self._request("PATCH", f"{self._properties(object_type)}/{name}", json=body)
        )

    async def list_property_groups(self, object_type: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"{self._properties(object_type)}/groups")
        return list((data or {}).get("results", []))

    async def create_property_group(
        self, object_type: str, name: str, label: str, display_order: int = -1
    ) -> dict[str, Any]:
        return dict(
            await self._request(
                "POST",
                f"{self._properties(object_type)}/groups",
                json={"name": name, "label": label, "displayOrder": display_order},
            )
        )
