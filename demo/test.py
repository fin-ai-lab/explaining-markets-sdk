"""Orchestrator for `python -m demo` — the local mock-competition test harness.

Steps:

  1. Load .env (OPENAI_API_KEY + COMPETITION_WEBHOOK_SECRET).
  2. Pick a fixture via the TUI picker.
  3. Stand up the catcher on :9999.
  4. Spawn `chalice local` on :8000 with COMPETITION_API_URL pointed at the catcher.
  5. Mint and sign a WebhookPayload, POST it to :8000/webhook.
  6. Wait for the handler to respond and the catcher to capture (or reject)
     the resulting submission.
  7. Render the final panel.

Local-only. The deployed Lambda has COMPETITION_API_URL baked into its env
and can't reach localhost — for testing your deployed handler, use the
competition's POST /v1/me/webhook/test instead.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.live import Live

from . import catcher, sign, tui
from .schema import WebhookPayload
from .tui import RunState, Stage, console, render_state

FIXTURES_DIR = Path(__file__).parent / "fixtures"
CHALICE_PORT = 8000
CATCHER_PORT = 9999
HANDLER_TIMEOUT_S = 60


def main() -> int:
    load_dotenv()

    secret = os.environ.get("COMPETITION_WEBHOOK_SECRET")
    if not secret:
        console.print("[red]COMPETITION_WEBHOOK_SECRET not set in .env[/red]")
        return 1
    if not os.environ.get("OPENAI_API_KEY"):
        console.print("[red]OPENAI_API_KEY not set in .env[/red]")
        return 1

    if _port_in_use(CHALICE_PORT):
        console.print(
            f"[red]Port {CHALICE_PORT} is already in use.[/red] "
            "Stop the process using it (likely a leftover `chalice local`) and re-run."
        )
        return 1
    if _port_in_use(CATCHER_PORT):
        console.print(f"[red]Port {CATCHER_PORT} is already in use.[/red] Free it and re-run.")
        return 1

    fixture = tui.pick_fixture(FIXTURES_DIR)

    state = RunState(title=f"{fixture.ticker}  ·  earnings  ·  {fixture.date}")
    for label in (
        f"catcher up on :{CATCHER_PORT}",
        f"chalice local up on :{CHALICE_PORT}",
        "webhook signed",
        "delivering POST → handler",
        "handler responded",
        "submission received by catcher",
        "submission validated",
    ):
        state.add(label)

    chalice_proc: subprocess.Popen[bytes] | None = None
    server = None
    config_path = Path(__file__).parent.parent / ".chalice" / "config.json"
    original_config = config_path.read_text() if config_path.exists() else None
    try:
        with Live(render_state(state), refresh_per_second=10, console=console) as live:
            # 1. catcher
            state.set(f"catcher up on :{CATCHER_PORT}", Stage.ACTIVE)
            live.update(render_state(state))
            server = catcher.serve_in_background()
            _wait_for(f"http://127.0.0.1:{CATCHER_PORT}/healthz", timeout=5.0)
            state.set(f"catcher up on :{CATCHER_PORT}", Stage.DONE)
            live.update(render_state(state))

            # 2. chalice local
            # Chalice's `local` reads environment_variables from .chalice/config.json
            # and that wins over parent env. Rewrite the config inline with our
            # real values so the handler sees them, restore on exit.
            state.set(f"chalice local up on :{CHALICE_PORT}", Stage.ACTIVE)
            live.update(render_state(state))
            _patch_chalice_config(
                config_path,
                api_url=f"http://127.0.0.1:{CATCHER_PORT}/v1",
                api_key=os.environ.get("COMPETITION_API_KEY", "unused-locally"),
                webhook_secret=secret,
                openai_key=os.environ["OPENAI_API_KEY"],
            )
            chalice_proc = subprocess.Popen(
                ["chalice", "local", "--port", str(CHALICE_PORT), "--stage", "dev"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=Path(__file__).parent.parent,
            )
            if not _wait_for_port(CHALICE_PORT, timeout=15.0):
                state.set(f"chalice local up on :{CHALICE_PORT}", Stage.FAILED)
                live.update(render_state(state))
                tui.render_final(state)
                return 1
            state.set(f"chalice local up on :{CHALICE_PORT}", Stage.DONE)
            live.update(render_state(state))

            # 3. arm catcher and sign
            state.set("webhook signed", Stage.ACTIVE)
            live.update(render_state(state))
            event_dt = _parse_fixture_date(fixture.date)
            payload: WebhookPayload = sign.build_payload(ticker=fixture.ticker, event_datetime=event_dt)
            catcher.arm(
                fixture={"ticker": fixture.ticker, "summary": fixture.summary},
                event_id=str(payload.event_id),
                focal_assets=[a.identifier_value for a in payload.focal_assets],
            )
            raw_body = sign.serialize(payload)
            timestamp = int(time.time())
            signature_header = sign.sign(
                secret=secret,
                webhook_id=payload.id,
                timestamp=timestamp,
                raw_body=raw_body,
            )
            state.webhook_id = payload.id
            state.set("webhook signed", Stage.DONE)
            live.update(render_state(state))

            # 4. deliver
            state.set("delivering POST → handler", Stage.ACTIVE)
            live.update(render_state(state))
            try:
                resp = httpx.post(
                    f"http://127.0.0.1:{CHALICE_PORT}/webhook",
                    content=raw_body,
                    headers={
                        "Content-Type": "application/json",
                        "Webhook-Id": payload.id,
                        "Webhook-Timestamp": str(timestamp),
                        "Webhook-Signature": signature_header,
                    },
                    timeout=HANDLER_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                state.set("delivering POST → handler", Stage.FAILED)
                state.set("handler responded", Stage.FAILED)
                live.update(render_state(state))
                console.print(f"[red]webhook delivery failed:[/red] {exc}")
                tui.render_final(state)
                return 1
            state.set("delivering POST → handler", Stage.DONE)
            state.handler_status = resp.status_code
            handler_ok = 200 <= resp.status_code < 300
            state.set("handler responded", Stage.DONE if handler_ok else Stage.FAILED)
            live.update(render_state(state))
            if not handler_ok:
                console.print(f"[red]handler returned {resp.status_code}:[/red] {resp.text}")
                tui.render_final(state)
                return 1

            # 5. wait for catcher capture
            got_submission = catcher.submission_event.wait(timeout=10.0)
            if not got_submission:
                state.set("submission received by catcher", Stage.FAILED)
                live.update(render_state(state))
                console.print("[red]handler returned 200 but no submission reached the catcher[/red]")
                tui.render_final(state)
                return 1
            state.set("submission received by catcher", Stage.DONE)
            captured = catcher.captured()
            rejection = catcher.rejection()
            if captured is not None:
                state.captured_body = captured
                state.set("submission validated", Stage.DONE)
            else:
                state.rejection = rejection
                state.set("submission validated", Stage.FAILED)
            live.update(render_state(state))

        tui.render_final(state)
        return 0 if state.captured_body else 1
    finally:
        if chalice_proc is not None:
            chalice_proc.terminate()
            try:
                chalice_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chalice_proc.kill()
        if server is not None:
            server.shutdown()
        if original_config is not None:
            config_path.write_text(original_config)


def _patch_chalice_config(
    path: Path, *, api_url: str, api_key: str, webhook_secret: str, openai_key: str
) -> None:
    config = json.loads(path.read_text())
    ev = config["stages"]["dev"].setdefault("environment_variables", {})
    ev["COMPETITION_API_URL"] = api_url
    ev["COMPETITION_API_KEY"] = api_key
    ev["COMPETITION_WEBHOOK_SECRET"] = webhook_secret
    ev["OPENAI_API_KEY"] = openai_key
    path.write_text(json.dumps(config, indent=4) + "\n")


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_in_use(port):
            return True
        time.sleep(0.1)
    return False


def _wait_for(url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {url}")


def _parse_fixture_date(date: str) -> datetime:
    """Parse YYYY-MM-DD; fall back to now() for `(custom)`."""
    try:
        return datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return datetime.now(UTC)


if __name__ == "__main__":
    sys.exit(main())
