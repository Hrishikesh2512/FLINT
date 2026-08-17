"""MusicPlayer behavior — honest failures, skip, and autoplay give-up.

Real subprocesses (tiny `python -c` stand-ins for yt-dlp and mpv) exercise
the actual spawn/monitor paths; no network, no audio hardware.
"""

import sys
import time

from venom.live import collapse_doubled
from venom.music import MusicPlayer
from venom.stores import FavouritesStore


def test_collapse_doubled_transcript():
    line = "Skip kar diya maine. Aur kya chahiye?"
    assert collapse_doubled(line + line) == line
    assert collapse_doubled(line) == line
    assert collapse_doubled("haan haan") == "haan haan"  # not an exact doubling
    assert collapse_doubled("") == ""

# A yt-dlp stand-in: prints title / id / url like the real search does.
FAKE_YTDLP = [sys.executable, "-c",
              "print('Fake Song'); print('vid123'); print('http://x/u')"]
# A yt-dlp stand-in whose search fails — stands in for "no signal".
YTDLP_FAILS = [sys.executable, "-c", "import sys; sys.exit(1)"]
# mpv stand-ins.
MPV_DIES = [sys.executable, "-c", "import sys; sys.exit(2)"]
MPV_PLAYS = [sys.executable, "-c", "import time; time.sleep(30)"]


class FakeProc:
    """Stands in for a finished mpv process in _monitor tests."""

    def __init__(self, returncode=0, alive=False):
        self.returncode = returncode
        self._alive = alive
        self.terminated = False

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return None if self._alive else self.returncode

    def terminate(self):
        self.terminated = True
        self._alive = False


def test_play_reports_instant_mpv_death():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, mpv=MPV_DIES)
    player.SPAWN_CHECK_SECONDS = 0.3
    result = player.play("anything")
    assert "playback failed" in result
    assert "Fake Song" in result


def test_play_succeeds_when_mpv_stays_alive():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, mpv=MPV_PLAYS)
    player.SPAWN_CHECK_SECONDS = 0.3
    try:
        assert player.play("anything") == "Playing Fake Song."
        assert player.playing
        assert player.now_playing == "Fake Song"
    finally:
        player.stop()
    assert not player.playing


def test_monitor_counts_instant_deaths_and_gives_up():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=True)
    player._seed = "vid123"
    attempts = []
    player._autoplay_next = lambda gen, seed: attempts.append(seed)

    for _ in range(MusicPlayer.MAX_FAILS):
        player._monitor(FakeProc(returncode=2), player._gen, time.monotonic())

    # The first failures still try the next similar track; the last one stops.
    assert len(attempts) == MusicPlayer.MAX_FAILS - 1


def test_monitor_treats_long_run_as_natural_finish():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=True)
    player._seed = "vid123"
    player._fail_streak = MusicPlayer.MAX_FAILS - 1
    attempts = []
    player._autoplay_next = lambda gen, seed: attempts.append(seed)

    started = time.monotonic() - MusicPlayer.MIN_PLAY_SECONDS - 1
    player._monitor(FakeProc(returncode=0), player._gen, started)

    assert attempts == ["vid123"]
    assert player._fail_streak == 0  # a real playthrough clears the streak


def test_monitor_skip_is_not_a_failure():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=True)
    player._seed = "vid123"
    player._skipping = True
    attempts = []
    player._autoplay_next = lambda gen, seed: attempts.append(seed)

    # Terminated instantly with a nonzero code — but it was a user skip.
    player._monitor(FakeProc(returncode=1), player._gen, time.monotonic())

    assert attempts == ["vid123"]
    assert player._fail_streak == 0
    assert player._skipping is False


def test_monitor_superseded_never_autoplays():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=True)
    player._seed = "vid123"
    attempts = []
    player._autoplay_next = lambda gen, seed: attempts.append(seed)

    player._monitor(FakeProc(returncode=0), player._gen - 1,
                    time.monotonic() - 60)
    assert attempts == []


