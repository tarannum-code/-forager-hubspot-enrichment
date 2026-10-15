"""Shared test fixtures.

Safety net: every test runs inside a respx router with `assert_all_mocked=True`, so any
HTTP request that isn't explicitly mocked raises instead of reaching the real Forager
or HubSpot APIs. Tests can never spend credits.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app import http_utils
from app.forager.cache import ResponseCache
from app.forager.client import ForagerClient
from app.forager.credits import CreditBudget
from app.hubspot.client import HubSpotClient

FIXTURES = Path(__file__).parent / "fixtures"
FORAGER_BASE = "https://forager.test"
HUBSPOT_BASE = "https://hubspot.test"
ACCOUNT_ID = 42


def load_fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_utils._WaitRetryAfterOrBackoff, "__call__", lambda self, state: 0)


@pytest.fixture(autouse=True)
def mock_router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


@pytest.fixture
def budget() -> CreditBudget:
    return CreditBudget(limit=100, label="test")


@pytest.fixture
def cache(tmp_path: Path) -> ResponseCache:
    return ResponseCache(tmp_path / "cache")


@pytest.fixture
async def forager(budget: CreditBudget, cache: ResponseCache) -> AsyncIterator[ForagerClient]:
    client = ForagerClient(
        api_key="test-key",
        account_id=ACCOUNT_ID,
        budget=budget,
        cache=cache,
        base_url=FORAGER_BASE,
        http=httpx.AsyncClient(base_url=FORAGER_BASE),
        work_email_poll_interval=0,
        work_email_poll_timeout=5,
    )
    yield client
    await client._http.aclose()


@pytest.fixture
async def hubspot() -> AsyncIterator[HubSpotClient]:
    client = HubSpotClient(
        access_token="test-token",
        base_url=HUBSPOT_BASE,
        http=httpx.AsyncClient(base_url=HUBSPOT_BASE),
    )
    yield client
    await client._http.aclose()


def forager_url(path: str, *, v2: bool = False) -> str:
    prefix = f"/api/v2/{ACCOUNT_ID}" if v2 else f"/api/{ACCOUNT_ID}"
    return f"{FORAGER_BASE}{prefix}/datastorage/{path}"
