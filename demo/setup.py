"""One-shot setup for a competition submission.

Run with:

    uv run python -m demo.setup            # state-aware: progresses to next step
    uv run python -m demo.setup --fresh    # force a new submission (warns first)

State machine:

  no submission in .env          -> create submission + mint creds + write .env
  submission in .env, no deploy  -> tell user to deploy (Chalice or set WEBHOOK_URL)
  submission in .env + deployed  -> PATCH webhook_url + offer test webhook

The "deployed URL" is resolved from `.chalice/deployed/dev.json` by default;
participants on other platforms can opt out of Chalice by setting `WEBHOOK_URL`
in `.env`, which takes precedence over the Chalice artifact.

Auth uses the Cognito Hosted UI OAuth 2.0 authorization-code flow with PKCE.
The script opens the participant's browser to the portal sign-in page and
catches the redirect on http://localhost:8765/callback. No passwords ever
touch the CLI or .env. Refresh tokens are cached to
`.competition-token-cache.json` (gitignored) so subsequent runs don't reopen
the browser.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx
import questionary
from dotenv import load_dotenv, set_key
from rich.console import Console
from rich.panel import Panel

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
CHALICE_CONFIG_PATH = REPO_ROOT / ".chalice" / "config.json"
CHALICE_DEPLOYED_PATH = REPO_ROOT / ".chalice" / "deployed" / "dev.json"
TOKEN_CACHE_PATH = REPO_ROOT / ".competition-token-cache.json"

DEFAULT_API_URL = "https://api-beta.explainingmarkets.ai/v1"
# Public Cognito OAuth client ID (PKCE flow, no client_secret) — safe to embed,
# same as OAuth client IDs in single-page apps.
DEFAULT_COGNITO_CLIENT_ID = "2kasgjsgubh4mco7dg9j83bp8n"
DEFAULT_COGNITO_DOMAIN = "https://auth.explainingmarkets.ai"
PORTAL_URL = "https://portal-beta.explainingmarkets.ai"
ONBOARDING_URL = f"{PORTAL_URL}/onboarding"
CALLBACK_PORT = 8765
CALLBACK_PATH = "/callback"
OAUTH_SCOPES = "openid email profile"

console = Console()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Force creation of a new submission, overwriting credentials in .env.",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Force a fresh browser login (ignore cached refresh token).",
    )
    args = parser.parse_args()

    load_dotenv(ENV_PATH)

    if not os.environ.get("OPENAI_API_KEY"):
        console.print(
            "🚨 [yellow]OPENAI_API_KEY is not set.[/yellow] Your handler will submit "
            "[bold]0.5[/bold] placeholders instead of real predictions until you add a "
            "key to [bold].env[/bold] (or replace [bold]generate_predictions[/bold] in "
            "[bold]app.py[/bold]). This is fine for verifying the round-trip.\n"
        )

    api_url = os.environ.get("COMPETITION_API_URL", DEFAULT_API_URL).rstrip("/")
    submission_id = os.environ.get("COMPETITION_SUBMISSION_ID", "").strip()
    has_creds = bool(os.environ.get("COMPETITION_API_KEY")) and bool(
        os.environ.get("COMPETITION_WEBHOOK_SECRET")
    )
    deployed_url = _read_deployed_webhook_url()

    if args.fresh:
        if submission_id:
            console.print(
                f"[yellow]This will create a NEW submission and mint NEW credentials.[/yellow]\n"
                f"Your existing submission [bold]{submission_id}[/bold] will remain in your "
                f"account but the credentials in .env will be replaced."
            )
            if not questionary.confirm("Continue?", default=False).ask():
                console.print("[red]Aborted.[/red]")
                return 1
        return _do_fresh_setup(api_url, force_reauth=args.reauth)

    if not submission_id or not has_creds:
        return _do_fresh_setup(api_url, force_reauth=args.reauth)

    if not deployed_url:
        console.print(
            f"[green]Submission {submission_id} already configured.[/green]\n"
            f"Now deploy your handler:\n"
            f"  [bold]uv run chalice deploy --stage dev[/bold]   (default — AWS via Chalice)\n"
            f"  …or deploy elsewhere and set [bold]WEBHOOK_URL[/bold] in [bold].env[/bold]\n"
            f"Then re-run [bold]uv run python -m demo.setup[/bold] to register the webhook."
        )
        return 0

    return _do_register_webhook(api_url, submission_id, deployed_url, force_reauth=args.reauth)


def _do_fresh_setup(api_url: str, *, force_reauth: bool) -> int:
    console.rule("[bold]Step 1 — authenticate")
    try:
        token = _get_id_token(force_reauth=force_reauth)
    except _SetupError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.rule("[bold]Step 2 — check profile")
    me = _get_me_or_redeem(api_url, token=token)
    if me is None:
        return 1
    if not me.get("profile_complete"):
        console.print(
            f"[yellow]Your profile isn't complete yet.[/yellow]\n"
            f"Please finish onboarding in the portal, then re-run this script:\n"
            f"  [bold]{ONBOARDING_URL}[/bold]"
        )
        return 1
    console.print(f"[green]✓[/green] profile complete · status: {me.get('status')}")

    console.rule("[bold]Step 3 — create submission")
    submission_id, status = _create_submission(api_url, token)
    if submission_id is None:
        return 1
    console.print(f"[green]✓[/green] submission [bold]{submission_id}[/bold] · status: {status}")

    console.rule("[bold]Step 4 — mint credentials")
    try:
        creds = _initialize_credentials(api_url, submission_id, token)
    except _SetupError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1
    api_key = creds["api_key"]
    signing_secret = creds["signing_secret"]
    console.print(
        f"[green]✓[/green] api_key prefix: [bold]{api_key[:14]}…[/bold]  "
        f"signing_secret prefix: [bold]{signing_secret[:10]}…[/bold]"
    )
    chalice_path = _is_chalice_path()
    persisted_to = ".env and .chalice/config.json" if chalice_path else ".env"
    console.print(
        f"[dim]These are shown ONCE. Saving to {persisted_to} now.[/dim]"
    )

    _persist_secrets(
        submission_id=submission_id,
        api_key=api_key,
        signing_secret=signing_secret,
        api_url=api_url,
    )
    console.print(f"[green]✓[/green] wrote to {persisted_to}")
    if not chalice_path:
        console.print(
            "[dim]Detected WEBHOOK_URL in .env — non-Chalice path. "
            "Inject COMPETITION_* env vars on your platform yourself.[/dim]"
        )

    if chalice_path:
        console.rule("[bold]Step 5 — deploy")
        console.print(
            "Your handler is ready to deploy to AWS Lambda. You can deploy now "
            "with the default [bold]0.5[/bold]-placeholder logic — fine for "
            "verifying the round-trip — and edit "
            "[bold]generate_predictions[/bold] in [bold]app.py[/bold] later, "
            "or skip this prompt and run [bold]chalice deploy[/bold] yourself "
            "after customizing.\n"
        )
        if questionary.confirm("Deploy now?", default=True).ask():
            if not _run_chalice_deploy():
                return 1
            deployed_url = _read_deployed_webhook_url()
            if deployed_url is None:
                console.print(
                    "[red]Deploy succeeded but couldn't read the deployed URL "
                    "from .chalice/deployed/dev.json. Re-run "
                    "[bold]uv run python -m demo.setup[/bold] to retry "
                    "registration.[/red]"
                )
                return 1
            if not _patch_webhook_url(
                api_url, submission_id, deployed_url,
                token=token, step_label="Step 6 — register webhook URL",
            ):
                return 1
            return _fire_test_webhook(
                api_url, submission_id,
                token=token, step_label="Step 7 — test event",
            )

    deploy_step = (
        "  [bold]uv run chalice deploy --stage dev[/bold]\n"
        if chalice_path
        else "  [bold]Deploy your app to your chosen platform[/bold]\n"
    )
    console.rule("[bold]Next")
    console.print(
        "Edit [bold]app.py[/bold]'s [bold]generate_predictions[/bold] function with your model,\n"
        "then deploy and register the webhook:\n\n"
        f"{deploy_step}"
        "  [bold]uv run python -m demo.setup[/bold]\n"
    )
    return 0


def _run_chalice_deploy() -> bool:
    """Run `uv run chalice deploy --stage dev` as a subprocess.

    Streams Chalice's CloudFormation output directly to the terminal so the
    user sees progress in real time. Returns True on success, False otherwise
    (with a recovery hint already printed).
    """
    console.print(
        "[dim]Running [bold]uv run chalice deploy --stage dev[/bold] — "
        "this usually takes 60+ seconds.[/dim]\n"
    )
    try:
        result = subprocess.run(
            ["uv", "run", "chalice", "deploy", "--stage", "dev"],
            cwd=REPO_ROOT,
        )
    except FileNotFoundError:
        console.print(
            "[red]Couldn't find [bold]uv[/bold] on PATH. Install it from "
            "https://docs.astral.sh/uv/#installation, or run "
            "[bold]chalice deploy --stage dev[/bold] yourself.[/red]"
        )
        return False

    if result.returncode != 0:
        console.print(
            f"\n[red]Deploy failed (exit code {result.returncode}).[/red]\n"
            "Common causes:\n"
            "  • AWS credentials not configured (run [bold]aws configure[/bold])\n"
            "  • Insufficient IAM permissions "
            "(needs Lambda, IAM, CloudFormation, API Gateway access)\n"
            "  • Network or AWS-side error (try again in a minute)\n\n"
            "If the deploy partially created resources, clean them up before "
            "retrying with:\n"
            "  [bold]uv run chalice delete --stage dev[/bold]"
        )
        return False

    console.print("\n[green]✓[/green] deploy succeeded")
    return True


def _do_register_webhook(
    api_url: str, submission_id: str, deployed_url: str, *, force_reauth: bool
) -> int:
    console.rule("[bold]Register webhook")
    console.print(f"submission: [bold]{submission_id}[/bold]")
    console.print(f"webhook URL: [bold]{deployed_url}[/bold]\n")

    console.rule("[bold]Step 1 — authenticate")
    try:
        token = _get_id_token(force_reauth=force_reauth)
    except _SetupError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    console.rule("[bold]Step 2 — check current submission state")
    sub = _api_get(api_url, f"/submissions/{submission_id}", token=token)
    if sub is None:
        return 1
    current_url = sub.get("webhook_url")
    current_status = sub.get("status")
    console.print(f"current status: [bold]{current_status}[/bold]  url: {current_url or '(none)'}")

    if current_url == deployed_url and current_status == "active":
        console.print("[green]✓[/green] webhook already registered and active")
    elif not _patch_webhook_url(
        api_url, submission_id, deployed_url,
        token=token, step_label="Step 3 — PATCH webhook_url",
    ):
        return 1

    return _fire_test_webhook(
        api_url, submission_id,
        token=token, step_label="Fire a test webhook?",
    )


def _patch_webhook_url(
    api_url: str,
    submission_id: str,
    deployed_url: str,
    *,
    token: str,
    step_label: str,
) -> bool:
    """PATCH the submission's webhook_url and confirm it transitioned to active."""
    console.rule(f"[bold]{step_label}")
    updated = _api_request(
        "PATCH",
        api_url,
        f"/submissions/{submission_id}",
        token=token,
        json_body={"webhook_url": deployed_url},
    )
    if updated is None:
        return False
    new_status = updated.get("status")
    console.print(f"[green]✓[/green] status now: [bold]{new_status}[/bold]")
    if new_status != "active":
        console.print(
            f"[yellow]Expected status 'active' after PATCH — got '{new_status}'.[/yellow]\n"
            "Credentials may not be initialized for this submission."
        )
        return False
    return True


