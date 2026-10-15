from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.http_utils import ApiError, RetryableApiError, is_transient
from app.main import EventProcessor, create_app
from app.webhooks.events import CreationEvent, parse_events
from app.webhooks.signature import SignatureError, compute_signature, decode_uri, verify_v3
from app.webhooks.worker import JobOutcome, WebhookWorker

SECRET = "test-client-secret"
PUBLIC = "https://enrich.example.com"


def _now_ms() -> str:
    return str(int(time.time() * 1000))


# ----------------------------------------------------------------------- signature


def test_signature_matches_independent_hmac() -> None:
    ts, body = "1700000000000", b'[{"objectId":1}]'
    uri = "https://enrich.example.com/webhooks/hubspot"
    expected = base64.b64encode(
        hmac.new(
            SECRET.encode(), b"POST" + uri.encode() + body + ts.encode(), hashlib.sha256
        ).digest()
    ).decode()
    assert compute_signature(SECRET, "post", uri, body, ts) == expected


def test_decode_uri_only_decodes_listed_characters() -> None:
    assert (
        decode_uri("https://x.io/a%3Ab%2Fc?q=%40me%20you#frag") == "https://x.io/a:b/c?q=@me%20you"
    )


def test_verify_accepts_valid_and_rejects_tampering() -> None:
    ts, body, uri = _now_ms(), b"[]", f"{PUBLIC}/webhooks/hubspot"
    sig = compute_signature(SECRET, "POST", uri, body, ts)
    verify_v3(secret=SECRET, method="POST", uri=uri, body=body, signature=sig, timestamp=ts)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_v3(secret=SECRET, method="POST", uri=uri, body=b"[{}]", signature=sig, timestamp=ts)
    with pytest.raises(SignatureError, match="mismatch"):
        verify_v3(secret="other", method="POST", uri=uri, body=body, signature=sig, timestamp=ts)
    with pytest.raises(SignatureError, match="missing"):
        verify_v3(secret=SECRET, method="POST", uri=uri, body=body, signature=None, timestamp=ts)


def test_verify_rejects_replayed_requests() -> None:
    old = str(int(time.time() * 1000) - 6 * 60 * 1000)
    uri = f"{PUBLIC}/webhooks/hubspot"
    sig = compute_signature(SECRET, "POST", uri, b"[]", old)
    with pytest.raises(SignatureError, match="window"):
        verify_v3(secret=SECRET, method="POST", uri=uri, body=b"[]", signature=sig, timestamp=old)


# ----------------------------------------------------------------------- events


def test_parse_events_both_formats() -> None:
    payload = [
        {"subscriptionType": "company.creation", "objectId": 11, "eventId": 1, "attemptNumber": 0},
        {"eventType": "contact.creation", "objectId": 22, "eventId": 2},
        {"subscriptionType": "object.creation", "objectTypeId": "0-1", "objectId": 33},
        {"subscriptionType": "object.creation", "objectTypeId": "0-3", "objectId": 44},  # deal
        {"subscriptionType": "contact.propertyChange", "objectId": 55},
        "garbage",
    ]
    events = parse_events(payload)
    assert [(e.kind, e.object_id) for e in events] == [
        ("company", "11"),
        ("contact", "22"),
        ("contact", "33"),
    ]
    assert events[0].dedupe_key == "company:11"


# ----------------------------------------------------------------------- HTTP endpoint


