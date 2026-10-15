"""FastAPI service: HubSpot webhooks in, Forager enrichment out.

Run locally:  uvicorn app.main:app --reload
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.enrichment.service import ContactLookupOptions, EnrichmentService
from app.enrichment.title_filter import TitleFilter
from app.forager.cache import ResponseCache
from app.forager.client import ForagerClient
from app.forager.credits import CreditBudget, CreditBudgetExceeded
from app.hubspot.client import HubSpotClient
from app.logging_config import configure_logging
from app.webhooks.events import CreationEvent, parse_events
from app.webhooks.signature import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureError, verify_v3
from app.webhooks.worker import JobOutcome, WebhookWorker

logger = logging.getLogger(__name__)


class EventProcessor:
    """Turns one webhook event into one enrichment run with its own credit budget."""

    def __init__(
        self, settings: Settings, hubspot: HubSpotClient, forager_http: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self.hubspot = hubspot
        self.forager_http = forager_http
        self.cache = ResponseCache(settings.forager_cache_dir, settings.forager_cache_enabled)
        self.options = ContactLookupOptions(
            work_email=settings.webhook_lookup_work_email,
            personal_email=settings.webhook_lookup_personal_email,
            phone=settings.webhook_lookup_phone,
            phone_requires_email=True,
        )
        self.title_filter = TitleFilter.from_lists(
            settings.contact_titles, settings.contact_title_excludes
        )

    def _forager(self, label: str) -> ForagerClient:
        api_key, account_id = self.settings.require_forager()
        budget = CreditBudget(
            limit=self.settings.webhook_max_credits_per_event,
            label=label,
            ledger_path=self.settings.forager_cache_dir.parent / "credit_ledger.jsonl",
        )
        return ForagerClient(
            api_key=api_key,
            account_id=account_id,
            budget=budget,
            cache=self.cache,
            http=self.forager_http,
            work_email_poll_timeout=self.settings.forager_work_email_poll_timeout_seconds,
        )

    async def __call__(self, event: CreationEvent) -> JobOutcome:
        forager = self._forager(f"webhook {event.dedupe_key}")
        service = EnrichmentService(forager, self.hubspot, self.settings)
        try:
            if event.kind == "company":
                flow = await service.process_company_created(
                    event.object_id,
                    title_filter=self.title_filter,
                    options=self.options,
                    max_new_contacts=self.settings.webhook_max_new_contacts,
                )
                contacts = ", ".join(f"{c.object_id}:{c.status}" for c in flow.contacts)
                message = flow.company.message or (f"contacts [{contacts}]" if contacts else "")
                if flow.created_contact_ids:
                    message += f" | created {len(flow.created_contact_ids)} new contact(s)"
                return JobOutcome(flow.company.status, forager.budget.spent, message.strip())
            result = await service.process_contact_created(event.object_id, self.options)
            return JobOutcome(result.status, forager.budget.spent, result.message)
        except CreditBudgetExceeded as exc:
            # Records were marked failed with the reason; a later manual run can resume.
            return JobOutcome("budget_exceeded", forager.budget.spent, str(exc))


def _signed_uri(request: Request, settings: Settings) -> str:
    """Rebuild the URL HubSpot signed. Behind Railway's proxy the app sees http://, so we
    prefer PUBLIC_BASE_URL, then X-Forwarded-* headers."""
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if settings.public_base_url:
        return settings.public_base_url + path
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return f"{proto}://{host}{path}"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        forager_http = httpx.AsyncClient(
            base_url=settings.forager_base_url, timeout=settings.forager_timeout_seconds
        )
        hubspot = HubSpotClient.from_settings(settings) if settings.hubspot_access_token else None
        worker = None
        if hubspot is not None:
            processor = EventProcessor(settings, hubspot, forager_http)
            worker = WebhookWorker(
                processor,
                concurrency=settings.webhook_worker_concurrency,
                settle_delay=settings.webhook_settle_delay_seconds,
                dedupe_ttl=settings.webhook_dedupe_ttl_seconds,
            )
            worker.start()
        app.state.worker = worker
        logger.info("Service started", extra={"version": __version__, "env": settings.environment})
        try:
            yield
        finally:
            if worker is not None:
                await worker.stop()
            if hubspot is not None:
                await hubspot.aclose()
            await forager_http.aclose()

    app = FastAPI(
        title="Forager x HubSpot enrichment",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "production" else None,
        redoc_url=None,
    )

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        """Liveness/readiness: no external calls, so it is cheap and never flaky."""
        worker: WebhookWorker | None = request.app.state.worker
        return {
            "status": "ok",
            "version": __version__,
            "environment": settings.environment,
            "webhooks": {
                "enabled": settings.webhook_enrichment_enabled,
                "signature_verification": bool(settings.hubspot_client_secret),
                "worker_running": worker is not None,
                "queue_size": worker.queue_size if worker else 0,
            },
            "config": {
                "forager_configured": bool(
                    settings.forager_api_key and settings.forager_account_id
                ),
                "hubspot_configured": bool(settings.hubspot_access_token),
                "max_credits_per_event": settings.webhook_max_credits_per_event,
                "max_new_contacts": settings.webhook_max_new_contacts,
                "lookup_phone": settings.webhook_lookup_phone,
                "titles": settings.contact_titles,
            },
        }

    @app.get("/jobs")
    async def jobs(request: Request) -> dict[str, Any]:
        """Recent webhook jobs (ids, status, credits only - no personal data)."""
        worker: WebhookWorker | None = request.app.state.worker
        return {"jobs": worker.recent() if worker else []}

    @app.post("/webhooks/hubspot")
    async def hubspot_webhook(request: Request) -> JSONResponse:
        body = await request.body()
        if not settings.hubspot_client_secret:
            logger.error("Webhook rejected: HUBSPOT_CLIENT_SECRET is not configured")
            raise HTTPException(503, "webhook secret not configured")
        try:
            verify_v3(
                secret=settings.hubspot_client_secret.get_secret_value(),
                method=request.method,
                uri=_signed_uri(request, settings),
                body=body,
                signature=request.headers.get(SIGNATURE_HEADER),
                timestamp=request.headers.get(TIMESTAMP_HEADER),
                max_skew_seconds=settings.webhook_max_timestamp_skew_seconds,
            )
        except SignatureError as exc:
            logger.warning("Webhook rejected", extra={"reason": str(exc)})
            raise HTTPException(401, "invalid signature") from exc

        try:
            payload = json.loads(body or b"[]")
        except ValueError as exc:
            raise HTTPException(400, "body is not JSON") from exc

        events = parse_events(payload)
        worker: WebhookWorker | None = request.app.state.worker
        accepted = 0
        if settings.webhook_enrichment_enabled and worker is not None:
            accepted = sum(worker.submit(event) for event in events)
        logger.info(
            "Webhook received",
            extra={
                "events": len(events),
                "queued": accepted,
                "batch": len(payload) if isinstance(payload, list) else 1,
            },
        )
        # Always 200 for authentic requests, so HubSpot doesn't retry events we chose to skip.
        return JSONResponse({"received": len(events), "queued": accepted})

    return app


app = create_app()