def _fire_test_webhook(
    api_url: str,
    submission_id: str,
    *,
    token: str,
    step_label: str,
) -> int:
    """Confirm-prompt + fire a synthetic TEST event + poll for the outcome."""
    console.rule(f"[bold]{step_label}")
    if not questionary.confirm(
        "Send a synthetic TEST event to your handler now?", default=True
    ).ask():
        console.print(
            "[dim]Skipping test webhook. You can fire one later from the portal.[/dim]"
        )
        return 0

    before = _api_get(api_url, f"/submissions/{submission_id}/health", token=token)
    if before is None:
        return 1

    fired = _api_request(
        "POST",
        api_url,
        f"/submissions/{submission_id}/webhook/test",
        token=token,
    )
    if fired is None:
        return 1
    webhook_id = fired.get("webhook_id")
    console.print(f"[green]✓[/green] fired test webhook · id: [bold]{webhook_id}[/bold]")
    console.print(
        Panel(
            f"event_type:    [bold]TEST[/bold]\n"
            f"webhook_id:    {webhook_id}\n"
            f"focal asset:   TICKER:TEST\n"
            f"signed with:   COMPETITION_WEBHOOK_SECRET",
            title="event sent",
            border_style="cyan",
        )
    )

    return _await_delivery(api_url, submission_id, token=token, before=before)


