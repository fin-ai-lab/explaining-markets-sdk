"""Build and sign WebhookPayloads for the local test harness.

Symmetric counterpart to chalicelib/webhook_verify.py. The participant's
runtime never signs, so this lives in the test-only `demo` package.

Algorithm matches the server signer byte-for-byte:

    secret_bytes   = base64url_decode(secret.removeprefix("whsec_"))
    signed_payload = f"{webhook_id}.{timestamp}.".encode() + raw_body
    signature      = base64(HMAC_SHA256(secret_bytes, signed_payload))
    header         = f"v1,{signature}"
"""

from __future__ import annotations

import base64
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from .schema import AssetIdentifier, WebhookPayload

CATCHER_BASE_URL = "http://localhost:9999"


def build_payload(*, ticker: str, event_datetime: datetime) -> WebhookPayload:
    """Mint a fresh WebhookPayload for the given ticker.

    `prediction_deadline = now + 5min` so the participant's handler always
    sees a live deadline regardless of the fixture's recorded date.
    """
    now = datetime.now(UTC)
    return WebhookPayload(
        id=f"evt_{uuid.uuid4().hex}",
        event_id=uuid.uuid4(),
        event_type="EARNINGS_RELEASE",
        timing_category="SCHEDULED",
        event_datetime=event_datetime,
        focal_assets=[AssetIdentifier(identifier_type="TICKER", identifier_value=ticker)],
        information_url=f"{CATCHER_BASE_URL}/summary/{ticker}",
        prediction_deadline=now + timedelta(minutes=5),
    )


def sign(*, secret: str, webhook_id: str, timestamp: int, raw_body: bytes) -> str:
    """Return the value to put in the `Webhook-Signature` header."""
    key = _decode_secret(secret)
    signed_payload = (
        webhook_id.encode("utf-8") + b"." + str(timestamp).encode("ascii") + b"." + raw_body
    )
    digest = hmac.new(key, signed_payload, sha256).digest()
    return f"v1,{base64.b64encode(digest).decode('ascii')}"


def _decode_secret(secret: str) -> bytes:
    if not secret.startswith("whsec_"):
        raise ValueError("signing secret must start with 'whsec_'")
    body = secret[len("whsec_"):]
    pad = "=" * (-len(body) % 4)
    return base64.urlsafe_b64decode(body + pad)


def serialize(payload: WebhookPayload) -> bytes:
    """JSON-encode a payload as bytes. Used as the signed body."""
    return json.dumps(
        payload.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def load_fixture(path: Path) -> dict:
    return json.loads(path.read_text())
