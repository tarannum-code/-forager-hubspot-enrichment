"""HubSpot request signature v3 verification.

Spec (developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/
request-validation):

* reject if `X-HubSpot-Request-Timestamp` (ms) is older than 5 minutes;
* decode a fixed set of URL-encoded characters in the request URI;
* source = method + uri + raw body + timestamp (UTF-8);
* signature = base64(HMAC-SHA256(client_secret, source)), compared in constant time.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

SIGNATURE_HEADER = "X-HubSpot-Signature-v3"
TIMESTAMP_HEADER = "X-HubSpot-Request-Timestamp"

# The exact set HubSpot lists; nothing else is decoded (notably not %20 or %26).
_DECODE = {
    "%3A": ":",
    "%2F": "/",
    "%3F": "?",
    "%40": "@",
    "%21": "!",
    "%24": "$",
    "%27": "'",
    "%28": "(",
    "%29": ")",
    "%2A": "*",
    "%2C": ",",
    "%3B": ";",
}
_ENCODED = re.compile("|".join(re.escape(k) for k in _DECODE), re.IGNORECASE)


class SignatureError(Exception):
    """The request could not be authenticated as coming from HubSpot."""


def decode_uri(uri: str) -> str:
    return _ENCODED.sub(lambda m: _DECODE[m.group(0).upper()], uri.split("#", 1)[0])


def compute_signature(secret: str, method: str, uri: str, body: bytes, timestamp: str) -> str:
    source = method.upper().encode() + decode_uri(uri).encode() + body + timestamp.encode()
    digest = hmac.new(secret.encode(), source, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_v3(
    *,
    secret: str,
    method: str,
    uri: str,
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    max_skew_seconds: int = 300,
    now_ms: int | None = None,
) -> None:
    """Raise SignatureError unless the request carries a valid, fresh v3 signature."""
    if not signature or not timestamp:
        raise SignatureError("missing signature headers")
    try:
        sent_ms = int(timestamp)
    except ValueError as exc:
        raise SignatureError("malformed timestamp") from exc
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    if abs(now_ms - sent_ms) > max_skew_seconds * 1000:
        raise SignatureError("timestamp outside the allowed window (possible replay)")
    expected = compute_signature(secret, method, uri, body, timestamp)
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        raise SignatureError("signature mismatch")