_HEALTH_POLL_INTERVAL = 1.0
_HEALTH_POLL_TIMEOUT = 30.0
_OUTCOME_FIELDS = ("webhook_n_2xx", "webhook_n_4xx", "webhook_n_5xx", "webhook_n_timeout")


def _await_delivery(
    api_url: str, submission_id: str, *, token: str, before: dict[str, Any]
) -> int:
    """Poll SubmissionHealth until the delivery lands, then render the outcome."""
    before_ts = before.get("webhook_last_delivery_at")
    deadline = time.monotonic() + _HEALTH_POLL_TIMEOUT
    with console.status("[dim]waiting for delivery…[/dim]", spinner="dots"):
        while time.monotonic() < deadline:
            time.sleep(_HEALTH_POLL_INTERVAL)
            after = _api_get(api_url, f"/submissions/{submission_id}/health", token=token)
            if after is None:
                return 1
            after_ts = after.get("webhook_last_delivery_at")
            if after_ts and after_ts != before_ts:
                return _render_delivery_outcome(before, after)
    console.print(
        Panel(
            f"No delivery recorded within {int(_HEALTH_POLL_TIMEOUT)}s.\n"
            "The deliver Lambda may be backed up, or your handler may have timed out.\n"
            "Check the portal for delivery status, or re-fire from the portal.",
            title="⏱️ Timed out",
            border_style="yellow",
        )
    )
    return 1