def test_skip_nothing_playing():
    player = MusicPlayer(ytdlp=FAKE_YTDLP)
    assert player.skip() == "Nothing is playing."


def test_skip_with_autoplay_off_explains():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=False)
    player._proc = FakeProc(alive=True)
    player._seed = "vid123"
    assert "Autoplay is off" in player.skip()
    assert not player._proc.terminated


def test_skip_terminates_and_flags_intent():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, autoplay=True)
    proc = FakeProc(alive=True)
    player._proc = proc
    player._seed = "vid123"
    assert "next song" in player.skip()
    assert proc.terminated
    assert player._skipping is True


def make_paused_player(playing=True, paused=False):
    """A player with a fake mpv IPC that remembers its pause state."""
    player = MusicPlayer(ytdlp=FAKE_YTDLP)
    if playing:
        player._proc = FakeProc(alive=True)
    state = {"pause": paused}

    def fake_ipc(command):
        if command[0] == "set_property" and command[1] == "pause":
            state["pause"] = bool(command[2])
            return {"error": "success"}
        if command[0] == "get_property" and command[1] == "pause":
            return {"data": state["pause"], "error": "success"}
        return {}

    player._ipc = fake_ipc
    return player, state


def test_unduck_resumes_what_duck_paused():
    player, state = make_paused_player()
    assert player.duck() is True
    assert state["pause"] is True
    assert player.unduck() is True
    assert state["pause"] is False


def test_user_pause_survives_the_conversation():
    # The tonight-bug: duck paused it, the user said "pause the music",
    # and unduck used to resume it anyway seconds later.
    player, state = make_paused_player()
    player.duck()
    assert "Paused" in player.set_paused(True)   # explicit user pause
    assert player.unduck() is False              # their word wins
    assert state["pause"] is True                # still paused


def test_user_resume_mid_conversation_sticks():
    player, state = make_paused_player()
    player.duck()
    assert "Resumed" in player.set_paused(False)  # "chalao na" mid-chat
    assert state["pause"] is False
    assert player.unduck() is False               # nothing left to unduck
    assert state["pause"] is False


def test_duck_ignores_user_paused_music():
    player, state = make_paused_player(paused=True)
    assert player.duck() is False
    assert player.unduck() is False
    assert state["pause"] is True


def test_set_paused_reports_dead_ipc_honestly():
    player = MusicPlayer(ytdlp=FAKE_YTDLP)
    player._proc = FakeProc(alive=True)
    player._ipc = lambda command: {}  # socket dead
    assert "couldn't control" in player.set_paused(True)


def test_fresh_play_resets_fail_streak():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, mpv=MPV_PLAYS)
    player.SPAWN_CHECK_SECONDS = 0.3
    player._fail_streak = MusicPlayer.MAX_FAILS
    try:
        player.play("anything")
        assert player._fail_streak == 0
    finally:
        player.stop()


# ── favourites, restart, previous, offline (train-journey features) ───────────
def fav_player(tmp_path, ytdlp=FAKE_YTDLP, **kw):
    """A player wired to a real FavouritesStore and an offline dir in tmp."""
    favs = FavouritesStore(tmp_path / "fav.json")
    player = MusicPlayer(ytdlp=ytdlp, favourites=favs,
                         offline_dir=tmp_path / "music", **kw)
    return player, favs


def _offline_file(tmp_path, video_id):
    music = tmp_path / "music"
    music.mkdir(exist_ok=True)
    (music / f"{video_id}.m4a").write_bytes(b"fake audio")


def test_add_favourite_current_song(tmp_path):
    player, favs = fav_player(tmp_path)
    player._proc = FakeProc(alive=True)
    player._current_id, player._title = "abc", "My Song"
    assert "Added My Song" in player.add_favourite()
    assert favs.is_favourite("abc")


