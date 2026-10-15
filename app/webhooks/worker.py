"""Background job queue for webhook events.

The HTTP handler only verifies and enqueues, so HubSpot gets its 200 well within its
5-second timeout; enrichment (which polls Forager for up to minutes) runs here.

Transient failures (upstream 5xx/429, connection refused) are re-queued after
`retry_delays`; HubSpot won't redeliver an event we already acknowledged with 200.

Duplicate protection, in layers:
1. this queue ignores an event whose (kind, object id) is queued, running, or finished
   within `dedupe_ttl` seconds (HubSpot retries and may batch the same event twice);
2. the service's HubSpot markers (in_progress / enriched) make re-processing a no-op even
   across restarts or multiple replicas.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from app.http_utils import is_transient
from app.webhooks.events import CreationEvent

logger = logging.getLogger(__name__)


@dataclass
class JobOutcome:
    status: str
    credits: int = 0
    message: str = ""


@dataclass
class JobRecord:
    key: str
    kind: str
    object_id: str
    state: str = "queued"  # queued | running | done | retrying | failed
    attempts: int = 0
    result: str = ""
    credits: int = 0
    message: str = ""
    received_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    finished_at: str | None = None


Processor = Callable[[CreationEvent], Awaitable[JobOutcome]]


class WebhookWorker:
    def __init__(
        self,
        processor: Processor,
        *,
        concurrency: int = 1,
        settle_delay: float = 5.0,
        dedupe_ttl: float = 3600.0,
        retry_delays: Sequence[float] = (60.0, 300.0),
        history: int = 100,
    ) -> None:
        self._processor = processor
        self._concurrency = concurrency
        self._settle_delay = settle_delay
        self._dedupe_ttl = dedupe_ttl
        self._retry_delays = tuple(retry_delays)
        self._retry_tasks: set[asyncio.Task[None]] = set()
        self._queue: asyncio.Queue[tuple[CreationEvent, JobRecord]] = asyncio.Queue()
        self._seen: dict[str, float] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._history: deque[JobRecord] = deque(maxlen=history)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        for index in range(self._concurrency):
            self._tasks.append(asyncio.create_task(self._run(), name=f"webhook-worker-{index}"))

    async def stop(self, timeout: float = 25.0) -> None:
        """Let queued jobs finish (up to `timeout`), then cancel the workers."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._queue.join(), timeout)
        for task in self._tasks:
            task.cancel()
        for task in self._retry_tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._retry_tasks, return_exceptions=True)
        self._tasks.clear()
        self._retry_tasks.clear()

    # ------------------------------------------------------------------ intake

    def submit(self, event: CreationEvent) -> bool:
        """Queue an event; returns False if it is a duplicate."""
        now = time.monotonic()
        self._seen = {k: t for k, t in self._seen.items() if now - t < self._dedupe_ttl}
        if event.dedupe_key in self._seen:
            logger.info("Duplicate webhook event ignored", extra={"key": event.dedupe_key})
            return False
        self._seen[event.dedupe_key] = now
        record = JobRecord(key=event.dedupe_key, kind=event.kind, object_id=event.object_id)
        self._history.appendleft(record)
        self._queue.put_nowait((event, record))
        logger.info(
            "Webhook event queued", extra={"key": event.dedupe_key, "attempt": event.attempt}
        )
        return True

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    def recent(self) -> list[dict[str, object]]:
        return [asdict(record) for record in self._history]

    # ------------------------------------------------------------------ processing

    async def _run(self) -> None:
        while True:
            event, record = await self._queue.get()
            try:
                # Give HubSpot a moment: associations made in the same UI action as the
                # creation (e.g. "create company + add contact") land just after the event.
                if self._settle_delay:
                    await asyncio.sleep(self._settle_delay)
                record.state = "running"
                record.attempts += 1
                outcome = await self._processor(event)
                record.state, record.result = "done", outcome.status
                record.credits, record.message = outcome.credits, outcome.message
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                record.message = f"{type(exc).__name__}: {exc}"[:500]
                if is_transient(exc) and event.retry < len(self._retry_delays):
                    record.state = "retrying"
                    self._schedule_retry(event, record, self._retry_delays[event.retry])
                    logger.warning(
                        "Webhook job failed, will retry",
                        extra={"key": event.dedupe_key, "error": record.message},
                    )
                else:
                    record.state = "failed"
                    # Allow a later (manual or HubSpot) delivery to try again.
                    self._seen.pop(event.dedupe_key, None)
                    logger.exception("Webhook job failed", extra={"key": event.dedupe_key})
            finally:
                record.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
                self._queue.task_done()

    def _schedule_retry(self, event: CreationEvent, record: JobRecord, delay: float) -> None:
        async def _later() -> None:
            await asyncio.sleep(delay)
            record.state = "queued"
            self._queue.put_nowait((replace(event, retry=event.retry + 1), record))

        task = asyncio.create_task(_later(), name=f"webhook-retry-{event.dedupe_key}")
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)