def _render_delivery_outcome(before: dict[str, Any], after: dict[str, Any]) -> int:
    """Diff the four outcome counters and render a panel; return 0 if 2xx."""
    diffs = {f: int(after.get(f, 0)) - int(before.get(f, 0)) for f in _OUTCOME_FIELDS}
    bucket = next((f for f, d in diffs.items() if d > 0), None)
    delivered_at = after.get("webhook_last_delivery_at", "")

    if bucket == "webhook_n_2xx":
        console.print(
            Panel(
                f"Your handler accepted the event (2xx) at {delivered_at}.",
                title="✅ Delivered",
                border_style="green",
            )
        )
        return 0

    explanations = {
        "webhook_n_4xx": (
            "Your handler rejected the event (4xx). The API treats this as terminal "
            "and won't retry. Most common cause: signature verification failed — the "
            "COMPETITION_WEBHOOK_SECRET in your Lambda environment doesn't match the "
            "one the API is signing with."
        ),
        "webhook_n_5xx": (
            "Your handler crashed while processing the event (5xx). The API will retry. "
            "Check your Lambda logs for a Python traceback."
        ),
        "webhook_n_timeout": (
            "Your handler didn't respond in time (timeout). "
            "Check that the Lambda is reachable and that startup isn't too slow."
        ),
    }
    msg = explanations.get(
        bucket,
        "Delivery was recorded but no outcome counter advanced. This is unexpected.",
    )
    console.print(
        Panel(
            f"{msg}\n\ndelivered at: {delivered_at}",
            title="❌ Delivery failed",
            border_style="red",
        )
    )
    return 1


