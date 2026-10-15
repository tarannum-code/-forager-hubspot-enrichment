from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.forager.cache import ResponseCache
from app.forager.credits import CreditBudget, CreditBudgetExceeded, credits_spent_since


def test_budget_reserve_settle_release(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    budget = CreditBudget(limit=20, label="t", ledger_path=ledger)
    budget.reserve("phone_numbers", 15)
    assert budget.remaining == 5
    with pytest.raises(CreditBudgetExceeded):
        budget.reserve("work_email", 6)
    budget.settle("phone_numbers", 15, 15)
    budget.reserve("work_email", 5)
    budget.release(5)
    assert (budget.spent, budget.reserved, budget.remaining) == (15, 0, 5)
    assert json.loads(ledger.read_text().splitlines()[0])["credits"] == 15


def test_cache_roundtrip_and_key_order_independence(tmp_path: Path) -> None:
    cache = ResponseCache(tmp_path)
    cache.set("organization_search", {"page": 1, "domains": ["a.com"]}, {"ok": True})
    assert cache.get("organization_search", {"domains": ["a.com"], "page": 1}) == {"ok": True}
    assert ResponseCache.is_missing(cache.get("organization_search", {"domains": ["b.com"]}))
    assert cache.stats() == {"organization_search": 1}


def test_disabled_cache(tmp_path: Path) -> None:
    cache = ResponseCache.disabled()
    cache.set("x", {}, 1)
    assert ResponseCache.is_missing(cache.get("x", {}))


def test_credits_spent_since_filters_by_time_and_run(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    rows = [
        {"ts": "2026-10-14T23:59:00+00:00", "run": "webhook company:1", "credits": 7},
        {"ts": "2026-10-15T08:00:00+00:00", "run": "webhook company:2", "credits": 5},
        {"ts": "2026-10-15T09:00:00+00:00", "run": "cli demo-openai", "credits": 20},
        {"ts": "2026-10-15T10:00:00+00:00", "run": "webhook contact:3", "credits": 1},
    ]
    ledger.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
    since = datetime(2026, 10, 15, tzinfo=UTC)
    assert credits_spent_since(ledger, since, run_prefix="webhook ") == 6
    assert credits_spent_since(ledger, since) == 26
    assert credits_spent_since(tmp_path / "missing.jsonl", since) == 0
