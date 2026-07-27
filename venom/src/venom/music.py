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
import random
import socket
import subprocess
import threading
import time
from pathlib import Path

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
                 mpv: list[str] | None = None, favourites=None,
                 offline_dir=None):
        self._ytdlp = list(ytdlp or YTDLP)
        self._mpv = list(mpv or MPV)
        self._proc: subprocess.Popen | None = None
        self._title = ""
        self._current_id = ""        # video id of the track playing now
        self._lock = threading.Lock()
        self._autoplay = autoplay
        # Favourites + their offline copies. `favourites` is a FavouritesStore;
        # `offline_dir` holds pre-downloaded audio so saved songs survive the
        # dead zones on a long train ride, when yt-dlp can't reach YouTube.
        self._favourites = favourites
        self._offline_dir = Path(offline_dir) if offline_dir else None
        self._downloading = False    # a background offline-download in flight
        # Ordered play history for "restart" / "previous song". `_hist_pos`
        # points at the entry playing now; navigating back replays an earlier id.
        self._history: list[dict] = []   # [{id, title}] in the order played
        self._hist_pos = -1
        # A favourites playlist run: chain through saved songs (offline copies
        # first) instead of YouTube's radio mix. Independent of `_autoplay`.
        self._fav_mode = False
        self._fav_queue: list[dict] = []
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

    def status(self) -> str:
        """A spoken 'what's playing' line, tagged if it's a favourite / offline."""
        if not self.playing:
            return "Nothing is playing."
        with self._lock:
            title, vid = self._title, self._current_id
        tags = []
        if self._favourites is not None and self._favourites.is_favourite(vid):
            tags.append("one of your favourites")
        if self._local_file(vid) is not None:
            tags.append("saved offline")
        suffix = f" — {', '.join(tags)}" if tags else ""
        return f"Now playing: {title}{suffix}."

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
            # Search stalled — likely no signal. A saved song can still play
            # from its offline copy.
            return (self._offline_fallback(query)
                    or f"Searching for '{query}' took too long — try again.")
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if out.returncode != 0 or len(lines) < 3:
            fallback = self._offline_fallback(query)
            if fallback:
                return fallback
            log.warning("yt-dlp failed: %s", (out.stderr or "")[:200])
            return f"I couldn't find '{query}' on YouTube."
        title, video_id, url = lines[0], lines[1], lines[2]

        # A fresh user request reseeds the radio mix and clears the old run —
        # and a local offline copy, if we have one, means it plays even with no
        # signal. (yt-dlp already gave us a live stream URL; prefer the file.)
        local = self._local_file(video_id)
        source = str(local) if local else url
        with self._lock:
            self._seed = video_id
            self._queue = []
            self._played = {video_id}
            self._fail_streak = 0  # a fresh ask gets a fresh chance
            self._user_paused = self._ducked = False
        proc = self._spawn(title, source, self._gen, video_id=video_id)
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
            self._current_id = ""
            self._gen += 1  # invalidate any monitor so it won't autoplay
            self._user_paused = self._ducked = False
            self._fav_mode = False   # a stop ends any favourites run
            self._fav_queue = []
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
    def _spawn(self, title: str, url: str, gen: int, video_id: str = "",
               record: bool = True) -> subprocess.Popen | None:
        """Start mpv on `url` (a stream URL or a local file) and a thread that
        reacts when it ends.

        `record` appends this track to the play history (a forward play); the
        history-navigation methods pass False so replaying an earlier song
        doesn't push a new entry. Returns None if the run was superseded (user
        stopped / played anew) between resolving `url` and here.
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
            self._current_id = video_id
            self._stderr_tail = collections.deque(maxlen=5)
            if record and video_id:
                # Forward play: drop anything we'd navigated back past, then
                # append and point at the new tail.
                del self._history[self._hist_pos + 1:]
                self._history.append({"id": video_id, "title": title})
                self._hist_pos = len(self._history) - 1
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
        # A favourites run chains through saved songs regardless of the radio
        # autoplay toggle — the user asked for the whole set.
        with self._lock:
            fav_mode = self._fav_mode
        if fav_mode:
            self._advance_favourites(gen)
            return
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
            if self._spawn(nxt["title"], url, gen, video_id=nxt["id"]):
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

    # ── search (id + title only, for favouriting by name) ────────────────────
    def _search_one(self, query: str) -> tuple[str, str]:
        """Resolve a query to a single (video_id, title) without playing it."""
        try:
            out = subprocess.run(
                [*self._ytdlp, "-4", "--no-playlist",
                 "--print", "title", "--print", "id", f"ytsearch1:{query}"],
                capture_output=True, text=True, timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return "", ""
        lines = [line for line in (out.stdout or "").splitlines() if line.strip()]
        if out.returncode != 0 or len(lines) < 2:
            return "", ""
        return lines[1], lines[0]  # id, title

    # ── offline copies (survive the dead zones) ──────────────────────────────
    def _local_file(self, video_id: str) -> Path | None:
        """A downloaded audio file for `video_id`, if one exists on disk."""
        if not self._offline_dir or not video_id:
            return None
        try:
            for path in self._offline_dir.glob(f"{video_id}.*"):
                if path.is_file() and path.suffix != ".part":
                    return path
        except OSError:
            return None
        return None

    def _download_one(self, video_id: str) -> bool:
        """Fetch the best audio for `video_id` into the offline dir. No
        re-encode (so no ffmpeg needed) — mpv plays the raw m4a/webm fine."""
        if not self._offline_dir:
            return False
        self._offline_dir.mkdir(parents=True, exist_ok=True)
        try:
            out = subprocess.run(
                [*self._ytdlp, "-4", "--no-playlist", "-f", "bestaudio/best",
                 "-o", str(self._offline_dir / "%(id)s.%(ext)s"),
                 f"https://www.youtube.com/watch?v={video_id}"],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            return False
        if out.returncode != 0:
            log.warning("offline download failed for %s: %s",
                        video_id, (out.stderr or "")[:200])
        return self._local_file(video_id) is not None

    def _download_batch(self, pending: list[dict]) -> None:
        ok = 0
        for fav in pending:
            if self._download_one(fav["id"]):
                ok += 1
                log.info("offline-saved: %s", fav["title"])
        log.info("offline download batch done: %d/%d saved", ok, len(pending))
        with self._lock:
            self._downloading = False

    def download_favourites(self) -> str:
        """Download every favourite that isn't offline yet, in the background."""
        if self._favourites is None or self._offline_dir is None:
            return "Offline downloads aren't set up on this device."
        favs = self._favourites.all()
        if not favs:
            return "You haven't saved any favourites yet — add some first."
        pending = [f for f in favs if self._local_file(f["id"]) is None]
        if not pending:
            return f"All {len(favs)} of your favourites are already saved offline."
        with self._lock:
            if self._downloading:
                return "I'm already downloading your favourites — hang on."
            self._downloading = True
        threading.Thread(target=self._download_batch, args=(pending,),
                         daemon=True, name="venom-fav-download").start()
        n = len(pending)
        return (f"Downloading {n} favourite{'s' if n != 1 else ''} for offline "
                "play — I'll keep going in the background, so they'll work with "
                "no signal.")

    # ── favourites ───────────────────────────────────────────────────────────
    def add_favourite(self, name: str = "") -> str:
        """Favourite the current song, or a named one (searched, not played)."""
        if self._favourites is None:
            return "Favourites aren't set up on this device."
        name = (name or "").strip()
        if not name:
            with self._lock:
                vid, title = self._current_id, self._title
                playing = self._proc is not None and self._proc.poll() is None
            if not playing or not vid:
                return ("Nothing's playing to favourite — say the song's name, "
                        "or play it first.")
            self._favourites.add(vid, title)
            return f"Added {title} to your favourites."
        vid, title = self._search_one(name)
        if not vid:
            return f"I couldn't find '{name}' on YouTube to favourite."
        self._favourites.add(vid, title)
        return f"Added {title} to your favourites."

    def remove_favourite(self, name: str) -> str:
        if self._favourites is None:
            return "Favourites aren't set up on this device."
        removed = self._favourites.remove(name)
        if not removed:
            return f"You don't have a favourite matching '{name}'."
        return f"Removed {name} from your favourites."

    def list_favourites(self) -> str:
        if self._favourites is None:
            return "Favourites aren't set up on this device."
        favs = self._favourites.all()
        if not favs:
            return "You haven't saved any favourites yet."
        offline = sum(1 for f in favs if self._local_file(f["id"]) is not None)
        titles = ", ".join(f["title"] for f in favs[:12])
        more = f" and {len(favs) - 12} more" if len(favs) > 12 else ""
        tail = f" {offline} of them saved offline." if self._offline_dir else ""
        return f"You have {len(favs)} favourites: {titles}{more}.{tail}"

    def play_favourite(self, name: str) -> str:
        """Play one saved song by name. We stored its id, so there's no search
        to run — it plays from the offline copy if we have one (works with no
        signal), otherwise it streams."""
        if self._favourites is None:
            return "Favourites aren't set up on this device."
        fav = self._favourites.find(name)
        if not fav:
            return (f"You don't have a favourite matching '{name}' — "
                    "want me to just play it from YouTube?")
        if self._play_track_id(fav["id"], fav["title"]):
            where = " (offline)" if self._local_file(fav["id"]) else ""
            return f"Playing {fav['title']}{where}."
        return (f"I couldn't play {fav['title']} — it's not saved offline and "
                "there's no signal right now.")

    def _offline_favourite(self, query: str) -> dict | None:
        """A saved song matching `query` that has an offline copy on disk."""
        if self._favourites is None:
            return None
        fav = self._favourites.find(query)
        if fav and self._local_file(fav["id"]) is not None:
            return fav
        return None

    def _offline_fallback(self, query: str) -> str | None:
        """When the YouTube search can't run (no signal), play a saved song by
        name from its offline copy. Returns a spoken result, or None if there's
        no offline match to fall back to."""
        fav = self._offline_favourite(query)
        if fav and self._play_track_id(fav["id"], fav["title"]):
            return f"No signal — playing your offline copy of {fav['title']}."
        return None

    def play_favourites(self, shuffle: bool = True) -> str:
        """Play through all favourites — offline copies first — chaining to the
        next when each finishes. The go-to for a long ride with no signal."""
        if self._favourites is None:
            return "Favourites aren't set up on this device."
        favs = self._favourites.all()
        if not favs:
            return "You haven't saved any favourites yet — add some first."
        order = favs[:]
        if shuffle:
            random.shuffle(order)
        self.stop()  # clears fav_mode; we re-arm it below under the fresh gen
        with self._lock:
            gen = self._gen
            self._fav_mode = True
            self._fav_queue = list(order)
            self._fail_streak = 0
            self._user_paused = self._ducked = False
        self._advance_favourites(gen)
        offline = sum(1 for f in favs if self._local_file(f["id"]) is not None)
        return (f"Playing your {len(favs)} favourites"
                f"{' (shuffled)' if shuffle else ''} — {offline} offline.")

    def _advance_favourites(self, gen: int) -> None:
        """Play the next favourite in the queue (local file if we have one)."""
        while True:
            with self._lock:
                if gen != self._gen:
                    return  # user stepped in
                nxt = self._fav_queue.pop(0) if self._fav_queue else None
                if nxt is None:
                    self._fav_mode = False
                    return
            local = self._local_file(nxt["id"])
            source = str(local) if local else self._resolve_url(nxt["id"])
            if not source:
                continue  # offline and no copy, or a dead id — skip it
            with self._lock:
                if gen != self._gen:
                    return
                self._seed = nxt["id"]
            if self._spawn(nxt["title"], source, gen, video_id=nxt["id"]):
                log.info("favourite: %s", nxt["title"])
                return
        # queue exhausted with nothing playable
        with self._lock:
            self._fav_mode = False

    # ── history: restart + previous ──────────────────────────────────────────
    def restart(self) -> str:
        """Play the current song again from the top (seek if we can, else
        reload — a reload keeps working when the IPC socket is down)."""
        with self._lock:
            title, vid = self._title, self._current_id
            playing = self._proc is not None and self._proc.poll() is None
        if not playing:
            return "Nothing is playing to restart."
        if self._ipc(["seek", 0, "absolute"]).get("error") == "success":
            self._ipc(["set_property", "pause", False])
            with self._lock:
                self._user_paused = self._ducked = False
            return f"Restarting {title} from the top."
        if vid and self._play_track_id(vid, title, record=False):
            return f"Restarting {title} from the top."
        return "I couldn't restart it — try playing it again."

    def play_previous(self) -> str:
        """Replay the song before the current one in this session's history."""
        with self._lock:
            if self._hist_pos <= 0:
                return "This is the first song — there's nothing before it."
            target = dict(self._history[self._hist_pos - 1])
            pos = self._hist_pos - 1
        if not self._play_track_id(target["id"], target["title"],
                                   record=False, hist_pos=pos):
            return f"I couldn't reload {target['title']} — try playing it again."
        return f"Going back to {target['title']}."

    def _play_track_id(self, video_id: str, title: str, *, record: bool = True,
                       hist_pos: int | None = None) -> bool:
        """Play a known track by id — local copy first, else stream. Reseeds the
        radio mix off it. Returns True if playback actually started."""
        self.stop()  # bumps the generation, ending the previous run cleanly
        local = self._local_file(video_id)
        source = str(local) if local else self._resolve_url(video_id)
        if not source:
            return False
        with self._lock:
            self._seed = video_id
            self._queue = []
            self._played = {video_id}
            self._fail_streak = 0
            self._user_paused = self._ducked = False
            if hist_pos is not None:
                self._hist_pos = hist_pos
        return self._spawn(title, source, self._gen, video_id=video_id,
                           record=record) is not None

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
