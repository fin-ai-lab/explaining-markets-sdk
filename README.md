# Explaining Markets SDK

An open competition, sponsored by Optiver, using AI to explain how markets react to earnings news. Use this SDK to create a submission — AWS Lambda and OpenAI by default; both are swappable. Setup takes < 5 minutes.

<!-- <ToDo: Diagram of how participation works so that users know what they are doing and why> -->

## Quickstart


### Prerequisites

- AWS account with credentials on this machine — the default path deploys an AWS Lambda handler ([sign up](https://signin.aws.amazon.com/signup?request_type=register)). 
   > See [Using a different deployment platform](#using-a-different-deployment-platform) for non-AWS options.
- The package and project manager [`uv`](https://docs.astral.sh/uv/#installation) installed.

### Register, Test, and Optionally Teardown

1. **Sign up**: create an Explaining Markets account [here](https://portal-beta.explainingmarkets.ai/). This will let you manage submissions and track performance.
2. **Clone and install**: `git clone https://github.com/fin-ai-lab/explaining-markets-participant-demo.git && cd explaining-markets-participant-demo && uv sync && cp .env.example .env`
3. **Run setup, deploy, register**: Create a submission and save credentials: `uv run python -m demo.setup`; deploy to Lambda `uv run chalice deploy --stage dev`; test `uv run python -m demo.setup`; view in the [UI](https://portal-beta.explainingmarkets.ai/submissions).

> Optional: set `OPENAI_API_KEY` in `.env` to use the reference LLM-based prediction. Without it, your handler submits a `0.5` placeholder for every prediction — enough to verify deployment and the end-to-end flow, but not enough to actually score in the competition.

**Tear-down**: AWS infrastructure can be torn down with a single command `uv run chalice delete --stage dev`.

## Detailed Setup

**The only file you need to edit is [`app.py`](./app.py).** Replace the
`generate_predictions` function with your own model. Everything else (HMAC
verification, API plumbing, submission setup) is wired up for you. You can
also ship a first deploy with no edits — your handler will submit `0.5`
placeholders until you plug in your model, which is enough to verify the
round-trip end-to-end.

### Setup

This project uses [uv](https://docs.astral.sh/uv/#installation). Once you have
it installed:

```sh
uv sync --extra dev
cp .env.example .env       # OPENAI_API_KEY is optional — see below
```

`uv sync` creates `.venv/` and installs both runtime and dev dependencies from
`pyproject.toml` / `uv.lock`. You don't need to activate the venv — every
command below uses `uv run`, which executes inside it. `pyproject.toml` pins
Python to `>=3.11,<3.14` (Chalice's IAM-policy analyzer uses `ast.Str`, which
was removed in 3.14); `.python-version` requests 3.12 so `uv` picks a
compatible interpreter automatically.

Environment variables in `.env`:

- `OPENAI_API_KEY` — **optional.** Used by the reference prediction in
  `app.py`. Without it, your handler submits a `0.5` placeholder for every
  prediction (enough to verify the deployment; not enough to score). Set the
  key — or replace `generate_predictions` — to get real predictions.
- `COMPETITION_API_URL` — defaults to beta (`api-beta.explainingmarkets.ai`).
  Switch to `api.explainingmarkets.ai` when you're ready to compete for real.

`COMPETITION_SUBMISSION_ID`, `COMPETITION_API_KEY`, and
`COMPETITION_WEBHOOK_SECRET` are filled in for you by the setup script in the
next step. The setup script authenticates by opening your browser to the
portal sign-in page and catching the OAuth redirect on
`http://localhost:8765/callback` — no passwords go through the CLI.
Your refresh token is cached to `.competition-token-cache.json` (gitignored)
so subsequent runs don't reopen the browser.

### Create your submission

```sh
uv run python -m demo.setup
```

This authenticates with the portal, creates a submission, mints credentials,
and writes them to `.env` (and to `.chalice/config.json` on the default
Chalice path — skipped if you've set `WEBHOOK_URL` for a non-Chalice
deployment). If you already have a submission configured, the script detects
this and offers to either reuse it or (with `--fresh`) create a new one —
your existing submission is never modified.

> If your profile isn't complete yet, the script will direct you to finish
> onboarding in the portal first.

> While the competition is in invite-gated beta, the script will prompt for
> an invite code on a brand-new account. Request one from the competition
> admins if you don't have one.

### Run the tests

```sh
uv run pytest
```

These verify the HMAC verifier against the frozen test vectors in
`tests/test_vectors.json` and exercise the local catcher's schema validation.
If they pass, your deployment will accept real broadcasts byte-for-byte.

### Test your handler end-to-end locally

```sh
uv run python -m demo
```

Stands up a mock-competition harness that exercises your full
verify → fetch → predict → submit loop, all on your laptop. This harness
boots `chalice local`, so it's Chalice-specific — non-Chalice users will
need to wire up their own local server against the catcher (or rely on the
synthetic test webhook against the real deployment):

1. Starts a local catcher on `:9999` that mimics the real `/v1/predictions`
   endpoint and validates inbound bodies against the OpenAPI
   `PredictionSubmission` schema.
2. Boots `chalice local` on `:8000` with the catcher wired in as
   `COMPETITION_API_URL`.
3. Mints a `WebhookPayload`, signs it with your
   `COMPETITION_WEBHOOK_SECRET`, and POSTs it to your handler.
4. Watches the round-trip live: a Rich panel walks each stage from
   `webhook signed` to `submission validated`, ending green with the
   captured submission body — or red with field-level errors.

Pick from five frozen earnings events (AAPL, ROKU, LMT, RIVN, UAL) in the
picker, or paste a path / raw JSON for your own.

> The orchestrator temporarily rewrites `.chalice/config.json` so
> `chalice local` sees your `.env` values (Chalice's local server reads
> the config file, not parent env). It restores on clean exit. If you
> `kill -9` it, run `git restore .chalice/config.json` to recover.

### Edit the prediction logic

Open `app.py` and look for:

```python
def generate_predictions(payload: dict) -> list[dict]:
```

The function receives the verified webhook payload (event_type, focal_assets,
information_url, prediction_deadline) and returns one prediction per focal
asset. Replace the body with whatever model you want.

The required output shape:

```python
[{"identifier_value": "AAPL", "predicted_percentile": 0.71}, ...]
```

`predicted_percentile` is a float in `[0, 1]`.

### Deploy

```sh
uv run chalice deploy --stage dev
```

Chalice prints the deployed URL, e.g.
`https://abc123.execute-api.us-east-1.amazonaws.com/api`.

### Register your webhook

```sh
uv run python -m demo.setup
```

Re-running the setup script after deploy auto-detects your handler's URL
(from `.chalice/deployed/dev.json` by default, or `WEBHOOK_URL` if set),
registers it against your submission (which auto-promotes from `draft` to
`active`), and offers to fire a synthetic test webhook. If you accept, the
script polls the competition API for the delivery outcome and renders it
inline:

- ✅ **Delivered** (2xx) — your handler accepted the event.
- ❌ **Delivery failed** (4xx/5xx) — with a hint at the likely cause
  (signature mismatch, traceback in your code, etc.).
- ⏱️ **Timed out** — handler unreachable or too slow.

No log-tailing required; the API itself is the source of truth.

You're done. Real events will arrive at your handler when the next competition
window opens.

### Layout

```
app.py                            # ★ edit `generate_predictions` here
chalicelib/webhook_verify.py      # HMAC-SHA256 verifier (vendored, stdlib only)
chalicelib/api_client.py          # POST /v1/predictions
demo/setup.py                     # `uv run python -m demo.setup` — submission lifecycle
demo/                             # `uv run python -m demo` — local test harness
tests/test_webhook_verify.py      # pinned to upstream test vectors
tests/test_catcher.py             # harness validation tests
.chalice/config.json              # Chalice stage config
requirements.txt                  # runtime deps Chalice bundles into the Lambda zip
.python-version                   # pins Python to 3.12 (Chalice doesn't work on 3.14)
```

### Using a different deployment platform

Chalice is opinionated about AWS. The two utility modules in `chalicelib/` are
plain-stdlib Python and have no AWS dependency — drop them into a Flask or
FastAPI app on Cloud Run / Fly / a VM, wire `verify_webhook` to your raw
request body, and you're done.

To register your webhook through `demo.setup` on a non-Chalice deployment,
set `WEBHOOK_URL` in `.env`:

```
WEBHOOK_URL=https://your-handler.example.com/webhook
```

The setup script will use that URL instead of `.chalice/deployed/dev.json`
and will skip writing credentials to `.chalice/config.json` (since your
platform — not Chalice — is responsible for injecting `COMPETITION_*` env
vars into your handler).
