"""Reference participant for the Explaining Markets prediction competition.

★ EDIT `generate_predictions` BELOW to plug in your own model. ★

The Chalice route at the top is boilerplate: it verifies the webhook signature,
skips test events, calls your prediction function, and submits the result. You
shouldn't need to touch it.

Deploy with `chalice deploy --stage dev`, then run `uv run python -m demo.setup`
to register the deployed URL with your submission.
"""

from __future__ import annotations

import json
import os

import httpx
from chalice import Chalice, Response
from openai import OpenAI
from pydantic import BaseModel, Field

from chalicelib.api_client import submit_predictions
from chalicelib.webhook_verify import WebhookVerificationError, verify_webhook

app = Chalice(app_name="explaining-markets-participant")
_openai: OpenAI | None = None  # lazy so imports don't require OPENAI_API_KEY
_openai_warned = False         # one-shot stdout warning per Lambda container

# Production participants should dedup on `event_id` in the body (or the
# `Webhook-Id` header) — the server retries on 5xx/timeout so the same event
# can arrive more than once. Offline scoring joins on `event_id`.


@app.route("/webhook", methods=["POST"], content_types=["application/json"])
def webhook() -> Response:
    """Receive a signed webhook, verify it, generate predictions, submit them."""
    req = app.current_request
    try:
        payload = verify_webhook(
            raw_body=req.raw_body,
            headers=req.headers,
            secret=os.environ["COMPETITION_WEBHOOK_SECRET"],
        )
    except WebhookVerificationError as exc:
        return Response(body={"error": str(exc)}, status_code=401)

    if payload.get("event_type") == "TEST":
        return Response(body={"ok": True, "skipped": "test"}, status_code=200)

    predictions = generate_predictions(payload)
    result = submit_predictions(event_id=payload["event_id"], predictions=predictions)
    return Response(body={"ok": True, "submitted": result}, status_code=200)


# ----------------------------------------------------------------------
# ★ EDIT BELOW — your prediction logic ★
# ----------------------------------------------------------------------


class Prediction(BaseModel):
    """Structured response shape for the LLM call.

    The `Field(ge=0, le=1)` constraint flows through into the JSON schema
    OpenAI's structured-outputs mode enforces during decoding, so the model
    is guaranteed to return a percentile in [0, 1] — no manual clamping or
    fallback parsing needed.
    """

    predicted_percentile: float = Field(ge=0.0, le=1.0)


SYSTEM_PROMPT = """\
You are a senior equity analyst predicting how a stock will react to an event.

Predict a single percentile in [0, 1] for where the focal asset's next-day
return will fall in its historical distribution: 0 = worst, 0.50 = median,
1 = best. The relevant return is the *unexpected*, market-adjusted return —
a great-but-fully-priced-in beat is not a top-decile event.

Calibration discipline:
- Long-run base rates: about 25% of events land "up" (>0.75), 50% "neutral"
  (0.25-0.75), 25% "down" (<0.25). Default toward 0.40-0.60 when signals are
  mixed or modest.
- Reserve values above 0.80 or below 0.20 for cases with unambiguous,
  multi-signal evidence. Do not exceed 0.90 or fall below 0.10 without
  overwhelming, lopsided evidence.
- Tone alone (confident vs hedging language) should move you no more than
  ~0.03 absent quantitative confirmation.
"""


def generate_predictions(payload: dict) -> list[dict]:
    """For each focal asset in the event, return one prediction.

    `payload` is the verified webhook body. Useful fields:
      payload["event_type"]          e.g. "EARNINGS_RELEASE"
      payload["focal_assets"]        list of {"identifier_type", "identifier_value"}
      payload["information_url"]     short-lived signed URL with the event summary JSON
      payload["prediction_deadline"] ISO timestamp; submit before this fires

    Required output: list of dicts, one per focal asset:
      [{"identifier_value": "AAPL", "predicted_percentile": 0.71}, ...]

    `predicted_percentile` is a float in [0, 1] — your model's prediction of
    where the asset's next-day return will fall in its historical distribution
    (0 = worst, 1 = best).
    """
    summary = httpx.get(payload["information_url"], timeout=10.0)
    summary.raise_for_status()
    summary_json = summary.json()

    return [
        {
            "identifier_value": asset["identifier_value"],
            "predicted_percentile": _ask_llm(
                summary=summary_json,
                ticker=asset["identifier_value"],
                event_type=payload["event_type"],
            ),
        }
        for asset in payload["focal_assets"]
    ]


def _ask_llm(*, summary: dict, ticker: str, event_type: str) -> float:
    """Ask gpt-5.4-nano for a calibrated percentile via structured outputs.

    Returns the model's `predicted_percentile`. Falls back to 0.5 only if the
    model refuses (rare); the [0, 1] bound is enforced by the JSON schema, not
    by us.

    If `OPENAI_API_KEY` is not configured, returns 0.5 unconditionally so the
    submission round-trip still works end-to-end without burning credits.
    """
    global _openai, _openai_warned
    if not os.environ.get("OPENAI_API_KEY"):
        if not _openai_warned:
            print(
                "[WARN] OPENAI_API_KEY not set — submitting 0.5 placeholder. "
                "Set the key (or replace generate_predictions) for real predictions."
            )
            _openai_warned = True
        return 0.5
    if _openai is None:
        _openai = OpenAI()  # picks up OPENAI_API_KEY from env

    summary_text = summary.get("summary") if isinstance(summary, dict) else None
    if not summary_text:
        summary_text = json.dumps(summary)
    summary_text = summary_text[:8000]

    user_prompt = (
        f"Event type: {event_type}\n"
        f"Ticker: {ticker}\n\n"
        f"Event summary:\n{summary_text}\n\n"
        "Weigh, in roughly this order:\n"
        "  1. Quantitative surprise vs expectations — revenue, EPS, segment metrics.\n"
        "  2. Guidance / outlook — raises, holds, cuts vs the prior trajectory.\n"
        "  3. Strategic shifts — product launches, M&A, capital allocation, leadership.\n"
        "  4. Tone and confidence in management commentary (small weight).\n"
        "  5. Risks called out — regulatory, supply chain, demand, competition.\n\n"
        f"Predict the next-day unexpected-return percentile for {ticker}."
    )

    resp = _openai.chat.completions.parse(
        model="gpt-5.4-nano",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        response_format=Prediction,
    )
    parsed = resp.choices[0].message.parsed
    if parsed is None:
        return 0.5  # model refused; competition expects a number
    return parsed.predicted_percentile
