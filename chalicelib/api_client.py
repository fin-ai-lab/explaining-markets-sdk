"""Submit predictions to the competition API."""

from __future__ import annotations

import os

import httpx


class PredictionSubmissionError(RuntimeError):
    pass


def submit_predictions(*, event_id: str, predictions: list[dict]) -> dict:
    """POST predictions to {COMPETITION_API_URL}/predictions with X-API-Key.

    `predictions` is a list of {"identifier_value": str, "predicted_percentile": float}.
    Returns the parsed response body on success; raises on non-2xx.
    """
    url = os.environ["COMPETITION_API_URL"].rstrip("/") + "/predictions"
    api_key = os.environ["COMPETITION_API_KEY"]
    body = {"event_id": event_id, "predictions": predictions}

    resp = httpx.post(
        url,
        json=body,
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        timeout=10.0,
    )
    if resp.status_code >= 300:
        raise PredictionSubmissionError(
            f"prediction submission failed: {resp.status_code} {resp.text}"
        )
    return resp.json()
