"""A local page whose buttons actually change a role's status.

The file `jobhunt digest` writes is inert: opened over file:// it has no way
to reach the database. This serves the same page from a small local server so
the buttons can write, which turns triage into clicking rather than typing an
id into a terminal.

Bound to the loopback interface, so nothing outside the machine can reach it.
Every request must also carry a token minted at startup and embedded in the
page: without it, any website open in the same browser could post to this
server, and the browser would attach no origin the server could refuse.
"""

from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .digest import collect
from .render import render_digest
from .store import USER_STATUSES, Store
from .util import now_iso

MAX_BODY = 8192


class DigestServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, db_path: Path, *, show_all: bool = False):
        self.db_path = Path(db_path)
        self.show_all = show_all
        self.token = secrets.token_urlsafe(24)
        self._lock = threading.Lock()
        super().__init__(address, _Handler)
        # Opening the page is looking at it, so it moves the "last looked"
        # marker just as `jobhunt digest` does. The previous value is kept for
        # the life of the server: a reload should not strip the New badges
        # off roles you have not got to yet.
        with Store(self.db_path) as store:
            self.since = store.meta_get("last_digest_at")
            store.meta_set("last_digest_at", now_iso())

    def render(self) -> bytes:
        """Build the page fresh, so a reload reflects what has been marked."""
        with self._lock, Store(self.db_path) as store:
            rows, subtitle, _ = collect(
                store, show_all=self.show_all, last_digest=self.since
            )
            html = render_digest(
                rows, subtitle=subtitle, since=self.since, api_token=self.token
            )
        return html.encode("utf-8")

    def set_status(self, uid: str, status: str) -> tuple[bool, str]:
        if status not in USER_STATUSES:
            return False, f"unknown status {status!r}"
        with self._lock, Store(self.db_path) as store:
            matches = store.resolve(uid)
            if not matches:
                return False, "no such role"
            if len(matches) > 1:
                return False, "ambiguous id"
            store.set_status(matches[0]["uid"], status)
        return True, status


class _Handler(BaseHTTPRequestHandler):
    server_version = "jobhunt"

    # -- helpers --------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should be cached: the page reflects live state.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    # -- routes ---------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, self.server.render(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.split("?", 1)[0] != "/api/status":
            self._json(404, {"error": "not found"})
            return

        offered = self.headers.get("X-Jobhunt-Token") or ""
        if not secrets.compare_digest(offered.encode(), self.server.token.encode()):
            self._json(403, {"error": "bad or missing token"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._json(400, {"error": "bad content length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(400, {"error": "bad request size"})
            return

        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, OSError):
            self._json(400, {"error": "body was not JSON"})
            return

        uid = str(payload.get("uid") or "")
        status = str(payload.get("status") or "")
        if not uid:
            self._json(400, {"error": "no uid given"})
            return

        ok, detail = self.server.set_status(uid, status)
        self._json(200 if ok else 400, {"ok": ok, "detail": detail, "uid": uid})

    def log_message(self, *args) -> None:
        pass  # the terminal is for the user, not a request log


def serve(db_path: Path, host: str = "127.0.0.1", port: int = 8765, show_all: bool = False):
    """Start the server. The caller owns the loop, so it can print first."""
    return DigestServer((host, port), db_path, show_all=show_all)
