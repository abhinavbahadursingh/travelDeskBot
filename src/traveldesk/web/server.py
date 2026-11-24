"""JSON HTTP API for the /book page (stdlib only). Run: python -m traveldesk.web

  GET  /api/book/travellers            people in travellers.json
  POST /api/book/searches              {user, text}        → 202 {id, status: "running"}
  GET  /api/book/searches/<id>         status, picks and all bookable flights
  POST /api/book/requests              {user, search, offer}           → 201 {booking_id}  (Slack card)
  POST /api/book/bookings              {user, search, offer, details?} → 201 {booking_id}  (confirm here)
  POST /api/book/requests/<id>/confirm {user, details?}                → 200 {booking_id, status}

A 409 with `needs` means passenger details are missing: `needs.fields` is the form
to show; send the typed values back as `details`. A 422 has `field_errors`.

Binds to 127.0.0.1 by default: the page reaches it through the UI's dev/preview
server proxy. There is no login, so don't expose it beyond a trusted machine.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from traveldesk.web import booking
from traveldesk.web.searches import SearchStore

log = logging.getLogger("traveldesk.web")

_MAX_BODY = 16 * 1024
_MAX_TEXT = 300

Response = tuple[int, dict | list]


class Api:
    """Routes → handlers. Handlers take (match, body) and return (status, json)."""

    def __init__(self, searches: SearchStore | None = None, slack_client=None):
        self.searches = searches or SearchStore()
        self.slack_client = slack_client
        self.routes: list[tuple[str, re.Pattern, Callable[..., Response]]] = [
            ("GET", re.compile(r"^/api/book/travellers$"), self.list_travellers),
            ("POST", re.compile(r"^/api/book/searches$"), self.start_search),
            ("GET", re.compile(r"^/api/book/searches/([\w-]+)$"), self.get_search),
            ("POST", re.compile(r"^/api/book/requests$"), self.send_request),
            ("POST", re.compile(r"^/api/book/bookings$"), self.book_here),
            ("POST", re.compile(r"^/api/book/requests/(\d+)/confirm$"), self.confirm_request),
        ]

    def handle(self, method: str, path: str, body: dict) -> Response:
        for m, pattern, fn in self.routes:
            if (match := pattern.match(path)) and m == method:
                try:
                    return fn(match, body)
                except booking.BookingError as e:
                    return e.status, {"error": e.message, **e.extra}
        return 404, {"error": "Not found"}

    def list_travellers(self, _m, _body) -> Response:
        return 200, booking.travellers()

    def start_search(self, _m, body) -> Response:
        user, text = str(body.get("user") or ""), str(body.get("text") or "").strip()
        if not text or len(text) > _MAX_TEXT:
            return 400, {"error": f"Describe the trip in 1–{_MAX_TEXT} characters."}
        booking.require_traveller(user)
        return 202, self.searches.start(user, text).to_json()

    def get_search(self, m, _body) -> Response:
        job = self.searches.get(m.group(1))
        return (200, job.to_json()) if job else (404, {"error": "Search not found or expired. Search again."})

    def send_request(self, _m, body) -> Response:
        job = self.searches.get(str(body.get("search") or ""))
        if not job:
            return 404, {"error": "Search not found or expired. Search again."}
        row_id = booking.send_to_slack(
            job, str(body.get("offer") or ""), str(body.get("user") or ""), self.slack_client
        )
        return 201, {"booking_id": row_id}

    def book_here(self, _m, body) -> Response:
        job = self.searches.get(str(body.get("search") or ""))
        if not job:
            return 404, {"error": "Search not found or expired. Search again."}
        user, offer = str(body.get("user") or ""), str(body.get("offer") or "")
        return 201, {
            "booking_id": booking.confirm_and_book(job, offer, user, _details(body), self.slack_client)
        }

    def confirm_request(self, m, body) -> Response:
        return 200, booking.confirm_on_web(
            int(m.group(1)), str(body.get("user") or ""), _details(body), self.slack_client
        )


def _details(body: dict) -> dict | None:
    """Typed passenger details: strings only."""
    d = body.get("details")
    return {str(k): v for k, v in d.items() if isinstance(v, str)} if isinstance(d, dict) else None


def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            body: dict = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > _MAX_BODY:
                    return self._send(413, {"error": "Request too large"})
                try:
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "Invalid JSON"})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "Expected a JSON object"})
            try:
                status, payload = api.handle(method, self.path.split("?", 1)[0], body)
            except Exception:  # noqa: BLE001 — never leak a traceback to the page
                log.exception("web API %s %s failed", method, self.path)
                status, payload = 500, {"error": "Something went wrong. Check the web service log."}
            self._send(status, payload)

        def do_GET(self):  # noqa: N802 — http.server naming
            self._dispatch("GET")

        def do_POST(self):  # noqa: N802
            self._dispatch("POST")

        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler


def main() -> None:
    host = os.environ.get("TRAVELDESK_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("TRAVELDESK_WEB_PORT", "8787"))
    server = ThreadingHTTPServer((host, port), make_handler(Api()))
    log.info("TravelDesk web service on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
