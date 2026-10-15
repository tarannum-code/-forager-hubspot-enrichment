"""On-disk cache of Forager responses, so the same lookup is never paid for twice.

One JSON file per (endpoint, request body). Writes are atomic (tmp file + rename), so a
crash mid-write can't leave a corrupt entry. "Not found" answers are cached too, because
re-asking would cost credits for the same empty answer.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MISSING = object()


def cache_key(endpoint: str, body: dict[str, Any]) -> str:
    canonical = json.dumps({"endpoint": endpoint, "body": body}, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class ResponseCache:
    def __init__(self, directory: Path | None, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled and directory is not None
        if self.directory is not None and self.enabled:
            self.directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def disabled(cls) -> ResponseCache:
        return cls(None, enabled=False)

    def _path(self, endpoint: str, body: dict[str, Any]) -> Path:
        assert self.directory is not None
        return self.directory / f"{endpoint}-{cache_key(endpoint, body)}.json"

    def get(self, endpoint: str, body: dict[str, Any]) -> Any:
        """Return the cached response, or the `MISSING` sentinel."""
        if not self.enabled:
            return _MISSING
        path = self._path(endpoint, body)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _MISSING
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable cache entry", extra={"path": str(path)})
            return _MISSING
        logger.info("Forager cache hit (0 credits)", extra={"endpoint": endpoint})
        return entry.get("response")

    def set(self, endpoint: str, body: dict[str, Any], response: Any) -> None:
        if not self.enabled:
            return
        path = self._path(endpoint, body)
        entry = {
            "endpoint": endpoint,
            "request": body,
            "fetched_at": datetime.now(UTC).isoformat(),
            "response": response,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(entry, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    def delete(self, endpoint: str, body: dict[str, Any]) -> None:
        if self.enabled:
            self._path(endpoint, body).unlink(missing_ok=True)

    def stats(self) -> dict[str, int]:
        if not self.enabled or self.directory is None or not self.directory.exists():
            return {}
        counts: dict[str, int] = {}
        for file in self.directory.glob("*.json"):
            endpoint = file.name.rsplit("-", 1)[0]
            counts[endpoint] = counts.get(endpoint, 0) + 1
        return counts

    @staticmethod
    def is_missing(value: Any) -> bool:
        return value is _MISSING
