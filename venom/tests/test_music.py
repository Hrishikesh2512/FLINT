"""MusicPlayer behavior — honest failures, skip, and autoplay give-up.

Real subprocesses (tiny `python -c` stand-ins for yt-dlp and mpv) exercise
the actual spawn/monitor paths; no network, no audio hardware.
"""

import sys
import time

from venom.live import collapse_doubled
from venom.music import MusicPlayer


def test_collapse_doubled_transcript():
    line = "Skip kar diya maine. Aur kya chahiye?"
    assert collapse_doubled(line + line) == line
    assert collapse_doubled(line) == line
    assert collapse_doubled("haan haan") == "haan haan"  # not an exact doubling
    assert collapse_doubled("") == ""

# A yt-dlp stand-in: prints title / id / url like the real search does.
FAKE_YTDLP = [sys.executable, "-c",
              "print('Fake Song'); print('vid123'); print('http://x/u')"]
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