def test_add_favourite_by_name_searches(tmp_path):
    player, favs = fav_player(tmp_path)          # FAKE_YTDLP → vid123 / Fake Song
    assert "Added Fake Song" in player.add_favourite("anything")
    assert favs.is_favourite("vid123")


def test_add_favourite_nothing_playing(tmp_path):
    player, favs = fav_player(tmp_path)
    assert "Nothing's playing" in player.add_favourite()
    assert favs.all() == []


def test_remove_favourite(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("a", "Kesariya")
    assert "Removed" in player.remove_favourite("kesariya")
    assert favs.all() == []
    assert "don't have a favourite" in player.remove_favourite("nope")


def test_list_favourites_counts_offline(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("vid123", "Fake Song")
    favs.add("b", "Other Song")
    _offline_file(tmp_path, "vid123")
    msg = player.list_favourites()
    assert "2 favourites" in msg and "Fake Song" in msg
    assert "1 of them saved offline" in msg


def test_local_file_prefers_offline_copy(tmp_path):
    player, _ = fav_player(tmp_path)
    assert player._local_file("vid123") is None
    _offline_file(tmp_path, "vid123")
    assert player._local_file("vid123").name == "vid123.m4a"


def test_status_tags_favourite_and_offline(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("vid123", "Fake Song")
    _offline_file(tmp_path, "vid123")
    player._proc = FakeProc(alive=True)
    player._current_id, player._title = "vid123", "Fake Song"
    s = player.status()
    assert "Fake Song" in s and "favourite" in s and "offline" in s


def test_status_nothing_playing(tmp_path):
    player, _ = fav_player(tmp_path)
    assert player.status() == "Nothing is playing."


def test_download_favourites_no_favourites(tmp_path):
    player, _ = fav_player(tmp_path)
    assert "haven't saved any favourites" in player.download_favourites()


def test_download_favourites_all_already_offline(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("vid123", "Fake Song")
    _offline_file(tmp_path, "vid123")
    assert "already saved offline" in player.download_favourites()


def test_restart_seeks_when_ipc_alive(tmp_path):
    player, _ = fav_player(tmp_path)
    player._proc = FakeProc(alive=True)
    player._current_id, player._title = "abc", "My Song"
    calls = []
    player._ipc = lambda cmd: (calls.append(cmd), {"error": "success"})[1]
    assert "Restarting My Song" in player.restart()
    assert ["seek", 0, "absolute"] in calls


def test_restart_reloads_when_ipc_dead(tmp_path):
    player, _ = fav_player(tmp_path)
    player._proc = FakeProc(alive=True)
    player._current_id, player._title = "abc", "My Song"
    player._ipc = lambda cmd: {}                 # socket down → no seek
    reloaded = []
    player._play_track_id = lambda vid, title, **kw: (
        reloaded.append((vid, title)) or True)
    assert "Restarting My Song" in player.restart()
    assert reloaded == [("abc", "My Song")]


def test_restart_nothing_playing(tmp_path):
    player, _ = fav_player(tmp_path)
    assert player.restart() == "Nothing is playing to restart."


def test_previous_replays_prior_song(tmp_path):
    player, _ = fav_player(tmp_path)
    player._history = [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}]
    player._hist_pos = 1
    played = []
    player._play_track_id = lambda vid, title, **kw: (
        played.append((vid, title, kw)) or True)
    assert "Going back to A" in player.play_previous()
    assert played[0][0] == "a" and played[0][2]["hist_pos"] == 0


def test_previous_at_first_song(tmp_path):
    player, _ = fav_player(tmp_path)
    player._history = [{"id": "a", "title": "A"}]
    player._hist_pos = 0
    assert "first song" in player.play_previous()


def test_play_records_history_and_current_id(tmp_path):
    player, _ = fav_player(tmp_path, mpv=MPV_PLAYS)
    player.SPAWN_CHECK_SECONDS = 0.3
    try:
        assert "Playing Fake Song" in player.play("anything")
        assert player._current_id == "vid123"
        assert player._history[-1]["id"] == "vid123"
        assert player._hist_pos == 0
    finally:
        player.stop()


def test_play_favourites_empty(tmp_path):
    player, _ = fav_player(tmp_path)
    assert "haven't saved any favourites" in player.play_favourites()


def test_play_favourites_starts_first_and_queues_rest(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("a", "Song A")
    favs.add("b", "Song B")
    spawned = []
    player._resolve_url = lambda vid: f"http://x/{vid}"

    def fake_spawn(title, url, gen, video_id="", record=True):
        spawned.append((title, video_id))
        player._proc = FakeProc(alive=True)
        return player._proc

    player._spawn = fake_spawn
    msg = player.play_favourites(shuffle=False)
    assert "Playing your 2 favourites" in msg
    assert player._fav_mode is True
    assert spawned == [("Song A", "a")]     # first one started
    assert player._fav_queue == [{"id": "b", "title": "Song B", "added":
                                  player._fav_queue[0]["added"]}]


def test_favourites_run_chains_on_finish(tmp_path):
    player, favs = fav_player(tmp_path)
    favs.add("a", "Song A")
    favs.add("b", "Song B")
    player._resolve_url = lambda vid: f"http://x/{vid}"
    spawned = []

    def fake_spawn(title, url, gen, video_id="", record=True):
        spawned.append(video_id)
        return FakeProc(alive=True)

    player._spawn = fake_spawn
    player.play_favourites(shuffle=False)     # starts "a", queues "b"
    # Simulate "a" finishing naturally → the monitor advances the run.
    player._monitor(FakeProc(returncode=0),
                    player._gen, time.monotonic() - 60)
    assert spawned == ["a", "b"]
    assert player._fav_queue == []


def test_stop_ends_favourites_run(tmp_path):
    player, _ = fav_player(tmp_path)
    player._fav_mode = True
    player._fav_queue = [{"id": "b", "title": "B"}]
    player.stop()
    assert player._fav_mode is False
    assert player._fav_queue == []


# ── play one specific song offline ────────────────────────────────────────────
def test_play_favourite_by_name_offline(tmp_path):
    player, favs = fav_player(tmp_path, mpv=MPV_PLAYS)
    favs.add("vid123", "Fake Song")
    _offline_file(tmp_path, "vid123")            # downloaded → no network needed
    msg = player.play_favourite("fake")
    assert "Playing Fake Song (offline)" in msg
    assert player._current_id == "vid123"
    player.stop()


def test_play_favourite_not_saved(tmp_path):
    player, favs = fav_player(tmp_path)
    assert "don't have a favourite matching" in player.play_favourite("nope")


def test_play_favourite_no_copy_no_signal(tmp_path):
    # Favourited but never downloaded, and the resolve (network) fails.
    player, favs = fav_player(tmp_path)
    favs.add("vid123", "Fake Song")
    player._resolve_url = lambda vid: ""         # no signal
    assert "not saved offline" in player.play_favourite("fake")


def test_play_music_falls_back_to_offline_when_search_fails(tmp_path):
    # yt-dlp search "fails" (stand-in exits nonzero) → offline copy plays.
    player, favs = fav_player(tmp_path, ytdlp=YTDLP_FAILS, mpv=MPV_PLAYS)
    favs.add("vid123", "Fake Song")
    _offline_file(tmp_path, "vid123")
    msg = player.play("fake song")
    assert "offline copy of Fake Song" in msg
    assert player._current_id == "vid123"
    player.stop()


def test_play_music_no_fallback_without_offline_copy(tmp_path):
    player, favs = fav_player(tmp_path, ytdlp=YTDLP_FAILS)
    favs.add("vid123", "Fake Song")             # favourited but not downloaded
    assert "couldn't find" in player.play("fake song")
