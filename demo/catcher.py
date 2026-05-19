"""Local mock-competition catcher.

Stands up an http.server on localhost:9999 with three routes:

    GET  /healthz                  → 200, used by orchestrator to know we're up
    GET  /summary/{anything}       → returns the active fixture's JSON
    POST /v1/predictions           → validates PredictionSubmission and captures it

State is module-level — one round-trip in flight at a time. The orchestrator
calls `arm()` before driving the webhook to set the active fixture +
event_id, then waits on `captured_event` for the submission to land.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from pydantic import ValidationError

from .schema import PredictionSubmission

PORT = 9999

_state: dict[str, Any] = {
    "fixture": None,           # dict — body to serve from /summary/*
    "event_id": None,          # str — the UUID we expect the submission to carry
    "focal_assets": set(),     # set[str] — allowed identifier_value
    "captured": None,          # dict — the parsed PredictionSubmission once received
    "rejection": None,         # dict — populated if validation rejected the submission
}
submission_event = threading.Event()  # set when any POST /v1/predictions arrives, valid or not


def arm(*, fixture: dict, event_id: str, focal_assets: list[str]) -> None:
    """Configure the catcher for one round-trip."""
    _state["fixture"] = fixture
    _state["event_id"] = event_id
    _state["focal_assets"] = set(focal_assets)
    _state["captured"] = None
    _state["rejection"] = None
    submission_event.clear()


def captured() -> dict | None:
    return _state["captured"]


def rejection() -> dict | None:
    return _state["rejection"]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence default stderr access log so it doesn't clutter the TUI.
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send(200, {"ok": True})
            return
        if self.path.startswith("/summary/"):
            fixture = _state["fixture"]
            if fixture is None:
                self._send(404, {"error": "catcher not armed"})
                return
            self._send(200, fixture)
            return
        self._send(404, {"error": "not found", "path": self.path})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/predictions":
            self._send(404, {"error": "not found", "path": self.path})
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        status, resp = _validate(body)
        self._send(status, resp)
        submission_event.set()

    def _send(self, status: int, body: dict) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _validate(body: bytes) -> tuple[int, dict]:
    try:
        sub = PredictionSubmission.model_validate_json(body)
    except ValidationError as exc:
        rej = {"error": "schema_invalid", "detail": exc.errors(include_url=False)}
        _state["rejection"] = rej
        return 400, rej

    if str(sub.event_id) != _state["event_id"]:
        rej = {
            "error": "event_id_mismatch",
            "expected": _state["event_id"],
            "got": str(sub.event_id),
        }
        _state["rejection"] = rej
        return 400, rej

    invalid = [p.identifier_value for p in sub.predictions if p.identifier_value not in _state["focal_assets"]]
    if invalid:
        rej = {
            "error": "identifier_value_not_in_focal_assets",
            "invalid": invalid,
            "allowed": sorted(_state["focal_assets"]),
        }
        _state["rejection"] = rej
        return 400, rej

    _state["captured"] = sub.model_dump(mode="json")
    return 201, {"accepted": True, "event_id": str(sub.event_id)}


def serve_in_background() -> HTTPServer:
    """Start the catcher on a daemon thread. Returns the server so callers can shut it down."""
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="catcher").start()
    return server
