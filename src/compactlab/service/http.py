"""The local HTTP face of `compactlab serve` (stdlib only).

Bound to loopback unless explicitly told otherwise. GET routes are read only. The one write, POST
/api/recalibrate, needs the service token (Authorization: Bearer <token>), and every request must name a loopback
Host, so a web page elsewhere cannot reach the service through DNS rebinding or a cross-site form.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..report.html import render_html
from ..sim.engine import reselect
from ..sim.select import PROFILE_NAMES
from .core import Calibrator

log = logging.getLogger("compactlab.service")
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}
MAX_BODY = 4096


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_bind(host: str, unsafe: bool) -> None:
    if not is_loopback(host) and not unsafe:
        raise PermissionError(f"refusing to bind {host}: the service is localhost-only (use --unsafe-bind to override)")


def load_token(path: Path) -> str:
    """The service token, created once (0600) and reused across restarts."""
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(32)
    path.write_text(tok)
    path.chmod(0o600)
    return tok


def _host_ok(header: str | None) -> bool:
    if not header:
        return False
    h = header.strip()
    host = h[1 : h.index("]")] if h.startswith("[") else h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    return is_loopback(host)


def make_server(cal: Calibrator, host: str, port: int, token: str, unsafe_bind: bool = False) -> ThreadingHTTPServer:
    check_bind(host, unsafe_bind)

    class Handler(BaseHTTPRequestHandler):
        server_version = "compactlab"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:
            log.debug("%s %s", self.address_string(), format % args)

        def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj: Any) -> None:
            self._send(code, json.dumps(obj).encode())

        def _guard(self) -> bool:
            if not _host_ok(self.headers.get("Host")) and not unsafe_bind:
                self._json(403, {"error": "host not allowed"})
                return False
            return True

        def do_GET(self) -> None:
            if not self._guard():
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/health":
                return self._json(200, {"ok": True, **cal.status()})
            if cal.result is None:
                if path == "/":
                    return self._send(503, b"<p>compactlab: no calibration yet; it is running.</p>", "text/html")
                return self._json(503, {"error": "no calibration yet", **cal.status()})
            if path == "/":
                res = dict(cal.result)
                res["service"] = {**cal.status(), "token": token}
                return self._send(200, render_html(res).encode(), "text/html; charset=utf-8")
            if path == "/api/result":
                return self._json(200, cal.result)
            if path.startswith("/api/profile/"):
                name = path.rsplit("/", 1)[1]
                if name not in PROFILE_NAMES:
                    return self._json(404, {"error": f"unknown profile {name!r}", "profiles": list(PROFILE_NAMES)})
                res = reselect(json.loads(json.dumps(cal.result)), name)
                return self._json(200, {"profile": name, "groups": [
                    {k: g[k] for k in ("harness", "model", "role", "profiles", "comparison")} for g in res["groups"]
                ]})  # fmt: skip
            return self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._guard():
                return
            if self.path != "/api/recalibrate":
                return self._json(404, {"error": "not found"})
            if int(self.headers.get("Content-Length") or 0) > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not got or not hmac.compare_digest(got, token):
                return self._json(401, {"error": "token required"})
            if cal.running:
                return self._json(409, {"status": "busy"})
            threading.Thread(target=cal.recalibrate, args=("api",), daemon=True).start()
            return self._json(202, {"status": "started"})

    return ThreadingHTTPServer((host, port), Handler)
