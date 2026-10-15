"""Shared HTTP error types and retry policy for the Forager and HubSpot clients.

Retry rules:
* 429 and 502/503/504 are retried, honouring `Retry-After` when the server sends it.
* Connection failures (request never reached the server) are always safe to retry.
* Read timeouts / dropped connections are only retried for *idempotent* calls. For paid
  Forager lookups we cannot know whether the server already charged us, so we surface
  the error instead of risking a double charge.
"""

from __future__ import annotations

import email.utils
import logging
import random
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_SAFE_TRANSPORT_ERRORS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
_IDEMPOTENT_TRANSPORT_ERRORS = (
    *_SAFE_TRANSPORT_ERRORS,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)


class ApiError(Exception):
    """Non-success response from an upstream API."""

    def __init__(self, service: str, status_code: int, message: str, body: Any = None) -> None:
        super().__init__(f"{service} API error {status_code}: {message}")
        self.service = service
        self.status_code = status_code
        self.body = body


class RetryableApiError(ApiError):
    def __init__(
        self,
        service: str,
        status_code: int,
        message: str,
        body: Any = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(service, status_code, message, body)
        self.retry_after = retry_after


def parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` may be delta-seconds or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed.timestamp() - time.time())


def raise_for_response(service: str, response: httpx.Response) -> None:
    if response.is_success:
        return
    try:
        body: Any = response.json()
    except ValueError:
        body = response.text[:500]
    message = _error_message(body) or response.reason_phrase
    if response.status_code in RETRYABLE_STATUS:
        raise RetryableApiError(
            service,
            response.status_code,
            message,
            body,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
    raise ApiError(service, response.status_code, message, body)


def _error_message(body: Any) -> str | None:
    if isinstance(body, dict):
        for key in ("message", "detail", "error"):
            if isinstance(body.get(key), str):
                return str(body[key])
        return str(body)[:300]
    if isinstance(body, str):
        return body[:300]
    return None


class _WaitRetryAfterOrBackoff:
    """Use the server's Retry-After when present, else exponential backoff with jitter."""

    def __init__(self, base: float = 1.0, maximum: float = 30.0) -> None:
        self.base = base
        self.maximum = maximum

    def __call__(self, state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome else None
        if isinstance(exc, RetryableApiError) and exc.retry_after is not None:
            return min(exc.retry_after + random.uniform(0, 0.5), self.maximum)
        backoff = self.base * float(2 ** (state.attempt_number - 1))
        return min(backoff, self.maximum) * random.uniform(0.5, 1.0)


def retry_policy(
    *, idempotent: bool, max_attempts: int = 5, base_wait: float = 1.0, max_wait: float = 30.0
) -> AsyncRetrying:
    transport_errors = _IDEMPOTENT_TRANSPORT_ERRORS if idempotent else _SAFE_TRANSPORT_ERRORS

    def _should_retry(exc: BaseException) -> bool:
        return isinstance(exc, (RetryableApiError, *transport_errors))

    def _log(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome else None
        logger.warning(
            "Retrying after error",
            extra={"attempt": state.attempt_number, "error": repr(exc)},
        )

    return AsyncRetrying(
        retry=retry_if_exception(_should_retry),
        stop=stop_after_attempt(max_attempts),
        wait=_WaitRetryAfterOrBackoff(base_wait, max_wait),
        before_sleep=_log,
        reraise=True,
    )
