"""MusicPlayer behavior — honest failures, skip, and autoplay give-up.

Real subprocesses (tiny `python -c` stand-ins for yt-dlp and mpv) exercise
the actual spawn/monitor paths; no network, no audio hardware.
"""

import sys
import time

from venom.music import MusicPlayer

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


def test_fresh_play_resets_fail_streak():
    player = MusicPlayer(ytdlp=FAKE_YTDLP, mpv=MPV_PLAYS)
    player.SPAWN_CHECK_SECONDS = 0.3
    player._fail_streak = MusicPlayer.MAX_FAILS
    try:
        player.play("anything")
        assert player._fail_streak == 0
    finally:
        player.stop()
