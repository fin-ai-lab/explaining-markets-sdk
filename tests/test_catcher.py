"""Validation behavior of the local submission catcher.

Drives the catcher in-process — no network, no subprocess. Plus a
sign↔verify round-trip self-test to catch drift between the demo signer
and the chalicelib verifier.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from chalicelib.webhook_verify import verify_webhook
from demo import catcher, sign


@pytest.fixture(autouse=True)
def _arm_catcher() -> None:
    catcher.arm(
        fixture={"ticker": "AAPL", "summary": "test"},
        event_id="00000000-0000-0000-0000-000000000001",
        focal_assets=["AAPL", "MSFT"],
    )


def _post(body: dict | bytes) -> tuple[int, dict]:
    """Invoke the catcher's validator directly, mirroring what _Handler does for POST."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    return catcher._validate(raw)  # noqa: SLF001 — direct test of validator


def test_happy_path_returns_201() -> None:
    status, resp = _post(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "predictions": [{"identifier_value": "AAPL", "predicted_percentile": 0.7}],
        }
    )
    assert status == 201
    assert resp == {"accepted": True, "event_id": "00000000-0000-0000-0000-000000000001"}
    assert catcher.captured() == {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "predictions": [{"identifier_value": "AAPL", "predicted_percentile": 0.7}],
    }


def test_wrong_event_id_rejected() -> None:
    status, resp = _post(
        {
            "event_id": "ffffffff-ffff-ffff-ffff-ffffffffffff",
            "predictions": [{"identifier_value": "AAPL", "predicted_percentile": 0.5}],
        }
    )
    assert status == 400
    assert resp["error"] == "event_id_mismatch"


def test_percentile_out_of_range_rejected() -> None:
    status, resp = _post(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "predictions": [{"identifier_value": "AAPL", "predicted_percentile": 1.5}],
        }
    )
    assert status == 400
    assert resp["error"] == "schema_invalid"


def test_unknown_identifier_value_rejected() -> None:
    status, resp = _post(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "predictions": [{"identifier_value": "TSLA", "predicted_percentile": 0.5}],
        }
    )
    assert status == 400
    assert resp["error"] == "identifier_value_not_in_focal_assets"
    assert resp["invalid"] == ["TSLA"]


def test_empty_predictions_rejected() -> None:
    status, resp = _post(
        {"event_id": "00000000-0000-0000-0000-000000000001", "predictions": []}
    )
    assert status == 400
    assert resp["error"] == "schema_invalid"


def test_malformed_json_rejected() -> None:
    status, resp = _post(b"{not json")
    assert status == 400
    assert resp["error"] == "schema_invalid"


def test_extra_field_rejected() -> None:
    status, resp = _post(
        {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "predictions": [{"identifier_value": "AAPL", "predicted_percentile": 0.5}],
            "bonus_field": "shouldnt_be_here",
        }
    )
    assert status == 400
    assert resp["error"] == "schema_invalid"


def test_sign_then_verify_roundtrip() -> None:
    """Catches drift between demo.sign.sign and chalicelib.webhook_verify.verify_webhook."""
    secret = "whsec_" + "A" * 43  # 43 url-safe base64 chars ≈ 32 random bytes
    webhook_id = f"evt_{uuid.uuid4().hex}"
    timestamp = int(time.time())
    raw_body = b'{"hello":"world","n":42}'
    header = sign.sign(
        secret=secret,
        webhook_id=webhook_id,
        timestamp=timestamp,
        raw_body=raw_body,
    )
    body = verify_webhook(
        raw_body=raw_body,
        headers={
            "webhook-id": webhook_id,
            "webhook-timestamp": str(timestamp),
            "webhook-signature": header,
        },
        secret=secret,
    )
    assert body == {"hello": "world", "n": 42}
