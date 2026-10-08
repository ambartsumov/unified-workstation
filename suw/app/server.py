"""Loopback HTTP server behind the application window.

Security model — the window is a local program, not a web site:
  * listens on 127.0.0.1 only, on a port chosen by the system;
  * every request must carry the per-session token (header for the API, query for the first
    page load and for downloads); the token is random and never stored;
  * the Host header must name this loopback address (defeats DNS rebinding) and a cross-site
    Origin is refused;
  * a strict Content-Security-Policy: no remote scripts, styles, fonts or connections.
"""

from __future__ import annotations

import json
import mimetypes
import secrets as pysecrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..core import events
from .backend import Backend, Problem

STATIC = Path(__file__).resolve().parent / "static"
MAX_BODY = 8 * 1024 * 1024
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


def static_root() -> Path:
    import sys

    frozen = getattr(sys, "_MEIPASS", "")
    bundled = Path(frozen) / "suw" / "app" / "static" if frozen else None
    return bundled if bundled and bundled.is_dir() else STATIC


class App:
    def __init__(self, backend: Backend, port: int = 0):
        self.backend = backend
        self.token = pysecrets.token_urlsafe(32)
        self.lock = threading.Lock()
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "UnifiedWorkstation"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # the window's traffic is not worth logging
                pass

            # ── helpers ─────────────────────────────────────────────────────
            def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Content-Security-Policy", CSP)
                for key, value in (extra or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, data) -> None:
                self._send(status, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

            def _local(self) -> bool:
                host = self.headers.get("Host", "")
                port = self.server.server_address[1]
                if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                    return False
                origin = self.headers.get("Origin")
                return origin in (None, f"http://127.0.0.1:{port}", f"http://localhost:{port}")

            def _token_ok(self, value: str) -> bool:
                return bool(value) and pysecrets.compare_digest(value, app.token)

            # ── routes ──────────────────────────────────────────────────────
            def do_GET(self) -> None:
                if not self._local():
                    return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/":
                    if not self._token_ok((query.get("k") or [""])[0]):
                        return self._send(HTTPStatus.FORBIDDEN, b"Open the application from its launcher.", "text/plain; charset=utf-8")
                    html = (static_root() / "index.html").read_text(encoding="utf-8").replace("__SESSION_TOKEN__", app.token)
                    return self._send(HTTPStatus.OK, html.encode(), "text/html; charset=utf-8")
                if url.path == "/download":
                    if not self._token_ok((query.get("k") or [""])[0]):
                        return self._send(HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
                    item = app.backend.take_download((query.get("id") or [""])[0])
                    if not item:
                        return self._send(HTTPStatus.NOT_FOUND, b"expired", "text/plain")
                    name, blob = item
                    safe = "".join(ch for ch in name if ch.isalnum() or ch in "._-")
                    return self._send(HTTPStatus.OK, blob, "application/zip", {"Content-Disposition": f'attachment; filename="{safe}"'})
                if url.path.startswith("/static/"):
                    root = static_root().resolve()
                    target = (root / url.path[len("/static/"):]).resolve()
                    if root not in target.parents or not target.is_file():
                        return self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")
                    kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
                        kind += "; charset=utf-8"
                    return self._send(HTTPStatus.OK, target.read_bytes(), kind)
                self._send(HTTPStatus.NOT_FOUND, b"not found", "text/plain")

            def do_POST(self) -> None:
                if not self._local() or not self._token_ok(self.headers.get("X-Session-Token", "")):
                    return self._json(HTTPStatus.FORBIDDEN, {"problem": {"code": "forbidden", "actions": ["close"], "detail": "", "params": {}}})
                if urlparse(self.path).path != "/api":
                    return self._json(HTTPStatus.NOT_FOUND, {"problem": {"code": "unknown_request", "actions": ["close"], "detail": "", "params": {}}})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 <= length <= MAX_BODY:
                        raise ValueError("body too large")
                    request = json.loads(self.rfile.read(length) or b"{}")
                    method, params = str(request["method"]), request.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("params")
                except (ValueError, KeyError, TypeError):
                    return self._json(HTTPStatus.BAD_REQUEST, {"problem": {"code": "unknown_request", "actions": ["close"], "detail": "", "params": {}}})
                try:
                    with app.lock:  # one change at a time: the window is single-user
                        result = app.backend.call(method, params)
                    self._json(HTTPStatus.OK, {"result": result})
                except Problem as problem:
                    self._json(HTTPStatus.OK, {"problem": problem.as_dict()})
                except TypeError as exc:
                    self._json(HTTPStatus.OK, {"problem": Problem("unknown_request", ["close"], detail=str(exc)).as_dict()})
                except Exception as exc:  # never a stack trace on screen; the detail goes to the event log
                    events.emit("app.error", f"{method}: {type(exc).__name__}: {exc}", "error")
                    self._json(HTTPStatus.OK, {"problem": Problem("unexpected", ["retry", "details"], detail=f"{type(exc).__name__}: {exc}").as_dict()})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.token}"

    def serve(self) -> None:
        self.httpd.serve_forever(poll_interval=0.5)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve, name="uw-app", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
