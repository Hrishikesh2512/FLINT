"""Carnage as a page the phone can install, so nothing has to be downloaded.

The Termux route works and asks for three apps from F-Droid, a terminal, and a
tolerance for `pkg install`. This is the other route: run Carnage on a machine
that is already yours, open a URL on the phone, and add it to the home screen.
It launches full-screen from an icon and there is no app store anywhere in the
story.

A stdlib server on purpose — `http.server`, no new dependency — matching
`venom/web.py`, which has been serving the console from the same primitives on
a 2 GB Pi for months. This is one page and a handful of JSON endpoints; a
framework would be more code than the thing it served.

**It must be served over HTTPS.** Not a preference: browsers refuse
`navigator.geolocation`, `getUserMedia`, service workers and installability on
a plain-HTTP origin unless it is localhost, and those four *are* the feature.
The intended answer is `tailscale serve`, which gives a real certificate on a
name only the tailnet can reach — no port forwarding, nothing public, no
self-signed warning. See `carnage/provisioning/README.md`.

Two things this server deliberately does not do:

  * **No cookies, no sessions.** Every request carries the same bearer token
    the sync hub uses. One secret for the device, not two.
  * **No state of its own.** It reads snapshots and posts readings into
    `BrowserPhone`; the runtime owns everything real. A page that crashes,
    reloads or is opened twice cannot corrupt anything, because there is
    nothing here to corrupt.
"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("carnage.web")

#: The page itself. Small, static, and read from disk on each request so that
#: editing it during development needs no restart.
WEB_ROOT = Path(__file__).resolve().parent.parent.parent / "web"

#: A posted reading is a few hundred bytes. Anything larger is not one.
MAX_BODY = 64 * 1024

_ALLOWED = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/sw.js": "sw.js",
    "/manifest.webmanifest": "manifest.webmanifest",
    "/icon.svg": "icon.svg",
}


class CarnageWeb:
    """Serves the page and the handful of endpoints it talks to."""

    def __init__(self, carnage, token: str = "", host: str = "127.0.0.1",
                 port: int = 8791, web_root: Path | None = None):
        self._carnage = carnage
        self._token = token or ""
        self._host = host
        self._port = port
        self._root = Path(web_root) if web_root else WEB_ROOT
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── the endpoints ───────────────────────────────────────────────────────
    def status(self) -> dict:
        """What she is, and what this page is currently giving her."""
        phone = self._carnage.phone
        return {
            "device": self._carnage.config.device,
            "user": self._carnage.config.user_name,
            "body": phone.name,
            "connected": bool(getattr(phone, "connected", lambda: False)()),
            "sends_directly": bool(getattr(phone, "sends_directly", True)),
            "tools": len(list(self._carnage.registry)),
            "capabilities": self._carnage.capabilities.names(),
            "devices": [
                {"name": d.name, "body": d.body,
                 "presence": d.presence(time.time())}
                for d in self._carnage.roster.others()
            ],
        }

    def report(self, reading: dict) -> dict:
        """The page hands over GPS and battery; we hand back anything to do."""
        phone = self._carnage.phone
        if hasattr(phone, "report"):
            phone.report(reading)
        outbox = phone.take_outbox() if hasattr(phone, "take_outbox") else []
        return {"ok": True, "outbox": outbox}

    def ask(self, text: str) -> dict:
        """One turn of conversation, as text.

        Text rather than audio because the two are different problems and only
        one of them is this file's. A page that can hold a conversation, read
        her memory and act is already the thing; adding a live audio socket is
        a separate piece of work with its own failure modes.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": False, "said": "You didn't say anything."}
        answer = self._carnage.answer(text)
        return {"ok": True, "said": answer}

    # ── plumbing ────────────────────────────────────────────────────────────
    def _authorised(self, header: str | None) -> bool:
        if not self._token:
            return True
        given = (header or "").removeprefix("Bearer ").strip()
        # Constant-time: the token is the only thing between the tailnet and
        # her whole memory.
        return hmac.compare_digest(given, self._token)

    def _handler(self):
        web = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):        # noqa: A003
                log.debug("web: " + fmt, *args)

            # ── responses ──────────────────────────────────────────────
            def _send(self, code: int, body: bytes, kind: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                # The page is served from one origin and talks only to it.
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")

            # ── GET ────────────────────────────────────────────────────
            def do_GET(self) -> None:      # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in _ALLOWED:
                    return self._serve_file(_ALLOWED[path])
                if path == "/api/status":
                    if not web._authorised(self.headers.get("Authorization")):
                        return self._json(401, {"error": "unauthorised"})
                    return self._json(200, web.status())
                self._json(404, {"error": "no such thing here"})

            def _serve_file(self, name: str) -> None:
                target = (web._root / name).resolve()
                # The map above is a fixed allowlist, so this cannot be
                # traversed — the check is belt and braces for a future edit
                # that makes the mapping dynamic.
                if web._root.resolve() not in target.parents:
                    return self._json(403, {"error": "no"})
                try:
                    body = target.read_bytes()
                except OSError:
                    return self._json(404, {"error": f"{name} is missing"})
                kind = mimetypes.guess_type(name)[0] or "text/plain"
                if name.endswith(".webmanifest"):
                    kind = "application/manifest+json"
                if name.endswith(".js"):
                    kind = "text/javascript"
                self._send(200, body, f"{kind}; charset=utf-8"
                           if kind.startswith("text") or "json" in kind else kind)

            # ── POST ───────────────────────────────────────────────────
            def do_POST(self) -> None:     # noqa: N802
                if not web._authorised(self.headers.get("Authorization")):
                    return self._json(401, {"error": "unauthorised"})
                try:
                    length = int(self.headers.get("Content-Length", 0))
                except ValueError:
                    return self._json(400, {"error": "bad length"})
                if length > MAX_BODY:
                    return self._json(413, {"error": "too much"})
                try:
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(payload, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "bad json"})

                path = self.path.split("?", 1)[0]
                try:
                    if path == "/api/report":
                        return self._json(200, web.report(payload))
                    if path == "/api/ask":
                        return self._json(200, web.ask(payload.get("text", "")))
                except Exception:          # noqa: BLE001
                    # A page in someone's pocket must not be able to take the
                    # assistant down by posting something odd.
                    log.exception("web: %s failed", path)
                    return self._json(500, {"error": "that went wrong here"})
                self._json(404, {"error": "no such thing here"})

        return Handler

    # ── lifecycle ───────────────────────────────────────────────────────────
    def start(self) -> bool:
        try:
            self._server = ThreadingHTTPServer((self._host, self._port),
                                               self._handler())
        except OSError as exc:
            log.warning("web console could not bind %s:%s — %s",
                        self._host, self._port, exc)
            return False
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="carnage-web", daemon=True)
        self._thread.start()
        log.info("web console on http://%s:%s", self._host, self._port)
        return True

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
