"""Incoming phone notifications → Venom, delivered locally.

WhatsApp messages arrive at the self-hosted Baileys bridge (venom-whatsapp),
which runs on this same Pi. Instead of shipping each message out to a public
ntfy topic and reading it back — which put your message contents through a
third-party server — the bridge now POSTs them straight to this hub over
loopback (127.0.0.1). Every arrival plays a distinct "message" chime through
the headset (via PipeWire's pw-play, so it works even when no conversation is
open), and the text is held so Venom can read it out only when asked.

Design: chime on arrival, explain on demand. Nothing is spoken automatically.
The chime is suppressed while Do-Not-Disturb is on. The ingest listener binds
loopback only, so nothing off-box can reach it; the bridge retries on its side
if a message lands while the voice daemon is mid-restart, so none are lost.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import wave
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("venom.notifications")

CHIME_WAV = Path("/run/venom/notif_chime.wav")
SAMPLE_RATE = 24000

# Loopback ingest endpoint the WhatsApp bridge POSTs incoming messages to.
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8789


def _write_chime(path: Path = CHIME_WAV) -> Path | None:
    """Synthesise the 'message' chime once — a soft rising two-note (C5→G5),
    deliberately unlike the wake/timer/translation chimes."""
    try:
        import numpy as np

        def tone(freq: float, dur: float, vol: float = 0.28) -> "np.ndarray":
            n = int(SAMPLE_RATE * dur)
            i = np.arange(n)
            fade = np.minimum(np.minimum(i, n - i) / (n * 0.2), 1.0)  # in+out, no click
            return (32767 * vol * fade
                    * np.sin(2 * np.pi * freq * i / SAMPLE_RATE)).astype("<i2")

        gap = np.zeros(int(SAMPLE_RATE * 0.05), dtype="<i2")
        pcm = np.concatenate([tone(523.25, 0.11), gap, tone(783.99, 0.16)])
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm.tobytes())
        return path
    except Exception as exc:  # numpy/fs issue — chime just won't play
        log.warning("notif chime synth failed: %s", exc)
        return None


class NotificationHub:
    """Receives incoming messages from the WhatsApp bridge over loopback;
    chimes on arrival and holds them for on-demand reading."""

    def __init__(self, is_dnd=None, on_arrival=None, bind: str = DEFAULT_BIND,
                 port: int = DEFAULT_PORT, enabled: bool = True):
        self._bind = bind or DEFAULT_BIND
        self._port = port
        self._enabled = enabled
        self._is_dnd = is_dnd or (lambda: False)
        # Called (off the network thread) with the sender name when a message
        # arrives and DND is off — lets the voice loop announce it proactively.
        self._on_arrival = on_arrival
        self._recent: deque[dict] = deque(maxlen=30)
        self._unread = 0
        self._seen: deque[str] = deque(maxlen=200)  # message ids, for dedupe
        self._lock = threading.Lock()
        self._chime_path = _write_chime()

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── loopback ingest server ───────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            return
        hub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep journald quiet
                pass

            def do_POST(self):
                if self.path != "/notify":
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    body = self.rfile.read(size) or b"{}"
                except (ValueError, OSError):
                    body = b"{}"
                ok = hub.ingest_json(body.decode("utf-8", "replace"))
                self.send_response(200 if ok else 400)
                self.send_header("Content-Length", "0")
                self.end_headers()

        try:
            server = ThreadingHTTPServer((self._bind, self._port), Handler)
        except OSError as exc:  # port busy (stale instance) — degrade, don't crash
            log.warning("notification ingest could not bind %s:%d (%s)",
                        self._bind, self._port, exc)
            return
        threading.Thread(target=server.serve_forever, daemon=True,
                         name="venom-notify").start()
        log.info("notification ingest on http://%s:%d/notify",
                 self._bind, self._port)

    # ── ingest ───────────────────────────────────────────────────────────────
    def ingest_json(self, body: str) -> bool:
        """Parse a bridge POST ({title, message, id}) and hand it to ingest()."""
        try:
            obj = json.loads(body)
        except ValueError:
            return False
        self.ingest(str(obj.get("title", "")).strip(),
                    str(obj.get("message", "")).strip(),
                    str(obj.get("id", "")).strip())
        return True

    def ingest(self, title: str, message: str, mid: str = "") -> None:
        """Record a message, dedupe by id, chime (unless DND), and notify."""
        if not message:
            return
        with self._lock:
            if mid and mid in self._seen:
                return  # already processed (a bridge retry replayed it)
            if mid:
                self._seen.append(mid)
            entry = {
                "app": "WhatsApp",
                "title": title,          # sender / chat
                "message": message,
                "ts": time.time(),
            }
            self._recent.append(entry)
            self._unread += 1
        log.info("notification: %s — %s", entry["title"], entry["message"][:60])
        if not self._is_dnd():
            self._chime()
            if self._on_arrival:
                try:
                    self._on_arrival(entry["title"] or "Someone")
                except Exception as exc:  # never let a hook kill the ingest
                    log.debug("on_arrival hook failed: %s", exc)

    def _chime(self) -> None:
        if not self._chime_path:
            return
        try:
            subprocess.Popen(["pw-play", str(self._chime_path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:  # pw-play missing / audio busy — never crash
            log.debug("notif chime play failed: %s", exc)

    # ── on-demand reading (voice tool) ───────────────────────────────────────
    def read_unread(self) -> str:
        """Speak the unread messages and mark them read."""
        with self._lock:
            n = self._unread
            items = list(self._recent)[-n:] if n else []
            self._unread = 0
        if not items:
            return "No new notifications."
        lead = ("You have one new WhatsApp message." if len(items) == 1
                else f"You have {len(items)} new WhatsApp messages.")
        return lead + " " + " ".join(self._say(e) for e in items)

    def read_all(self) -> str:
        with self._lock:
            items = list(self._recent)
            self._unread = 0
        if not items:
            return "No notifications yet."
        recent = items[-5:]
        return "Recent WhatsApp: " + " ".join(self._say(e) for e in recent)

    @staticmethod
    def _say(entry: dict) -> str:
        who = entry.get("title") or "Someone"
        msg = entry.get("message") or ""
        return f"{who} says: {msg}." if msg else f"A message from {who}."
