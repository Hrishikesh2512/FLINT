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

import collections
import json
import logging
import socket
import subprocess
import threading
import time

log = logging.getLogger("venom.music")

# Invoke as a module: immune to console-script corruption on flaky flash.
YTDLP = ["/opt/venom/venv/bin/python", "-m", "yt_dlp"]
MPV = ["mpv", "--no-video", "--really-quiet", "--volume=70",
       "--network-timeout=15"]  # a dead CDN link must fail, not hang
DEFAULT_TIMEOUT = 25  # seconds for search/URL resolution
MPV_SOCKET = "/run/venom/mpv.sock"
RADIO_BATCH = 20  # how many similar tracks to pull from the mix at a time


class MusicPlayer:
    # A song that ends this quickly never actually played — a 403 stream URL,
    # a missing audio sink, or an mpv crash. Distinguishing it from a natural
    # finish is what keeps autoplay from chaining dead track after dead track
    # while the user hears nothing.
    MIN_PLAY_SECONDS = 3.0
    # How long play() waits before vouching that the song is really playing.
    SPAWN_CHECK_SECONDS = 1.2
    # Consecutive instant deaths before we stop trying — something systemic
    # (no audio device, YouTube blocking us) that retrying won't fix.
    MAX_FAILS = 3

    def __init__(self, ytdlp: list[str] | None = None, autoplay: bool = True,
                 mpv: list[str] | None = None):
        self._ytdlp = list(ytdlp or YTDLP)
        self._mpv = list(mpv or MPV)
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
        self._fail_streak = 0        # consecutive instant-death spawns
        self._skipping = False       # a user skip: terminate ≠ failure
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=5)
        # Pause bookkeeping. A conversation "ducks" (pauses) the music so the
        # shared-headset mic stays clean, then resumes it on the way out — but
        # an explicit user pause/stop during that window must win over the
        # resume, or "pause the music" un-pauses itself seconds later.
        self._user_paused = False    # the user's explicit intent
        self._ducked = False         # we paused it for a conversation

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
            self._fail_streak = 0  # a fresh ask gets a fresh chance
            self._user_paused = self._ducked = False
        proc = self._spawn(title, url, self._gen)
        if proc is None:
            return "Something interrupted that — try again."
        # Don't vouch for a song that died on arrival (bad stream URL, no
        # audio device): wait a beat and check mpv is actually still playing.
        time.sleep(self.SPAWN_CHECK_SECONDS)
        if proc.poll() is not None:
            err = "; ".join(self._stderr_tail)
            log.warning("mpv died immediately (exit %s): %s",
                        proc.returncode, err[:200])
            return (f"I found '{title}' but playback failed on the device — "
                    "the audio output may be down.")
        return f"Playing {title}."

    def stop(self) -> str:
        with self._lock:
            proc, self._proc, self._title = self._proc, None, ""
            self._gen += 1  # invalidate any monitor so it won't autoplay
            self._user_paused = self._ducked = False
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

    def skip(self) -> str:
        """Jump to the next similar track — a user skip, not a failure."""
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return "Nothing is playing."
            if not self._autoplay or not self._seed:
                return ("Autoplay is off, so there's nothing queued after "
                        "this — say 'play something' instead.")
            self._skipping = True  # the monitor treats this end as natural
            self._user_paused = False  # skipping means they want it playing
        proc.terminate()
        return "Skipping — next song coming up."

    # ── spawning + the finish monitor ────────────────────────────────────────
    def _spawn(self, title: str, url: str, gen: int) -> subprocess.Popen | None:
        """Start mpv on `url` and a thread that reacts when it ends.

        Returns None if the run was superseded (user stopped / played anew)
        between resolving `url` and here.
        """
        with self._lock:
            if gen != self._gen:
                return None
            proc = subprocess.Popen(
                [*self._mpv, f"--input-ipc-server={MPV_SOCKET}", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            self._proc = proc
            self._title = title
            self._stderr_tail = collections.deque(maxlen=5)
        log.info("playing: %s", title)
        threading.Thread(target=self._drain_stderr, args=(proc,),
                         daemon=True).start()
        threading.Thread(target=self._monitor,
                         args=(proc, gen, time.monotonic()),
                         daemon=True).start()
        return proc

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Keep mpv's last few stderr lines so a failure can say *why*.
        Also stops a chatty mpv from blocking on a full pipe."""
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):
            pass

    def _monitor(self, proc: subprocess.Popen, gen: int, started: float) -> None:
        proc.wait()  # blocks until the song ends — naturally or on terminate()
        lifetime = time.monotonic() - started
        with self._lock:
            superseded = gen != self._gen  # stop()/play() bumped the generation
            autoplay = self._autoplay
            seed = self._seed
            skipped, self._skipping = self._skipping, False
        if superseded:
            return
        # An instant death is a broken stream or a dead audio path, not a
        # finished song (even exit 0: an empty stream "finishes" instantly).
        # Autoplaying "the next one" would just chain failures while the user
        # hears nothing — count it, and stop after a few.
        if not skipped and lifetime < self.MIN_PLAY_SECONDS:
            with self._lock:
                self._fail_streak += 1
                streak = self._fail_streak
            log.warning("song died after %.1fs (exit %s, failure %d/%d): %s",
                        lifetime, proc.returncode, streak, self.MAX_FAILS,
                        "; ".join(self._stderr_tail)[:200])
            if streak >= self.MAX_FAILS:
                log.error("music: %d consecutive playback failures — "
                          "giving up until the next request", streak)
                return
        else:
            with self._lock:
                self._fail_streak = 0
        if not autoplay:
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
        self._user_paused = bool(state)
        self._ducked = False
        return "Paused." if state else "Resumed."

    def set_paused(self, paused: bool) -> str:
        """An explicit user pause/resume (voice tool, console button). This is
        intent — it out-ranks the conversation duck, so unduck() won't undo it."""
        if not self.playing:
            return "Nothing is playing."
        self._user_paused = bool(paused)
        self._ducked = False
        self._ipc(["set_property", "pause", bool(paused)])
        # Read back instead of assuming: with the IPC socket dead this used to
        # cheerfully answer "Paused." while the song played on.
        state = self._ipc(["get_property", "pause"]).get("data")
        if state is None or bool(state) != bool(paused):
            return ("I couldn't control the player — try stopping and "
                    "playing it again.")
        return "Paused." if paused else "Resumed."

    # ── conversation ducking (the voice loop, never the user) ───────────────
    def duck(self) -> bool:
        """A conversation is starting: pause our own audible playback so the
        shared-headset mic hears the user cleanly. True if we paused it."""
        if not self.playing or self.paused:
            return False
        self._ipc(["set_property", "pause", True])
        self._ducked = True
        return True

    def unduck(self) -> bool:
        """Conversation over: resume ONLY what duck() paused — and not if the
        user explicitly paused or stopped in the meantime (their word wins).
        True if playback was actually resumed."""
        ducked, self._ducked = self._ducked, False
        if not ducked or self._user_paused or not self.playing:
            return False
        self._ipc(["set_property", "pause", False])
        return True
