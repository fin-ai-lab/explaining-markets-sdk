"""Pinned to docs/test_vectors.json from the main competition repo.

These tests confirm the verifier matches the server signer byte-for-byte. If
they pass, your participant deployment will accept real broadcasts.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chalicelib.webhook_verify import WebhookVerificationError, verify_webhook

VECTORS = json.loads((Path(__file__).parent / "test_vectors.json").read_text())["vectors"]
# Vectors are frozen at a fixed UNIX timestamp from 2024 — disable time check.
NO_CLOCK = 10**9


def _headers(v: dict) -> dict:
    return {
        "webhook-id": v["webhook_id"],
        "webhook-timestamp": str(v["timestamp"]),
        "webhook-signature": v["expected_signature_header"],
    }


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_valid_signature_accepts_and_returns_parsed_body(v: dict) -> None:
    body = verify_webhook(
        raw_body=v["raw_body"].encode("utf-8"),
        headers=_headers(v),
        secret=v["secret"],
        tolerance_seconds=NO_CLOCK,
    )
    assert body == json.loads(v["raw_body"])


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_tampered_body_rejected(v: dict) -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            raw_body=v["raw_body"].encode("utf-8") + b" ",
            headers=_headers(v),
            secret=v["secret"],
            tolerance_seconds=NO_CLOCK,
        )


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_wrong_secret_rejected(v: dict) -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            raw_body=v["raw_body"].encode("utf-8"),
            headers=_headers(v),
            secret="whsec_d3Jvbmctc2VjcmV0LXdyb25nLXNlY3JldC13cm9uZw",
            tolerance_seconds=NO_CLOCK,
        )


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_stale_timestamp_rejected_with_default_tolerance(v: dict) -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(
            raw_body=v["raw_body"].encode("utf-8"),
            headers=_headers(v),
            secret=v["secret"],
        )


@pytest.mark.parametrize("v", VECTORS, ids=[v["name"] for v in VECTORS])
def test_case_insensitive_headers(v: dict) -> None:
    body = verify_webhook(
        raw_body=v["raw_body"].encode("utf-8"),
        headers={
            "Webhook-Id": v["webhook_id"],
            "Webhook-Timestamp": str(v["timestamp"]),
            "Webhook-Signature": v["expected_signature_header"],
        },
        secret=v["secret"],
        tolerance_seconds=NO_CLOCK,
    )
    assert body == json.loads(v["raw_body"])


def test_missing_headers_rejected() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_webhook(raw_body=b"{}", headers={}, secret="whsec_anything")


def test_multiple_signatures_accept_any_match() -> None:
    """During rotation the server emits two space-delimited tokens."""
    v = VECTORS[0]
    rotated_header = f"v1,not-a-real-sig= {v['expected_signature_header']}"
    body = verify_webhook(
        raw_body=v["raw_body"].encode("utf-8"),
        headers={
            "webhook-id": v["webhook_id"],
            "webhook-timestamp": str(v["timestamp"]),
            "webhook-signature": rotated_header,
        },
        secret=v["secret"],
        tolerance_seconds=NO_CLOCK,
    )
    assert body == json.loads(v["raw_body"])