@pytest.fixture
def processed(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def fake_call(self: EventProcessor, event: object) -> JobOutcome:
        calls.append(event.dedupe_key)  # type: ignore[attr-defined]
        return JobOutcome("enriched", 6, "ok")

    monkeypatch.setattr(EventProcessor, "__call__", fake_call)
    return calls


@pytest.fixture
def client(processed: list[str]) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        hubspot_access_token="token",
        hubspot_client_secret=SECRET,
        forager_api_key="key",
        forager_account_id=42,
        public_base_url=PUBLIC,
        webhook_settle_delay_seconds=0,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _post(client: TestClient, payload: object, *, secret: str = SECRET) -> object:
    body = json.dumps(payload).encode()
    ts = _now_ms()
    sig = compute_signature(secret, "POST", f"{PUBLIC}/webhooks/hubspot", body, ts)
    return client.post(
        "/webhooks/hubspot",
        content=body,
        headers={
            "X-HubSpot-Signature-v3": sig,
            "X-HubSpot-Request-Timestamp": ts,
            "Content-Type": "application/json",
        },
    )


def test_health(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["webhooks"]["signature_verification"] is True
    assert body["webhooks"]["worker_running"] is True
    assert body["webhooks"]["public_base_url"] == PUBLIC
    assert body["storage"]["writable"] is True


def test_unsigned_request_is_rejected(client: TestClient, processed: list[str]) -> None:
    response = client.post("/webhooks/hubspot", json=[{"subscriptionType": "contact.creation"}])
    assert response.status_code == 401
    bad = _post(client, [{"subscriptionType": "contact.creation", "objectId": 1}], secret="nope")
    assert bad.status_code == 401  # type: ignore[attr-defined]
    assert processed == []


def test_valid_webhook_is_queued_processed_and_deduplicated(
    client: TestClient, processed: list[str]
) -> None:
    event = {"subscriptionType": "contact.creation", "objectId": 77, "eventId": 9}
    first = _post(client, [event])
    assert first.status_code == 200 and first.json() == {"received": 1, "queued": 1}  # type: ignore[attr-defined]
    # HubSpot retry / duplicate delivery of the same creation:
    second = _post(client, [event | {"attemptNumber": 1, "eventId": 10}])
    assert second.json() == {"received": 1, "queued": 0}  # type: ignore[attr-defined]

    deadline = time.time() + 5
    while time.time() < deadline and not processed:
        time.sleep(0.05)
    assert processed == ["contact:77"]
    jobs = client.get("/jobs").json()["jobs"]
    assert jobs[0]["key"] == "contact:77"


def test_irrelevant_events_are_acknowledged(client: TestClient, processed: list[str]) -> None:
    response = _post(client, [{"subscriptionType": "deal.creation", "objectId": 5}])
    assert response.status_code == 200 and response.json() == {"received": 0, "queued": 0}  # type: ignore[attr-defined]


# ----------------------------------------------------------------------- worker retries


async def _drain(worker: WebhookWorker, calls: list[CreationEvent], expected: int) -> None:
    for _ in range(200):
        if len(calls) >= expected and worker.queue_size == 0:
            break
        await asyncio.sleep(0.01)
    await worker.stop(timeout=1)


async def test_worker_retries_transient_failures_as_resume() -> None:
    calls: list[CreationEvent] = []

    async def processor(event: CreationEvent) -> JobOutcome:
        calls.append(event)
        if len(calls) == 1:
            raise RetryableApiError("HubSpot", 503, "unavailable")
        return JobOutcome("enriched")

    worker = WebhookWorker(processor, settle_delay=0, retry_delays=(0,))
    worker.start()
    worker.submit(parse_events([{"subscriptionType": "contact.creation", "objectId": 7}])[0])
    await _drain(worker, calls, 2)

    assert [e.retry for e in calls] == [0, 1]
    job = worker.recent()[0]
    assert (job["state"], job["result"], job["attempts"]) == ("done", "enriched", 2)


async def test_worker_does_not_retry_permanent_failures() -> None:
    calls: list[CreationEvent] = []

    async def processor(event: CreationEvent) -> JobOutcome:
        calls.append(event)
        raise ApiError("HubSpot", 400, "bad request")

    worker = WebhookWorker(processor, settle_delay=0, retry_delays=(0, 0))
    worker.start()
    event = parse_events([{"subscriptionType": "company.creation", "objectId": 9}])[0]
    worker.submit(event)
    await _drain(worker, calls, 1)

    assert len(calls) == 1 and worker.recent()[0]["state"] == "failed"
    assert not is_transient(ApiError("Forager", 404, "x"))
    assert is_transient(ApiError("Forager", 500, "x"))


def test_daily_webhook_credit_cap_limits_event_budget(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        forager_api_key="key",
        forager_account_id=42,
        forager_cache_dir=tmp_path / "forager",
        webhook_max_credits_per_event=25,
        webhook_max_credits_per_day=30,
    )
    now = datetime.now(UTC).isoformat()
    (tmp_path / "credit_ledger.jsonl").write_text(
        json.dumps({"ts": now, "run": "webhook company:1", "credits": 21})
        + "\n"
        + json.dumps({"ts": now, "run": "cli demo-openai", "credits": 40})
        + "\n"
    )
    processor = EventProcessor(settings, None, httpx.AsyncClient())  # type: ignore[arg-type]
    assert processor.credits_spent_today() == 21
    assert processor._forager("webhook contact:2").budget.limit == 9  # 30 - 21, not 25


def _app_with(**overrides: object) -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        hubspot_client_secret=SECRET,
        public_base_url=PUBLIC,
        **overrides,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings))


def test_jobs_requires_admin_token_when_configured() -> None:
    with _app_with(admin_token="s3cret") as client:
        assert client.get("/jobs").status_code == 401
        bad = client.get("/jobs", headers={"Authorization": "Bearer nope"})
        assert bad.status_code == 401
        ok = client.get("/jobs", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and ok.json() == {"jobs": []}


def test_jobs_disabled_in_production_without_token() -> None:
    with _app_with(environment="production") as client:
        assert client.get("/jobs").status_code == 403
    with _app_with(environment="development") as client:
        assert client.get("/jobs").status_code == 200
