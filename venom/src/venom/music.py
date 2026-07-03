"""YouTube music playback for the wearable.

yt-dlp resolves "play <anything>" to an audio stream; mpv plays it into
the system PipeWire (= the connected headset). Playback runs as a child
process so the voice loop stays fully responsive — the wake word and
"stop the music" keep working while a song plays.
"""

from __future__ import annotations

import logging
import subprocess
import threading

log = logging.getLogger("venom.music")

YTDLP = "/opt/venom/venv/bin/yt-dlp"
DEFAULT_TIMEOUT = 25  # seconds for search/URL resolution


class MusicPlayer:
    def __init__(self, ytdlp: str = YTDLP):
        self._ytdlp = ytdlp
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

    # ── control ──────────────────────────────────────────────────────────────
    def play(self, query: str) -> str:
        query = (query or "").strip()
        if not query:
            return "What should I play?"
        self.stop()

        try:
            out = subprocess.run(
                [self._ytdlp, "--no-playlist", "-f", "bestaudio/best",
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
                ["mpv", "--no-video", "--really-quiet", "--volume=70", url],
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
