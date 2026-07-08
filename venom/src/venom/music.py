"""YouTube music playback for the wearable.

yt-dlp resolves "play <anything>" to an audio stream; mpv plays it into
the system PipeWire (= the connected headset). Playback runs as a child
process so the voice loop stays fully responsive — the wake word and
"stop the music" keep working while a song plays.

When a song finishes on its own, autoplay queues up a *similar* track from
YouTube's radio mix (``RD<videoid>``) — a coherent, endless run seeded off
the first thing the user asked for. A user "stop" (or a new "play …") ends
the run; only a natural finish rolls to the next song.
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
RADIO_BATCH = 20  # how many similar tracks to pull from the mix at a time


class MusicPlayer:
    def __init__(self, ytdlp: list[str] | None = None, autoplay: bool = True):
        self._ytdlp = list(ytdlp or YTDLP)
        self._proc: subprocess.Popen | None = None
        self._title = ""
        self._lock = threading.Lock()
        self._autoplay = autoplay
        # `_gen` invalidates a monitor thread the moment the user stops or plays
        # something else, so a finishing song never autoplays over their intent.
        self._gen = 0
        self._seed = ""              # video id the radio mix is built from
        self._queue: list[dict] = []  # upcoming similar tracks: {id, title}
        self._played: set[str] = set()  # ids already played this run (no repeats)

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
                 "--print", "title", "--print", "id", "--print", "url",
                 f"ytsearch1:{query}"],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"Searching for '{query}' took too long — try again."
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if out.returncode != 0 or len(lines) < 3:
            log.warning("yt-dlp failed: %s", (out.stderr or "")[:200])
            return f"I couldn't find '{query}' on YouTube."
        title, video_id, url = lines[0], lines[1], lines[2]

        # A fresh user request reseeds the radio mix and clears the old run.
        with self._lock:
            self._seed = video_id
            self._queue = []
            self._played = {video_id}
        self._spawn(title, url, self._gen)
        return f"Playing {title}."

    def stop(self) -> str:
        with self._lock:
            proc, self._proc, self._title = self._proc, None, ""
            self._gen += 1  # invalidate any monitor so it won't autoplay
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            return "Music stopped."
        return "Nothing is playing."

    def set_autoplay(self, on: bool) -> str:
        with self._lock:
            self._autoplay = bool(on)
        return ("I'll keep the music going with similar songs."
                if on else "I'll stop after the current song.")

    # ── spawning + the finish monitor ────────────────────────────────────────
    def _spawn(self, title: str, url: str, gen: int) -> bool:
        """Start mpv on `url` and a thread that reacts when it ends.

        Returns False if the run was superseded (user stopped / played anew)
        between resolving `url` and here.
        """
        with self._lock:
            if gen != self._gen:
                return False
            proc = subprocess.Popen(
                ["mpv", "--no-video", "--really-quiet", "--volume=70",
                 "--network-timeout=15",  # a dead CDN link must fail, not hang
                 f"--input-ipc-server={MPV_SOCKET}", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._proc = proc
            self._title = title
        log.info("playing: %s", title)
        threading.Thread(target=self._monitor, args=(proc, gen),
                         daemon=True).start()
        return True

    def _monitor(self, proc: subprocess.Popen, gen: int) -> None:
        proc.wait()  # blocks until the song ends — naturally or on terminate()
        with self._lock:
            superseded = gen != self._gen  # stop()/play() bumped the generation
            autoplay = self._autoplay
            seed = self._seed
        if superseded or not autoplay:
            return
        self._autoplay_next(gen, seed)

    def _autoplay_next(self, gen: int, seed: str) -> None:
        """A song finished on its own → play the next similar track."""
        if not seed:
            return
        with self._lock:
            if not self._queue:
                self._queue = self._fetch_radio(seed)
        while True:
            with self._lock:
                if gen != self._gen:
                    return  # user stepped in while we were resolving
                nxt = self._queue.pop(0) if self._queue else None
                if nxt and nxt["id"] in self._played:
                    continue
            if nxt is None:
                break
            url = self._resolve_url(nxt["id"])
            if not url:
                continue
            with self._lock:
                if gen != self._gen:
                    return
                self._played.add(nxt["id"])
            if self._spawn(nxt["title"], url, gen):
                log.info("autoplay similar: %s", nxt["title"])
                return
        log.info("autoplay: no more similar tracks for seed %s", seed)

    def _fetch_radio(self, seed_id: str) -> list[dict]:
        """The seed's YouTube mix — an ordered list of similar tracks."""
        mix = f"https://www.youtube.com/watch?v={seed_id}&list=RD{seed_id}"
        try:
            out = subprocess.run(
                [*self._ytdlp, "-4", "--flat-playlist",
                 "--playlist-items", f"1-{RADIO_BATCH}",
                 "--print", "%(id)s\t%(title)s", mix],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return []
        if out.returncode != 0:
            log.warning("radio fetch failed: %s", (out.stderr or "")[:200])
            return []
        tracks = []
        for line in (out.stdout or "").splitlines():
            if "\t" not in line:
                continue
            vid, title = line.split("\t", 1)
            vid = vid.strip()
            if vid and vid != seed_id:
                tracks.append({"id": vid, "title": title.strip()})
        return tracks

    def _resolve_url(self, video_id: str) -> str:
        """A playable audio stream URL for a specific video id."""
        try:
            out = subprocess.run(
                [*self._ytdlp, "-4", "--no-playlist", "-f", "bestaudio/best",
                 "--print", "url",
                 f"https://www.youtube.com/watch?v={video_id}"],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return ""
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if out.returncode != 0 or not lines:
            return ""
        return lines[0]

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
