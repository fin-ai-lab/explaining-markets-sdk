"""HMAC-SHA256 webhook verifier for the Explaining Markets competition.

Standard Webhooks / Svix style. The server signs:

    secret_bytes   = base64url_decode(secret.removeprefix("whsec_"))
    signed_payload = f"{webhook_id}.{timestamp}.".encode() + raw_body
    signature      = base64(HMAC_SHA256(secret_bytes, signed_payload))

The `Webhook-Signature` header is one or more `v1,<sig>` tokens, space-delimited
(multiple appear during signing-secret rotation). Accept the delivery if any
token matches.

Pass the *raw* request body bytes — not a re-serialized JSON dict.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from typing import Mapping

_SECRET_PREFIX = "whsec_"
_SIGNATURE_VERSION = "v1"


class WebhookVerificationError(Exception):
    pass


def verify_webhook(
    *,
    raw_body: bytes,
    headers: Mapping[str, str],
    secret: str,
    tolerance_seconds: int = 300,
) -> dict:
    """Verify a webhook delivery and return the parsed JSON body.

    Raises `WebhookVerificationError` on any failure (missing headers, stale
    timestamp, signature mismatch, malformed JSON).
    """
    h = {k.lower(): v for k, v in headers.items()}
    webhook_id = h.get("webhook-id")
    timestamp_raw = h.get("webhook-timestamp")
    signature_header = h.get("webhook-signature")
    if not (webhook_id and timestamp_raw and signature_header):
        raise WebhookVerificationError("missing required webhook headers")

    try:
        timestamp = int(timestamp_raw)
    except ValueError as exc:
        raise WebhookVerificationError("webhook-timestamp is not an integer") from exc

    if abs(int(time.time()) - timestamp) > tolerance_seconds:
        raise WebhookVerificationError("webhook timestamp outside tolerance window")

    key = _decode_secret(secret)
    signed_payload = (
        webhook_id.encode("utf-8") + b"." + str(timestamp).encode("ascii") + b"." + raw_body
    )
    expected = base64.b64encode(hmac.new(key, signed_payload, sha256).digest()).decode("ascii")

    for token in signature_header.split():
        version, _, value = token.partition(",")
        if version != _SIGNATURE_VERSION or not value:
            continue
        if hmac.compare_digest(expected, value):
            try:
                return json.loads(raw_body)
            except json.JSONDecodeError as exc:
                raise WebhookVerificationError("body is not valid JSON") from exc

    raise WebhookVerificationError("no matching signature")


def _decode_secret(secret: str) -> bytes:
    if not secret.startswith(_SECRET_PREFIX):
        raise WebhookVerificationError(f"signing secret must start with {_SECRET_PREFIX!r}")
    body = secret[len(_SECRET_PREFIX):]
    pad = "=" * (-len(body) % 4)
    try:
        return base64.urlsafe_b64decode(body + pad)
    except Exception as exc:
        raise WebhookVerificationError("signing secret body is not valid base64url") from exc
