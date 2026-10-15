"""Credit accounting: price list, a per-run budget, and an append-only spend ledger.

Every paid Forager call must `reserve()` its worst-case cost before the request is sent;
if that would exceed the run's limit the call is refused *before* any money is spent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Source: https://docs.forager.ai/api-overview/credit-pricing (checked 2026-10-15).
# Values are the worst case; `charged_*` rules below refine what is actually billed.
CREDIT_COSTS: dict[str, int] = {
    "organization_search": 1,
    "person_role_search": 1,  # per page
    # The pricing table's "Person role details lookup" (1) links to the totals endpoint,
    # so we treat totals as paid rather than assume it is free.
    "person_role_search_totals": 1,
    "person_detail_lookup": 1,
    "person_reverse_email": 5,
    "work_email": 5,  # charged only when an address is found (per API docs)
    "personal_email": 5,  # free when the result set is empty (per API docs)
    "phone_numbers": 15,  # charged only when numbers are returned (verified live)
}
FREE_ENDPOINTS = frozenset({"autocomplete_organizations", "current_user", "balance_logs"})


class CreditBudgetExceeded(RuntimeError):
    def __init__(self, endpoint: str, cost: int, remaining: int) -> None:
        super().__init__(
            f"Forager call '{endpoint}' needs up to {cost} credits but only {remaining} "
            f"remain in this run's budget (raise --max-credits for the CLI, or "
            f"WEBHOOK_MAX_CREDITS_PER_EVENT / _PER_DAY for webhooks)."
        )
        self.endpoint = endpoint
        self.cost = cost
        self.remaining = remaining


@dataclass
class CreditBudget:
    """Spending limit for one run (a CLI command, or one webhook event)."""

    limit: int
    label: str = "run"
    ledger_path: Path | None = None
    spent: int = 0
    reserved: int = 0
    calls: list[dict[str, object]] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return self.limit - self.spent - self.reserved

    def can_afford(self, cost: int) -> bool:
        return cost <= self.remaining

    def reserve(self, endpoint: str, cost: int) -> None:
        if cost > self.remaining:
            raise CreditBudgetExceeded(endpoint, cost, self.remaining)
        self.reserved += cost

    def settle(self, endpoint: str, reserved: int, charged: int, note: str = "") -> None:
        """Convert a reservation into actual spend (charged may be 0, e.g. a miss)."""
        self.reserved -= reserved
        self.spent += charged
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "run": self.label,
            "endpoint": endpoint,
            "credits": charged,
            "note": note,
        }
        self.calls.append(record)
        logger.info(
            "Forager credits spent",
            extra={"endpoint": endpoint, "credits": charged, "run_total": self.spent},
        )
        if self.ledger_path:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")

    def release(self, reserved: int) -> None:
        """The call failed before being charged."""
        self.reserved -= reserved


def credits_spent_since(ledger_path: Path, since: datetime, *, run_prefix: str = "") -> int:
    """Credits recorded in the ledger at or after `since` (optionally for runs whose label
    starts with `run_prefix`). Unreadable lines are ignored, a missing ledger counts as 0."""
    total = 0
    try:
        with ledger_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                    ts = datetime.fromisoformat(str(record["ts"]))
                    if ts >= since and str(record.get("run", "")).startswith(run_prefix):
                        total += int(record.get("credits") or 0)
                except (ValueError, KeyError, TypeError):
                    continue
    except FileNotFoundError:
        return 0
    return total
