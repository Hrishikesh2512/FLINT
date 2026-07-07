"""YouTube music playback for the wearable.

yt-dlp resolves "play <anything>" to an audio stream; mpv plays it into
the system PipeWire (= the connected headset). Playback runs as a child
process so the voice loop stays fully responsive — the wake word and
"stop the music" keep working while a song plays.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading

log = logging.getLogger("venom.music")

# Invoke as a module: immune to console-script corruption on flaky flash.
YTDLP = ["/opt/venom/venv/bin/python", "-m", "yt_dlp"]
DEFAULT_TIMEOUT = 25  # seconds for search/URL resolution
MPV_SOCKET = "/run/venom/mpv.sock"


class MusicPlayer:
    def __init__(self, ytdlp: list[str] | None = None):
        self._ytdlp = list(ytdlp or YTDLP)
        self._proc: subprocess.Popen | None = None
        self._title = ""
        self._lock = threading.Lock()

    # ── queries ───────────────────────────────────────────────────────────────
    @property
    def playing(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def now_playing(self) -> str:
        return self._title if self.playing else ""

    @property
    def paused(self) -> bool:
        """True when a track is loaded but mpv is paused (asks mpv directly)."""
        if not self.playing:
            return False
        return bool(self._ipc(["get_property", "pause"]).get("data"))

    # ── control ──────────────────────────────────────────────────────────────
    def play(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "What should I play?"
        self.stop()

        try:
            out = subprocess.run(
                [*self._ytdlp, "-4", "--no-playlist", "-f", "bestaudio/best",
                 "--print", "title", "--print", "url",
                 f"ytsearch1:{query}"],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"Searching for '{query}' took too long — try again."
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if out.returncode != 0 or len(lines) < 2:
            log.warning("yt-dlp failed: %s", (out.stderr or "")[:200])
            return f"I couldn't find '{query}' on YouTube."
        title, url = lines[0], lines[1]

        with self._lock:
            self._proc = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet", "--volume=70",
                 "--network-timeout=15",  # a dead CDN link must fail, not hang
                 f"--input-ipc-server={MPV_SOCKET}", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._title = title
        log.info("playing: %s", title)
        return f"Playing {title}."

    def stop(self) -> str:
        with self._lock:
            proc, self._proc, self._title = self._proc, None, ""
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return "Music stopped."
        return "Nothing is playing."

    # ── pause / resume (voice tools + the headset button) ────────────────────
    def _ipc(self, command: list) -> dict:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(3)
                sock.connect(MPV_SOCKET)
                sock.sendall(json.dumps({"command": command}).encode() + b"\n")
                return json.loads(sock.recv(4096).split(b"\n")[0])
        except (OSError, json.JSONDecodeError) as exc:
            log.debug("mpv ipc failed: %s", exc)
            return {}

    def toggle_pause(self) -> str:
        if not self.playing:
            return "Nothing is playing."
        self._ipc(["cycle", "pause"])
        state = self._ipc(["get_property", "pause"]).get("data")
        return "Paused." if state else "Resumed."

    def set_paused(self, paused: bool) -> str:
        if not self.playing:
            return "Nothing is playing."
        self._ipc(["set_property", "pause", paused])
        return "Paused." if paused else "Resumed."
