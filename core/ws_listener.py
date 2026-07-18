"""WebSocket command listener — the bridge for the upcoming smartphone app.

Runs a small JSON-over-WebSocket server on its own thread + event loop.
Phone clients connect, optionally authenticate with a shared token, and can
then push commands into FLINT exactly as if they were typed into the desktop
command box.  The listener also broadcasts state changes, log lines and job
lifecycle events so a remote client can mirror the desktop HUD.

Wire protocol (one JSON object per message):

    client -> server
        {"type": "auth",    "token": "<shared secret>"}
        {"type": "command", "text": "open spotify"}
        {"type": "ping"}
        {"type": "status"}

    server -> client
        {"type": "hello",  "name": "FLINT", "auth_required": bool}
        {"type": "auth_ok"} | {"type": "auth_failed"}
        {"type": "ack",    "text": "<echoed command>"}
        {"type": "pong"}
        {"type": "state",  "state": "LISTENING"}
        {"type": "log",    "line": "...", "level": "info"}
        {"type": "job",    "event": "job_started", ...}
        {"type": "status", "clients": 1, "uptime": 12.3, ...}
        {"type": "error",  "message": "..."}

Configuration lives in config/api_keys.json:
    "remote_port":  8765        (optional, default 8765)
    "remote_token": "secret"    (optional; if set, clients must auth first)
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Callable

try:
    import websockets
    _WS_OK = True
except ImportError:
    _WS_OK = False

logger = logging.getLogger(__name__)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765


def _say(msg: str) -> None:
    """print() that survives cp1252 consoles choking on emoji."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))

CommandHandler = Callable[[str, Callable[[str], None]], None]
StatusCallback = Callable[[int], None]


class CommandListener:
    """Threaded WebSocket server pushing remote commands into FLINT."""

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 token: str | None = None):
        self.host = host
        self.port = port
        self.token = (token or "").strip() or None
        self.on_command: CommandHandler | None = None
        self.on_clients_changed: StatusCallback | None = None
        self.started_at: float | None = None

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._clients: set = set()
        self._authed: set = set()
        self._lock = threading.Lock()
        self._running = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return _WS_OK

    @property
    def running(self) -> bool:
        return self._running

    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def start(self) -> bool:
        if not _WS_OK:
            _say("[Remote] ⚠️ 'websockets' package not installed — "
                  "remote listener disabled (pip install websockets)")
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="FlintRemoteListener")
        self._thread.start()
        return True

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except Exception as e:
            _say(f"[Remote] ❌ Listener crashed: {e}")
        finally:
            self._running = False

    async def _serve(self):
        async with websockets.serve(self._handler, self.host, self.port,
                                    ping_interval=20, ping_timeout=20):
            self._running = True
            self.started_at = time.time()
            _say(f"[Remote] ✅ WebSocket listener on ws://{self.host}:{self.port}"
                  f"{' (token auth)' if self.token else ''}")
            await asyncio.Future()              # serve forever

    # ── connection handling ──────────────────────────────────────────────────
    def _notify_clients_changed(self):
        if self.on_clients_changed:
            try:
                self.on_clients_changed(self.client_count())
            except Exception as e:
                logger.warning("ws_listener: notify_clients_changed failed: %s", e)

    async def _handler(self, ws):
        with self._lock:
            self._clients.add(ws)
        self._notify_clients_changed()
        peer = getattr(ws, "remote_address", ("?",))[0]
        _say(f"[Remote] 📱 Client connected: {peer}")
        try:
            await ws.send(json.dumps({
                "type": "hello", "name": "FLINT",
                "auth_required": self.token is not None,
            }))
            async for raw in ws:
                await self._on_message(ws, raw)
        except Exception as e:
            logger.warning("ws_listener: handler exception: %s", e)
        finally:
            with self._lock:
                self._clients.discard(ws)
                self._authed.discard(ws)
            self._notify_clients_changed()
            _say(f"[Remote] 📴 Client disconnected: {peer}")

    async def _on_message(self, ws, raw):
        try:
            msg = json.loads(raw)
            if not isinstance(msg, dict):
                raise ValueError("expected a JSON object")
        except Exception as e:
            await ws.send(json.dumps({"type": "error", "message": f"bad json: {e}"}))
            return

        mtype = str(msg.get("type", "")).lower()

        if mtype == "ping":
            await ws.send(json.dumps({"type": "pong"}))
            return

        if mtype == "auth":
            if self.token is None or msg.get("token") == self.token:
                with self._lock:
                    self._authed.add(ws)
                await ws.send(json.dumps({"type": "auth_ok"}))
            else:
                await ws.send(json.dumps({"type": "auth_failed"}))
            return

        if self.token is not None:
            with self._lock:
                authed = ws in self._authed
            if not authed:
                await ws.send(json.dumps(
                    {"type": "error", "message": "auth required"}))
                return

        if mtype == "status":
            await ws.send(json.dumps({
                "type": "status",
                "clients": self.client_count(),
                "uptime": round(time.time() - (self.started_at or time.time()), 1),
            }))
            return

        if mtype == "command":
            text = str(msg.get("text", "")).strip()
            if not text:
                await ws.send(json.dumps(
                    {"type": "error", "message": "empty command"}))
                return
            await ws.send(json.dumps({"type": "ack", "text": text}))
            handler = self.on_command
            if handler:
                loop = self._loop

                def reply(out: str, _ws=ws, _loop=loop):
                    asyncio.run_coroutine_threadsafe(
                        _ws.send(json.dumps({"type": "reply", "text": out})),
                        _loop)

                # Hand off to FLINT on a plain thread; never block the server.
                threading.Thread(target=handler, args=(text, reply),
                                 daemon=True).start()
            return

        await ws.send(json.dumps(
            {"type": "error", "message": f"unknown type: {mtype}"}))

    # ── broadcasting ─────────────────────────────────────────────────────────
    def broadcast(self, payload: dict) -> None:
        """Fire-and-forget fan-out to all authed clients (thread-safe)."""
        if not self._running or not self._loop:
            return
        data = json.dumps(payload)
        asyncio.run_coroutine_threadsafe(self._broadcast(data), self._loop)

    async def _broadcast(self, data: str):
        with self._lock:
            targets = [ws for ws in self._clients
                       if self.token is None or ws in self._authed]
        for ws in targets:
            try:
                await ws.send(data)
            except Exception as e:
                logger.warning("ws_listener: broadcast send failed: %s", e)


_listener: CommandListener | None = None
_listener_lock = threading.Lock()


def get_listener(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 token: str | None = None) -> CommandListener:
    global _listener
    with _listener_lock:
        if _listener is None:
            _listener = CommandListener(host, port, token)
        return _listener


if __name__ == "__main__":
    lst = get_listener(port=8765)

    def on_cmd(text, reply):
        _say(f"[TEST] command received: {text!r}")
        reply(f"echo: {text}")

    lst.on_command = on_cmd
    lst.start()
    _say("Listening — connect with a WebSocket client and send "
          '{"type":"command","text":"hello"}')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

