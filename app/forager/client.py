"""Async Forager API client.

Endpoints, request bodies and response shapes follow the official OpenAPI spec at
https://docs.forager.ai/_bundle/openapi.yaml. Auth: `X-API-KEY` header; every data
endpoint is scoped by `/api/{account_id}/...`.

Every paid call goes through `_paid_call`, which:
  1. serves the response from the disk cache when possible (0 credits),
  2. reserves the worst-case cost against the run's `CreditBudget` (refusing if over),
  3. records what was actually charged and caches the answer (including "not found").
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self

import httpx

from app.config import Settings
from app.forager.cache import ResponseCache
from app.forager.credits import CREDIT_COSTS, CreditBudget
from app.forager.models import (
    AutocompleteItem,
    EmailAddress,
    OrganizationSearchResponse,
    PersonDetail,
    PersonRoleSearchResponse,
    PersonRoleSearchTotals,
    PhoneNumber,
    WorkEmailJob,
)
from app.http_utils import ApiError, parse_retry_after, raise_for_response, retry_policy

logger = logging.getLogger(__name__)

SERVICE = "Forager"
_NOT_FOUND = {"__not_found__": True}
_PENDING_KEY = "__pending_work_email_job__"


class ForagerClient:
    def __init__(
        self,
        *,
        api_key: str,
        account_id: int,
        budget: CreditBudget,
        cache: ResponseCache | None = None,
        base_url: str = "https://api-v2.forager.ai",
        timeout: float = 30.0,
        http: httpx.AsyncClient | None = None,
        work_email_poll_timeout: float = 180.0,
        work_email_poll_interval: float = 3.0,
    ) -> None:
        self.account_id = account_id
        self.budget = budget
        self.cache = cache or ResponseCache.disabled()
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._headers = {"X-API-KEY": api_key, "Accept": "application/json"}
        self._poll_timeout = work_email_poll_timeout
        self._poll_interval = work_email_poll_interval

    @classmethod
    def from_settings(
        cls, settings: Settings, budget: CreditBudget, http: httpx.AsyncClient | None = None
    ) -> Self:
        api_key, account_id = settings.require_forager()
        return cls(
            api_key=api_key,
            account_id=account_id,
            budget=budget,
            cache=ResponseCache(settings.forager_cache_dir, settings.forager_cache_enabled),
            base_url=settings.forager_base_url,
            timeout=settings.forager_timeout_seconds,
            http=http,
            work_email_poll_timeout=settings.forager_work_email_poll_timeout_seconds,
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

    def _ds(self, path: str, *, v2: bool = False) -> str:
        prefix = f"/api/v2/{self.account_id}" if v2 else f"/api/{self.account_id}"
        return f"{prefix}/datastorage/{path}"

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> httpx.Response:
        async for attempt in retry_policy(idempotent=idempotent):
            with attempt:
                response = await self._http.request(
                    method, path, json=json, params=params, headers=self._headers
                )
                raise_for_response(SERVICE, response)
        return response

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        if not response.content or not response.content.strip():
            return None
        return response.json()

    async def _paid_call(
        self,
        endpoint: str,
        path: str,
        body: dict[str, Any],
        *,
        charged: Callable[[Any], int] | None = None,
    ) -> Any:
        """POST a credit-consuming request. Returns parsed JSON, or None for "not found"."""
        cached = self.cache.get(endpoint, body)
        if not ResponseCache.is_missing(cached):
            return None if cached == _NOT_FOUND else cached

        cost = CREDIT_COSTS[endpoint]
        self.budget.reserve(endpoint, cost)
        try:
            response = await self._send("POST", path, json=body, idempotent=False)
        except ApiError as exc:
            if exc.status_code == 404:
                # A miss: assume uncharged (Forager returns 404 when nothing matches).
                self.budget.settle(endpoint, cost, 0, note="not found (404)")
                self.cache.set(endpoint, body, _NOT_FOUND)
                return None
            self.budget.release(cost)
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            self.budget.release(cost)  # never reached the server
            raise
        except BaseException:
            # Network failure after sending: we can't know if it was billed. Count it.
            self.budget.settle(endpoint, cost, cost, note="unknown outcome (transport error)")
            raise

        data = self._json(response)
        actual = charged(data) if charged else cost
        self.budget.settle(endpoint, cost, actual)
        self.cache.set(endpoint, body, data if data is not None else _NOT_FOUND)
        return data

    @staticmethod
    def person_ref(
        *, person_id: int | None = None, linkedin_slug: str | None = None
    ) -> dict[str, Any]:
        """`PersonInfoRequest`. Prefer the LinkedIn slug so cache keys stay stable."""
        if linkedin_slug:
            return {"linkedin_public_identifier": linkedin_slug}
        if person_id:
            return {"person_id": person_id}
        raise ValueError("A Forager person_id or LinkedIn public identifier is required.")

    # ------------------------------------------------------------------ free endpoints

    async def current_user(self) -> dict[str, Any]:
        """GET /api/users/current/ — user, accounts and credit balance (free)."""
        response = await self._send("GET", "/api/users/current/", idempotent=True)
        return dict(self._json(response) or {})

    async def credits_balance(self) -> float | None:
        user = await self.current_user()
        for account in user.get("accounts") or []:
            if account.get("id") == self.account_id:
                balance = (account.get("subscription") or {}).get("credits_balance")
                return float(balance) if balance is not None else None
        return None

    async def credit_change_logs(self, page: int = 1) -> dict[str, Any]:
        response = await self._send(
            "GET",
            f"/api/{self.account_id}/subscriptions/balance_change_logs/",
            params={"page": page},
            idempotent=True,
        )
        return dict(self._json(response) or {})

    async def autocomplete_organizations(self, query: str) -> list[AutocompleteItem]:
        """Resolve an organization name to Forager IDs/domains. Not in the price list."""
        endpoint = "autocomplete_organizations"
        body = {"q": query}
        cached = self.cache.get(endpoint, body)
        if ResponseCache.is_missing(cached):
            response = await self._send(
                "GET", self._ds("autocomplete/organizations/"), params=body, idempotent=True
            )
            cached = self._json(response) or {}
            self.cache.set(endpoint, body, cached)
        return [AutocompleteItem.model_validate(r) for r in (cached or {}).get("results", [])]

    # ------------------------------------------------------------------ organizations

    async def search_organizations(
        self,
        *,
        domains: list[str] | None = None,
        linkedin_public_identifiers: list[str] | None = None,
        organization_ids: list[int] | None = None,
        page: int = 1,
    ) -> OrganizationSearchResponse:
        """POST organization_search/ — 1 credit."""
        body: dict[str, Any] = {"page": page}
        if domains:
            body["domains"] = domains
        if linkedin_public_identifiers:
            body["linkedin_public_identifiers"] = linkedin_public_identifiers
        if organization_ids:
            body["organization_ids"] = organization_ids
        if len(body) == 1:
            raise ValueError("Provide domains, LinkedIn identifiers or organization IDs.")
        data = await self._paid_call("organization_search", self._ds("organization_search/"), body)
        return OrganizationSearchResponse.model_validate(data or {})

    # ------------------------------------------------------------------ people

    @staticmethod
    def person_role_query(
        *,
        role_title: str | None = None,
        organization_domains: list[str] | None = None,
        organization_ids: list[int] | None = None,
        organization_linkedin_ids: list[str] | None = None,
        person_linkedin_ids: list[str] | None = None,
        person_name: str | None = None,
        role_is_current: bool = True,
        page: int = 1,
    ) -> dict[str, Any]:
        """Build an `OrganizationPersonRoleSearch` body (only non-empty filters)."""
        body: dict[str, Any] = {"page": page, "role_is_current": role_is_current}
        optional = {
            "role_title": role_title,
            "organization_domains": organization_domains,
            "organizations": organization_ids,
            "organization_linkedin_public_identifiers": organization_linkedin_ids,
            "person_linkedin_public_identifiers": person_linkedin_ids,
            "person_name": person_name,
        }
        body.update({k: v for k, v in optional.items() if v})
        return body

    async def search_person_roles(self, query: dict[str, Any]) -> PersonRoleSearchResponse:
        """POST person_role_search/ — 1 credit per page."""
        data = await self._paid_call("person_role_search", self._ds("person_role_search/"), query)
        return PersonRoleSearchResponse.model_validate(data or {})

    async def person_role_totals(self, query: dict[str, Any]) -> PersonRoleSearchTotals:
        """POST person_role_search/totals/ — treated as 1 credit (see credits.py)."""
        body = {k: v for k, v in query.items() if k != "page"}
        data = await self._paid_call(
            "person_role_search_totals", self._ds("person_role_search/totals/"), body
        )
        return PersonRoleSearchTotals.model_validate(data or {})

    async def person_detail(
        self, *, person_id: int | None = None, linkedin_slug: str | None = None
    ) -> PersonDetail | None:
        """POST person_detail_lookup/ — 1 credit."""
        body = self.person_ref(person_id=person_id, linkedin_slug=linkedin_slug)
        data = await self._paid_call(
            "person_detail_lookup", self._ds("person_detail_lookup/"), body
        )
        return PersonDetail.model_validate(data) if data else None

    async def person_by_email(self, email: str) -> PersonDetail | None:
        """POST person_detail_reverse_lookup/by_email/ — 5 credits."""
        data = await self._paid_call(
            "person_reverse_email",
            self._ds("person_detail_reverse_lookup/by_email/"),
            {"email": email},
        )
        return PersonDetail.model_validate(data) if data else None

    # ------------------------------------------------------------------ contact data

    async def phone_numbers(
        self, *, person_id: int | None = None, linkedin_slug: str | None = None
    ) -> list[PhoneNumber]:
        """POST person_contacts_lookup/phone_numbers/ — 15 credits."""
        body = self.person_ref(person_id=person_id, linkedin_slug=linkedin_slug)
        data = await self._paid_call(
            "phone_numbers", self._ds("person_contacts_lookup/phone_numbers/"), body
        )
        return [PhoneNumber.model_validate(p) for p in data or [] if p.get("phone_number")]

    async def personal_emails(
        self, *, person_id: int | None = None, linkedin_slug: str | None = None
    ) -> list[EmailAddress]:
        """POST person_contacts_lookup/personal_emails/ — 5 credits, free when empty."""
        body = self.person_ref(person_id=person_id, linkedin_slug=linkedin_slug)
        data = await self._paid_call(
            "personal_email",
            self._ds("person_contacts_lookup/personal_emails/"),
            body,
            charged=lambda d: CREDIT_COSTS["personal_email"] if d else 0,
        )
        return [EmailAddress.model_validate(e) for e in data or [] if e.get("email")]

    async def work_email(
        self,
        *,
        linkedin_slug: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        domain: str | None = None,
    ) -> WorkEmailJob | None:
        """Async work email lookup (v2): submit, then poll until terminal.

        5 credits, charged only when an address is found; polling is free. If polling
        times out, the job id is cached so the next run resumes polling instead of paying
        for a new submission.
        """
        if linkedin_slug:
            body: dict[str, Any] = {"linkedin_public_identifier": linkedin_slug}
        elif first_name and last_name and domain:
            body = {"first_name": first_name, "last_name": last_name, "domain": domain}
        else:
            raise ValueError("Work email lookup needs a LinkedIn slug, or first+last name+domain.")

        endpoint = "work_email"
        cost = CREDIT_COSTS[endpoint]
        cached = self.cache.get(endpoint, body)
        if not ResponseCache.is_missing(cached):
            if isinstance(cached, dict) and _PENDING_KEY in cached:
                job = await self._poll_work_email(cached[_PENDING_KEY])
                return self._finish_work_email(body, job, reserved=0)
            return None if cached == _NOT_FOUND else WorkEmailJob.model_validate(cached)

        self.budget.reserve(endpoint, cost)
        try:
            response = await self._send(
                "POST",
                self._ds("person_contacts_lookup/work_emails/", v2=True),
                json=body,
                idempotent=False,
            )
        except ApiError:
            self.budget.release(cost)
            raise
        job_id = str((self._json(response) or {}).get("job_id"))
        self.cache.set(endpoint, body, {_PENDING_KEY: job_id})
        try:
            job = await self._poll_work_email(job_id)
        except TimeoutError:
            # Keep the reservation as spent (conservative) — the job may still find an address.
            self.budget.settle(endpoint, cost, cost, note=f"poll timeout, job {job_id} pending")
            raise
        return self._finish_work_email(body, job, reserved=cost)

    def _finish_work_email(
        self, body: dict[str, Any], job: WorkEmailJob, *, reserved: int
    ) -> WorkEmailJob | None:
        charged = CREDIT_COSTS["work_email"] if job.found else 0
        if reserved:
            self.budget.settle("work_email", reserved, charged, note=job.result or job.status)
        self.cache.set("work_email", body, job.model_dump() if job.found else _NOT_FOUND)
        if job.error_code:
            logger.info("Work email lookup ended", extra={"error_code": job.error_code})
        return job if job.found else None

    async def _poll_work_email(self, job_id: str) -> WorkEmailJob:
        deadline = time.monotonic() + self._poll_timeout
        path = self._ds(f"person_contacts_lookup/work_emails/{job_id}/", v2=True)
        while True:
            response = await self._send("GET", path, idempotent=True)
            job = WorkEmailJob.model_validate(self._json(response))
            if job.is_terminal:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Work email job {job_id} still '{job.status}' after polling.")
            delay = parse_retry_after(response.headers.get("Retry-After")) or self._poll_interval
            await asyncio.sleep(min(delay, 15.0))
