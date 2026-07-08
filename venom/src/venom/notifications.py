"""Phone notifications → Venom, over ntfy (the reverse of find-my-phone).

On the phone, an automation (MacroDroid / Tasker) watches the apps YOU pick —
WhatsApp for now — and POSTs each notification to an ntfy topic. Here a
background thread subscribes to that topic's JSON stream: every arrival plays a
distinct "message" chime through the headset (via PipeWire's pw-play, so it
works even when no conversation is open), and the text is held so Venom can read
it out only when asked.

Design: chime on arrival, explain on demand. Nothing is spoken automatically.
The chime is suppressed while Do-Not-Disturb is on.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import urllib.request
import wave
from collections import deque
from pathlib import Path

log = logging.getLogger("venom.notifications")

CHIME_WAV = Path("/run/venom/notif_chime.wav")
SAMPLE_RATE = 24000


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
    """Subscribes to the phone's notification topic; chimes + holds messages."""

    def __init__(self, server: str, topic: str, is_dnd=None):
        self._server = (server or "https://ntfy.sh").rstrip("/")
        self._topic = (topic or "").strip()
        self._is_dnd = is_dnd or (lambda: False)
        self._recent: deque[dict] = deque(maxlen=30)
        self._unread = 0
        self._seen: deque[str] = deque(maxlen=200)  # message ids, for dedupe
        self._lock = threading.Lock()
        self._chime_path = _write_chime()

    @property
    def enabled(self) -> bool:
        return bool(self._topic)

    # ── background subscriber ────────────────────────────────────────────────
    def start(self) -> None:
        if not self.enabled:
            return
        threading.Thread(target=self._subscribe, daemon=True,
                         name="venom-notifs").start()
        log.info("notification hub subscribed to %s/%s", self._server, self._topic)

    def _subscribe(self) -> None:
        url = f"{self._server}/{self._topic}/json"
        backoff = 2
        while True:
            try:
                with urllib.request.urlopen(url, timeout=75) as resp:
                    backoff = 2  # a clean connect resets the retry delay
                    for raw in resp:
                        line = raw.decode("utf-8", "replace").strip()
                        if line:
                            self._handle(line)
            except Exception as exc:  # network drop / timeout → reconnect
                log.debug("notif stream reconnecting (%s)", exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _handle(self, line: str) -> None:
        try:
            obj = json.loads(line)
        except ValueError:
            return
        if obj.get("event") != "message":  # skip open/keepalive/poll_request
            return
        mid = obj.get("id", "")
        with self._lock:
            if mid and mid in self._seen:
                return  # already processed (reconnect replayed the cache)
            if mid:
                self._seen.append(mid)
            entry = {
                "app": "WhatsApp",
                "title": (obj.get("title") or "").strip(),   # sender / chat
                "message": (obj.get("message") or "").strip(),
                "ts": obj.get("time") or time.time(),
            }
            self._recent.append(entry)
            self._unread += 1
        log.info("notification: %s — %s", entry["title"], entry["message"][:60])
        if not self._is_dnd():
            self._chime()

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