# ---------------------------------------------------------------------------
# OAuth code flow with PKCE + localhost callback
# ---------------------------------------------------------------------------


def _get_id_token(*, force_reauth: bool) -> str:
    """Return a fresh ID token, using the cached refresh token when possible."""
    client_id = os.environ.get("COMPETITION_COGNITO_CLIENT_ID", DEFAULT_COGNITO_CLIENT_ID)
    cognito_domain = os.environ.get("COMPETITION_COGNITO_DOMAIN", DEFAULT_COGNITO_DOMAIN).rstrip("/")

    if not force_reauth:
        cached = _load_token_cache()
        if cached and cached.get("client_id") == client_id:
            refresh = cached.get("refresh_token")
            if refresh:
                try:
                    return _refresh_id_token(cognito_domain, client_id, refresh)
                except _SetupError as exc:
                    console.print(
                        f"[dim]Cached refresh token rejected ({exc}); opening browser…[/dim]"
                    )

    return _interactive_login(cognito_domain, client_id)


def _interactive_login(cognito_domain: str, client_id: str) -> str:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(16))
    redirect_uri = f"http://localhost:{CALLBACK_PORT}{CALLBACK_PATH}"

    authorize_url = (
        f"{cognito_domain}/oauth2/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": OAUTH_SCOPES,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )

    code_holder: dict[str, str] = {}
    server = _make_callback_server(state=state, sink=code_holder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        console.print(
            f"Opening browser to sign in…\n"
            f"[dim]If it doesn't open automatically, visit:\n  {authorize_url}[/dim]"
        )
        webbrowser.open(authorize_url, new=1)

        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and "code" not in code_holder and "error" not in code_holder:
            time.sleep(0.1)
    finally:
        server.shutdown()
        thread.join(timeout=2)

    if "error" in code_holder:
        raise _SetupError(f"OAuth error: {code_holder['error']}")
    if "code" not in code_holder:
        raise _SetupError("Timed out waiting for browser sign-in (5 min). Re-run to retry.")

    tokens = _exchange_code(
        cognito_domain=cognito_domain,
        client_id=client_id,
        code=code_holder["code"],
        verifier=verifier,
        redirect_uri=redirect_uri,
    )
    _save_token_cache(client_id=client_id, refresh_token=tokens.get("refresh_token"))
    id_token = tokens.get("id_token")
    if not id_token:
        raise _SetupError(f"No id_token in token response: {tokens}")
    console.print("[green]✓[/green] authenticated via browser")
    return id_token


def _exchange_code(
    *, cognito_domain: str, client_id: str, code: str, verifier: str, redirect_uri: str
) -> dict[str, Any]:
    resp = httpx.post(
        f"{cognito_domain}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise _SetupError(f"Token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


def _refresh_id_token(cognito_domain: str, client_id: str, refresh_token: str) -> str:
    resp = httpx.post(
        f"{cognito_domain}/oauth2/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    if resp.status_code != 200:
        raise _SetupError(f"refresh failed ({resp.status_code})")
    body = resp.json()
    id_token = body.get("id_token")
    if not id_token:
        raise _SetupError("no id_token in refresh response")
    console.print("[green]✓[/green] authenticated via cached refresh token")
    return id_token


def _make_callback_server(*, state: str, sink: dict[str, str]) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return  # silence stdout

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != CALLBACK_PATH:
                self.send_response(404)
                self.end_headers()
                return
            params = urllib.parse.parse_qs(parsed.query)
            if "error" in params:
                sink["error"] = params["error"][0]
                body = _render_callback_page(
                    tone="error",
                    heading="Sign-in failed",
                    message="Return to the terminal and try again.",
                )
            elif params.get("state", [None])[0] != state:
                sink["error"] = "state mismatch"
                body = _render_callback_page(
                    tone="error",
                    heading="Sign-in failed",
                    message="The sign-in request couldn't be verified. Return to the terminal and try again.",
                )
            elif "code" in params:
                sink["code"] = params["code"][0]
                body = _render_callback_page(
                    tone="success",
                    heading="Signed in",
                    message="You can close this tab and return to the terminal.",
                )
            else:
                sink["error"] = "no code in callback"
                body = _render_callback_page(
                    tone="error",
                    heading="Sign-in failed",
                    message="No authorization code returned. Return to the terminal and try again.",
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    try:
        return http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
    except OSError as exc:
        raise _SetupError(
            f"Could not bind localhost:{CALLBACK_PORT} for the OAuth callback "
            f"({exc}). Close whatever is using that port and re-run."
        ) from exc


_ICON_CHECK = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true"><polyline points="5 12.5 10 17.5 19 7.5"></polyline></svg>'
)
_ICON_X = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" '
    'stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" '
    'aria-hidden="true"><line x1="7" y1="7" x2="17" y2="17"></line>'
    '<line x1="17" y1="7" x2="7" y2="17"></line></svg>'
)

_CALLBACK_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{heading} — Explaining Markets</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #ffffff;
  --fg: #171717;
  --surface: #fafaf9;
  --border: #e7e5e4;
  --muted: #78716c;
  --success-bg: #d1fae5;
  --success-fg: #065f46;
  --error-bg: #fee2e2;
  --error-fg: #991b1b;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0a0a0a;
    --fg: #ededed;
    --surface: #1c1917;
    --border: #292524;
    --muted: #a8a29e;
    --success-bg: rgba(6, 78, 59, 0.45);
    --success-fg: #a7f3d0;
    --error-bg: rgba(127, 29, 29, 0.45);
    --error-fg: #fecaca;
  }}
}}
* {{ box-sizing: border-box; }}
html, body {{ height: 100%; }}
body {{
  margin: 0;
  font-family: 'Geist', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  background: var(--bg);
  color: var(--fg);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 24px;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}}
.stack {{
  width: 100%;
  max-width: 420px;
}}
.wordmark {{
  font-size: 13px;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--fg);
  text-align: center;
  margin-bottom: 24px;
}}
.card {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 40px 32px;
  text-align: center;
}}
.icon-wrap {{
  width: 44px;
  height: 44px;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 20px;
}}
.icon-wrap.success {{ background: var(--success-bg); color: var(--success-fg); }}
.icon-wrap.error   {{ background: var(--error-bg);   color: var(--error-fg);   }}
.icon-wrap svg {{ width: 24px; height: 24px; }}
h1 {{
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.015em;
  margin: 0 0 8px 0;
  color: var(--fg);
}}
p {{
  font-size: 14px;
  line-height: 1.5;
  color: var(--muted);
  margin: 0;
}}
</style>
</head>
<body>
<div class="stack">
  <div class="wordmark">Explaining Markets</div>
  <main class="card">
    <div class="icon-wrap {tone}">{icon}</div>
    <h1>{heading}</h1>
    <p>{message}</p>
  </main>
</div>
</body>
</html>
"""


def _render_callback_page(*, tone: str, heading: str, message: str) -> bytes:
    icon = _ICON_CHECK if tone == "success" else _ICON_X
    return _CALLBACK_PAGE.format(
        tone=tone,
        icon=icon,
        heading=heading,
        message=message,
    ).encode("utf-8")


def _load_token_cache() -> dict[str, Any] | None:
    if not TOKEN_CACHE_PATH.exists():
        return None
    try:
        return json.loads(TOKEN_CACHE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_token_cache(*, client_id: str, refresh_token: str | None) -> None:
    if not refresh_token:
        return
    TOKEN_CACHE_PATH.write_text(
        json.dumps({"client_id": client_id, "refresh_token": refresh_token}, indent=2) + "\n"
    )
    try:
        os.chmod(TOKEN_CACHE_PATH, 0o600)
    except OSError:
        pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _api_get(api_url: str, path: str, *, token: str) -> dict[str, Any] | None:
    return _api_request("GET", api_url, path, token=token)


def _get_me_or_redeem(api_url: str, *, token: str) -> dict[str, Any] | None:
    """GET /me; on INVITE_REQUIRED, prompt for a code and POST /me/redeem.

    The API may return 403 with detail.code == "INVITE_REQUIRED" on
    invite-gated stages (currently the beta API). In that case, prompt
    for an invite code, redeem it, then return the freshly-provisioned
    profile.
    """
    try:
        resp = httpx.get(
            f"{api_url}/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        console.print(f"[red]Request to /me failed: {exc}[/red]")
        return None
    if resp.status_code < 300:
        return resp.json()

    detail = _safe_json(resp).get("detail")
    if resp.status_code == 403 and isinstance(detail, dict) and detail.get("code") == "INVITE_REQUIRED":
        return _redeem_invite_then_get_me(api_url, token=token)

    console.print(
        f"[red]GET /me → {resp.status_code}[/red]: {detail if detail else resp.text}"
    )
    return None


def _redeem_invite_then_get_me(api_url: str, *, token: str) -> dict[str, Any] | None:
    console.print(
        "[yellow]This stage of the competition is invite-gated.[/yellow]\n"
        "Redeem your invite code to provision your account."
    )
    for attempt in range(3):
        code = questionary.text(
            "Invite code:",
            validate=lambda v: bool(v.strip()) or "Required.",
        ).ask()
        if not code:
            console.print("[red]Aborted.[/red]")
            return None
        try:
            resp = httpx.post(
                f"{api_url}/me/redeem",
                headers={"Authorization": f"Bearer {token}"},
                json={"code": code.strip()},
                timeout=30.0,
            )
        except httpx.HTTPError as exc:
            console.print(f"[red]Request to /me/redeem failed: {exc}[/red]")
            return None
        if resp.status_code < 300:
            console.print(f"[green]✓[/green] invite redeemed")
            return _api_get(api_url, "/me", token=token)
        if resp.status_code == 404:
            remaining = 2 - attempt
            if remaining > 0:
                console.print(
                    f"[red]Code not recognized, expired, or exhausted "
                    f"({remaining} attempt{'s' if remaining > 1 else ''} left).[/red]"
                )
                continue
            console.print("[red]Code not recognized. Giving up.[/red]")
            return None
        if resp.status_code == 409:
            console.print(
                "[yellow]Account already provisioned — re-fetching profile.[/yellow]"
            )
            return _api_get(api_url, "/me", token=token)
        detail = _safe_json(resp).get("detail")
        console.print(
            f"[red]POST /me/redeem → {resp.status_code}[/red]: "
            f"{detail if detail else resp.text}"
        )
        return None
    return None


def _api_request(
    method: str,
    api_url: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    try:
        resp = httpx.request(
            method,
            f"{api_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=json_body,
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        console.print(f"[red]Request to {path} failed: {exc}[/red]")
        return None
    if resp.status_code >= 400:
        detail = _safe_json(resp).get("detail")
        console.print(
            f"[red]{method} {path} → {resp.status_code}[/red]: "
            f"{detail if detail else resp.text}"
        )
        return None
    if not resp.content:
        return {}
    return resp.json()


def _create_submission(api_url: str, token: str) -> tuple[str | None, str | None]:
    while True:
        public_name = questionary.text(
            "Public name for your submission:",
            validate=lambda v: bool(v.strip()) or "Required.",
        ).ask()
        if not public_name:
            return None, None
        resp = httpx.post(
            f"{api_url}/submissions",
            headers={"Authorization": f"Bearer {token}"},
            json={"public_name": public_name.strip()},
            timeout=30.0,
        )
        if resp.status_code == 409:
            console.print("[yellow]That name is taken. Try another.[/yellow]")
            continue
        if resp.status_code >= 400:
            detail = _safe_json(resp).get("detail")
            if isinstance(detail, dict) and detail.get("code") == "PROFILE_INCOMPLETE":
                console.print(
                    f"[yellow]Your profile isn't complete yet — finish it in the portal:[/yellow]\n"
                    f"  [bold]{ONBOARDING_URL}[/bold]"
                )
            else:
                console.print(
                    f"[red]POST /submissions → {resp.status_code}[/red]: "
                    f"{detail if detail else resp.text}"
                )
            return None, None
        body = resp.json()
        return body["submission_id"], body.get("status")


def _initialize_credentials(api_url: str, submission_id: str, token: str) -> dict[str, str]:
    resp = httpx.post(
        f"{api_url}/submissions/{submission_id}/credentials/initialize",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    if resp.status_code == 409:
        raise _SetupError(
            "Credentials already initialized for this submission. "
            "Rotate them in the portal, or create a new submission with --fresh."
        )
    if resp.status_code >= 400:
        detail = _safe_json(resp).get("detail")
        raise _SetupError(
            f"credentials/initialize → {resp.status_code}: {detail if detail else resp.text}"
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _persist_secrets(*, submission_id: str, api_key: str, signing_secret: str, api_url: str) -> None:
    ENV_PATH.touch(exist_ok=True)
    set_key(str(ENV_PATH), "COMPETITION_SUBMISSION_ID", submission_id, quote_mode="never")
    set_key(str(ENV_PATH), "COMPETITION_API_KEY", api_key, quote_mode="never")
    set_key(str(ENV_PATH), "COMPETITION_WEBHOOK_SECRET", signing_secret, quote_mode="never")
    set_key(str(ENV_PATH), "COMPETITION_API_URL", api_url, quote_mode="never")

    if not _is_chalice_path():
        return

    config = json.loads(CHALICE_CONFIG_PATH.read_text())
    ev = config["stages"]["dev"].setdefault("environment_variables", {})
    ev["COMPETITION_API_URL"] = api_url
    ev["COMPETITION_SUBMISSION_ID"] = submission_id
    ev["COMPETITION_API_KEY"] = api_key
    ev["COMPETITION_WEBHOOK_SECRET"] = signing_secret
    CHALICE_CONFIG_PATH.write_text(json.dumps(config, indent=4) + "\n")


def _read_deployed_webhook_url() -> str | None:
    """Resolve the deployed handler's URL.

    Precedence: explicit ``WEBHOOK_URL`` env var (escape hatch for non-Chalice
    deployments) → ``.chalice/deployed/dev.json`` (the default Chalice path).
    """
    explicit = os.environ.get("WEBHOOK_URL", "").strip()
    if explicit:
        return explicit
    if not CHALICE_DEPLOYED_PATH.exists():
        return None
    try:
        data = json.loads(CHALICE_DEPLOYED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for resource in data.get("resources", []):
        url = resource.get("rest_api_url")
        if url:
            return url.rstrip("/") + "/webhook"
    return None


def _is_chalice_path() -> bool:
    """True when the user is on the default Chalice deployment path.

    The non-Chalice path is opted into by setting ``WEBHOOK_URL`` in ``.env``.
    """
    return not os.environ.get("WEBHOOK_URL", "").strip()


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


class _SetupError(Exception):
    pass


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


if __name__ == "__main__":
    sys.exit(main())
