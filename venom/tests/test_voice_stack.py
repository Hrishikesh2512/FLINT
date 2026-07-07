"""Tests for the voice pipeline logic — no audio hardware, no network."""

import pytest

from flint_core.memory import MemoryStore
from venom.audio.devices import DevicePick, pick_devices
from venom.audio.streams import SPEAKER_SAMPLE_RATE, SpeakerStream, chime
from venom.config import VenomConfig, VoiceConfig
from venom.tools_pi import TimerBoard, build_pi_registry, fetch_weather, set_alsa_volume
from venom.wake import WAKE_FRAME_BYTES, InactivityTimer, WakeWordDetector


# ── device selection ─────────────────────────────────────────────────────────
def test_pick_prefers_usb_headset():
    table = [
        {"name": "HDMI Audio", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "bcm2835 Headphones", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "USB PnP Sound Device", "max_input_channels": 1, "max_output_channels": 2},
    ]
    pick = pick_devices(table)
    assert pick.input_index == 2 and pick.output_index == 2
    assert "USB" in pick.input_name


def test_pick_falls_back_to_default():
    table = [
        {"name": "Built-in Mic", "max_input_channels": 2, "max_output_channels": 0},
        {"name": "Built-in Output", "max_input_channels": 0, "max_output_channels": 2},
    ]
    pick = pick_devices(table)
    assert pick.input_index is None and pick.output_index is None
    assert pick.input_name == "(system default)"


def test_pick_empty_table():
    pick = pick_devices([])
    assert pick.input_name == "(none found)"


def test_pick_usb_prefers_pipewire_bridge():
    # With PipeWire present, USB mode routes through it (resampling), not the
    # raw hw device that rejects our sample rates.
    table = [
        {"name": "USB PnP Sound Device", "max_input_channels": 1, "max_output_channels": 2},
        {"name": "pipewire", "max_input_channels": 1, "max_output_channels": 2},
    ]
    pick = pick_devices(table)  # bluetooth=False → USB tiers
    assert "pipewire" in pick.input_name.lower()
    assert "pipewire" in pick.output_name.lower()


def test_find_usb_nodes_picks_usb_over_bluetooth():
    from venom.audio.routing import find_usb_nodes

    objects = [
        {"id": 54, "info": {"props": {
            "media.class": "Audio/Sink",
            "node.description": "USB-Audio-1.0 Analog Stereo"}}},
        {"id": 55, "info": {"props": {
            "media.class": "Audio/Source", "node.name": "alsa_input.usb-xyz"}}},
        {"id": 73, "info": {"props": {
            "media.class": "Audio/Sink", "node.description": "AirBass Headphone"}}},
    ]
    assert find_usb_nodes(objects) == {"sink": 54, "source": 55}


# ── wake word framing + endpointing ──────────────────────────────────────────
class FakeOwwModel:
    def __init__(self, hot_on_call: int):
        self.calls = 0
        self.hot_on_call = hot_on_call

    def predict(self, _audio):
        self.calls += 1
        return {"hey_jarvis": 0.9 if self.calls == self.hot_on_call else 0.01}

    def reset(self):
        self.calls = 0


def test_detector_buffers_partial_frames():
    detector = WakeWordDetector(threshold=0.6)
    detector._model = FakeOwwModel(hot_on_call=3)
    half = WAKE_FRAME_BYTES // 2
    assert detector.feed(b"\x00" * half) is False          # 0 full frames
    assert detector.feed(b"\x00" * half) is False          # 1st frame scored
    assert detector.feed(b"\x00" * WAKE_FRAME_BYTES) is False  # 2nd
    assert detector.feed(b"\x00" * WAKE_FRAME_BYTES) is True   # 3rd → hot
    assert detector._model.calls == 3


def test_detector_requires_load():
    with pytest.raises(RuntimeError):
        WakeWordDetector().feed(b"\x00" * WAKE_FRAME_BYTES)


def test_normalize_boosts_quiet_speech_leaves_silence_and_loud():
    import numpy as np

    quiet = np.full(1280, 900, dtype=np.int16)     # soft speech
    boosted = WakeWordDetector._normalize(quiet)
    assert boosted.max() > 6000                     # amplified toward full scale

    silence = np.full(1280, 50, dtype=np.int16)     # below the noise floor
    assert WakeWordDetector._normalize(silence).max() == 50

    loud = np.full(1280, 20000, dtype=np.int16)     # already strong
    assert WakeWordDetector._normalize(loud).max() == 20000


def test_inactivity_timer():
    now = [100.0]
    timer = InactivityTimer(timeout=10, clock=lambda: now[0])
    assert not timer.expired
    now[0] += 9
    assert not timer.expired
    timer.touch()
    now[0] += 9
    assert not timer.expired
    now[0] += 2
    assert timer.expired
    with pytest.raises(ValueError):
        InactivityTimer(timeout=0)


# ── timers ────────────────────────────────────────────────────────────────────
def test_timer_board():
    now = [0.0]
    board = TimerBoard(clock=lambda: now[0])
    board.add(1, "tea")
    board.add(5, "laundry")
    assert board.pop_due() == []
    assert len(board.pending()) == 2
    now[0] = 61
    due = board.pop_due()
    assert [t.label for t in due] == ["tea"]
    assert [label for label, _ in board.pending()] == ["laundry"]


# ── weather ───────────────────────────────────────────────────────────────────
class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def fake_get_factory(geo, forecast):
    def get(url, params=None, timeout=None):
        return FakeResponse(geo if "geocoding" in url else forecast)
    return get


def test_fetch_weather_happy_path():
    geo = {"results": [{"name": "Pune", "latitude": 18.5, "longitude": 73.9}]}
    forecast = {"current": {"temperature_2m": 29.1, "apparent_temperature": 31.0,
                            "relative_humidity_2m": 60, "weather_code": 2,
                            "wind_speed_10m": 8.2}}
    text = fetch_weather("Pune", get=fake_get_factory(geo, forecast))
    assert "Pune" in text and "partly cloudy" in text and "29.1" in text


def test_fetch_weather_unknown_city():
    text = fetch_weather("Xyzzy", get=fake_get_factory({"results": []}, {}))
    assert "couldn't find" in text


# ── volume (non-Linux simulation path) ────────────────────────────────────────
def test_set_volume_clamps_and_simulates_off_linux():
    assert "100%" in set_alsa_volume(250)
    assert "0%" in set_alsa_volume(-5)


# ── pi tool registry ──────────────────────────────────────────────────────────
@pytest.fixture()
def pi_setup(tmp_path):
    config = VenomConfig(gemini_api_key="test-key",
                         memory_path=tmp_path / "memory.json")
    memory = MemoryStore(config.memory_path)
    timers = TimerBoard(clock=lambda: 0.0)
    return build_pi_registry(config, memory, timers), memory, timers


def test_pi_registry_toolset(pi_setup):
    registry, _, _ = pi_setup
    assert set(registry.names()) == {
        "web_search", "weather_report", "current_time", "set_timer",
        "check_timers", "set_volume", "save_memory", "translation_mode",
        "end_conversation", "power_off",
    }


def test_pi_registry_dispatch(pi_setup):
    registry, memory, timers = pi_setup
    assert "It is" in registry.dispatch("current_time", {})
    assert "set for 3" in registry.dispatch("set_timer", {"minutes": 3, "label": "tea"})
    assert "tea" in registry.dispatch("check_timers", {})
    assert registry.dispatch("save_memory",
                             {"category": "identity", "key": "name", "value": "Tushar"}
                             ) == "remembered identity/name"
    assert memory.load()["identity"]["name"]["value"] == "Tushar"
    assert registry.dispatch("end_conversation", {}) == "Ending conversation."


def test_music_tools_registered_and_dispatch(tmp_path):
    class FakeMusic:
        def __init__(self):
            self.now_playing = ""

        def play(self, query):
            self.now_playing = query
            return f"Playing {query}."

        def stop(self):
            self.now_playing = ""
            return "Music stopped."

    config = VenomConfig(gemini_api_key="k", memory_path=tmp_path / "m.json")
    music = FakeMusic()
    registry = build_pi_registry(config, MemoryStore(config.memory_path),
                                 TimerBoard(), music=music)
    assert {"play_music", "stop_music", "now_playing"} <= set(registry.names())
    assert registry.dispatch("play_music", {"query": "Kesariya"}) == "Playing Kesariya."
    assert "Kesariya" in registry.dispatch("now_playing", {})
    assert registry.dispatch("stop_music", {}) == "Music stopped."
    assert registry.dispatch("now_playing", {}) == "Nothing is playing."


def test_duck_music_pauses_for_conversation_and_resumes_only_its_own():
    # Music and the mic share one headset, so a live conversation pauses our
    # own player. We must resume only what we paused — never a track the user
    # paused by hand, and never touch anything when nothing is playing.
    from types import SimpleNamespace

    from venom.voice import VoiceOrchestrator

    class FakeMusic:
        def __init__(self, playing, paused):
            self.playing, self.paused, self.calls = playing, paused, []

        def set_paused(self, p):
            self.calls.append(p)
            self.paused = p

    # playing and audible → paused for the turn, then resumed on the way out.
    o = SimpleNamespace(music=FakeMusic(True, False), _music_ducked=False)
    VoiceOrchestrator._duck_music(o)
    assert o._music_ducked and o.music.calls == [True]
    VoiceOrchestrator._unduck_music(o)
    assert not o._music_ducked and o.music.calls == [True, False]

    # already paused by the user → never touched, coming or going.
    o = SimpleNamespace(music=FakeMusic(True, True), _music_ducked=False)
    VoiceOrchestrator._duck_music(o)
    VoiceOrchestrator._unduck_music(o)
    assert not o._music_ducked and o.music.calls == []

    # nothing playing → no-op.
    o = SimpleNamespace(music=FakeMusic(False, False), _music_ducked=False)
    VoiceOrchestrator._duck_music(o)
    assert not o._music_ducked and o.music.calls == []


def test_button_routing():
    from venom.buttons import WAKE_CODES, route_key

    # Every headset play/pause code routes to wake.
    for code in WAKE_CODES:
        assert route_key(code, dnd_code=115, wake_code=114) == "wake"
    # Shutter button 1 (DND) and button 2 (a second wake) route accordingly.
    assert route_key(115, dnd_code=115, wake_code=114) == "dnd"
    assert route_key(114, dnd_code=115, wake_code=114) == "wake"
    # Unknown codes (and unmapped-when-zero) fall through to None for logging.
    assert route_key(999, dnd_code=115, wake_code=114) is None
    assert route_key(115, dnd_code=0, wake_code=0) is None


def test_find_phone_no_topic_is_safe():
    from venom.phone import find_phone

    # No topic configured → never touches the network, returns a clear message.
    assert find_phone("https://ntfy.sh", "") == "No phone is set up to find."
    assert find_phone("https://ntfy.sh", "   ") == "No phone is set up to find."


def test_find_my_phone_tool_registered_only_when_topic_set(tmp_path):
    from venom.config import PhoneConfig, VenomConfig

    base = dict(gemini_api_key="k", memory_path=tmp_path / "m.json")
    off = build_pi_registry(VenomConfig(**base),
                            MemoryStore(base["memory_path"]), TimerBoard())
    assert "find_my_phone" not in off.names()

    on = build_pi_registry(
        VenomConfig(**base, phone=PhoneConfig(ntfy_topic="venom-xyz")),
        MemoryStore(base["memory_path"]), TimerBoard())
    assert "find_my_phone" in on.names()


def test_dnd_toggle_and_wake_button_respect_dnd():
    from types import SimpleNamespace

    from venom.voice import VoiceOrchestrator

    class FakeSpeaker:
        def __init__(self):
            self.plays = 0

        def play(self, data):
            self.plays += 1

    class FakeEvent:
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            return self._set

    o = SimpleNamespace(_dnd=False, _speaker=FakeSpeaker(),
                        _manual_wake=FakeEvent(), _session=None)

    # Headset button while awake → requests a wake.
    VoiceOrchestrator._on_wake_button(o)
    assert o._manual_wake.is_set()

    # Toggle DND on → flag flips and a chime plays; headset button now ignored.
    o._manual_wake = FakeEvent()
    VoiceOrchestrator._on_dnd_button(o)
    assert o._dnd is True and o._speaker.plays > 0
    VoiceOrchestrator._on_wake_button(o)
    assert not o._manual_wake.is_set()      # ignored under DND

    # Toggle DND off again → flag clears, headset works once more.
    VoiceOrchestrator._on_dnd_button(o)
    assert o._dnd is False
    VoiceOrchestrator._on_wake_button(o)
    assert o._manual_wake.is_set()


def test_translation_mode_tool(tmp_path):
    from venom.config import VenomConfig

    config = VenomConfig(gemini_api_key="k", memory_path=tmp_path / "m.json")
    reg = build_pi_registry(config, MemoryStore(config.memory_path), TimerBoard())
    assert "translation_mode" in reg.names()
    on = reg.dispatch("translation_mode", {"enable": True})
    assert "TRANSLATION MODE ON" in on and "Hindi" in on and "Kannada" in on
    off = reg.dispatch("translation_mode", {"enable": False})
    assert "OFF" in off and "Jarvis" in off


def test_live_interrupt_flushes_and_suppresses():
    from types import SimpleNamespace

    from venom.live import LiveSession

    class FakeSpeaker:
        def __init__(self):
            self.flushed = 0

        def flush(self):
            self.flushed += 1

    o = SimpleNamespace(speaker=FakeSpeaker(), _suppress_output=False)
    LiveSession.interrupt(o)
    assert o.speaker.flushed == 1 and o._suppress_output is True


def test_wake_button_toggles_conversation():
    from types import SimpleNamespace

    from venom.voice import VoiceOrchestrator

    class FakeSession:
        def __init__(self, ended=False):
            self.ended = ended
            self.stopped = 0

        def request_stop(self):
            self.stopped += 1

    class FakeEvent:
        def __init__(self):
            self._set = False

        def set(self):
            self._set = True

        def is_set(self):
            return self._set

    # Live conversation → the wake button ends it, does not queue a wake.
    live = FakeSession(ended=False)
    o = SimpleNamespace(_dnd=False, _session=live, _manual_wake=FakeEvent())
    VoiceOrchestrator._on_wake_button(o)
    assert live.stopped == 1 and not o._manual_wake.is_set()

    # No conversation → the wake button queues a wake as before.
    o2 = SimpleNamespace(_dnd=False, _session=None, _manual_wake=FakeEvent())
    VoiceOrchestrator._on_wake_button(o2)
    assert o2._manual_wake.is_set()

    # An ended session is treated as no conversation (queues a wake).
    o3 = SimpleNamespace(_dnd=False, _session=FakeSession(ended=True),
                         _manual_wake=FakeEvent())
    VoiceOrchestrator._on_wake_button(o3)
    assert o3._manual_wake.is_set()

    # DND wins over everything — no stop, no wake.
    live2 = FakeSession(ended=False)
    o4 = SimpleNamespace(_dnd=True, _session=live2, _manual_wake=FakeEvent())
    VoiceOrchestrator._on_wake_button(o4)
    assert live2.stopped == 0 and not o4._manual_wake.is_set()


def test_pi_declarations_validate_against_gemini_sdk(pi_setup):
    genai_types = pytest.importorskip("google.genai.types")
    registry, _, _ = pi_setup
    config = genai_types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        tools=[{"function_declarations":
                registry.gemini_declarations(uppercase_types=True)}],
    )
    assert config.tools


# ── speaker chime + system prompt ─────────────────────────────────────────────
def test_chime_generates_bounded_pcm():
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    chime(speaker, duration=0.1)
    data = speaker._buffer.get_nowait()
    assert len(data) == int(SPEAKER_SAMPLE_RATE * 0.1) * 2
    assert max(abs(int.from_bytes(data[i:i + 2], "little", signed=True))
               for i in range(0, len(data), 2)) <= 32767 * 0.31


def test_speaker_flush():
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    speaker.play(b"\x01\x02" * 100)
    assert speaker.playing
    speaker.flush()
    assert not speaker.playing


# ── speaker jitter buffer ─────────────────────────────────────────────────────
def _ms_bytes(ms: float) -> int:
    return int(SPEAKER_SAMPLE_RATE * 2 * ms / 1000)


def test_speaker_prebuffers_after_underrun():
    """A trickle below the prebuffer threshold plays silence, not crackle."""
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    speaker.play(b"\x11\x11" * 100)  # 200 bytes ≈ 4 ms — way below threshold
    out = speaker._fill(2048)
    assert out == b"\x00" * 2048          # held back, not played
    assert speaker.playing                # ... and not dropped


def test_speaker_plays_once_prebuffer_met():
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    payload = b"\x11\x11" * (_ms_bytes(SpeakerStream.PREBUFFER_MS + 60) // 2)
    speaker.play(payload)
    out = speaker._fill(2048)
    assert out == payload[:2048]


def test_speaker_short_tail_plays_after_max_hold():
    """Sub-threshold audio (a clipped word ending) must still come out."""
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    tail = b"\x22\x22" * 100
    speaker.play(tail)
    block = _ms_bytes(SpeakerStream.MAX_HOLD_MS * 0.6)  # two calls cross the hold
    out = speaker._fill(block)
    assert out == b"\x00" * block         # first call still holds
    out = speaker._fill(block)
    assert out[:len(tail)] == tail        # released by the hold timeout


def test_speaker_rebuffers_after_drain():
    speaker = SpeakerStream(DevicePick(None, None, "t", "t"))
    speaker.play(b"\x11\x11" * (_ms_bytes(SpeakerStream.PREBUFFER_MS + 60) // 2))
    while speaker.playing:
        speaker._fill(4096)
    speaker.play(b"\x33\x33" * 10)        # next burst starts small again
    assert speaker._fill(1024) == b"\x00" * 1024  # back to prebuffering


# ── mic noise suppression ─────────────────────────────────────────────────────
def test_noise_suppressor_never_lengthens_or_crashes():
    import numpy as np

    from venom.audio.denoise import NoiseSuppressor

    ns = NoiseSuppressor()
    tone = (np.sin(np.linspace(0, 40 * np.pi, 1024)) * 8000).astype(np.int16)
    out = ns.process(tone.tobytes())
    assert len(out) == len(tone.tobytes())
    assert ns.process(b"") == b""          # empty frame is safe


def test_noise_suppressor_attenuates_dc_and_rumble():
    import numpy as np

    from venom.audio.denoise import NoiseSuppressor

    ns = NoiseSuppressor(expander=False)   # isolate the high-pass
    dc = np.full(1024, 6000, dtype=np.int16)
    # Prime the filter state, then measure a steady-state block.
    ns.process(dc.tobytes())
    out = np.frombuffer(ns.process(dc.tobytes()), dtype=np.int16)
    assert abs(int(out.mean())) < 500       # DC/low-freq largely removed


def test_noise_suppressor_passthrough_on_expander_open():
    import numpy as np

    from venom.audio.denoise import NoiseSuppressor

    ns = NoiseSuppressor(highpass_hz=20.0)  # keep speech band intact
    loud = (np.sin(np.linspace(0, 200 * np.pi, 1024)) * 12000).astype(np.int16)
    for _ in range(5):
        out = np.frombuffer(ns.process(loud.tobytes()), dtype=np.int16)
    assert float(np.abs(out).max()) > 5000  # loud speech is not gated away


def test_noise_gate_emits_silence_after_hangover_but_passes_speech():
    import numpy as np

    from venom.audio.denoise import NoiseSuppressor

    ns = NoiseSuppressor(highpass_hz=20.0, gate=True)  # enabled only mid-conversation
    rng = np.random.default_rng(0)
    quiet = (rng.standard_normal(1024) * 120).astype(np.int16)  # noise floor
    # Sustained quiet longer than the hangover (0.3s ≈ 5 frames of 64ms).
    outs = [np.frombuffer(ns.process(quiet.tobytes()), dtype=np.int16)
            for _ in range(12)]
    assert float(np.abs(outs[-1]).max()) == 0        # gate closed → true silence

    # A loud speech frame breaks the gate open again immediately.
    loud = (np.sin(np.linspace(0, 200 * np.pi, 1024)) * 12000).astype(np.int16)
    spoken = np.frombuffer(ns.process(loud.tobytes()), dtype=np.int16)
    assert float(np.abs(spoken).max()) > 5000        # not clipped by the gate


# ── multi-target internet probe ───────────────────────────────────────────────
def test_probe_any_succeeds_if_any_target_up():
    import asyncio

    from venom.monitors import network

    async def fake(host, port, timeout):
        await asyncio.sleep(0.01 if port == 443 else 0)
        return port == 443     # only HTTPS answers (port 53 blocked)

    async def go():
        return await network.probe_any(
            (("1.1.1.1", 53), ("1.1.1.1", 443)), timeout=1)

    orig, network.probe_tcp = network.probe_tcp, fake
    try:
        assert asyncio.run(go()) is True
    finally:
        network.probe_tcp = orig


def test_probe_any_false_when_all_down():
    import asyncio

    from venom.monitors import network

    async def fake(host, port, timeout):
        return False

    orig, network.probe_tcp = network.probe_tcp, fake
    try:
        assert asyncio.run(network.probe_any((("a", 53), ("b", 443)))) is False
        assert asyncio.run(network.probe_any(())) is False
    finally:
        network.probe_tcp = orig


# ── web console terminal ──────────────────────────────────────────────────────
def test_terminal_cd_tracks_directory(tmp_path):
    from venom.web import WebConsole

    console = WebConsole()
    console._cwd = str(tmp_path)
    sub = tmp_path / "child"
    sub.mkdir()

    assert console.terminal("cd child")["cwd"] == str(sub)
    assert console.terminal("cd ..")["cwd"] == str(tmp_path)
    # a bad path is reported, not applied
    r = console.terminal("cd nope")
    assert "not a directory" in r["out"] and r["cwd"] == str(tmp_path)
    # `cd -` returns to the previous directory
    console.terminal("cd child")
    assert console.terminal("cd -")["cwd"] == str(tmp_path)


# ── web console auth ──────────────────────────────────────────────────────────
def test_web_authorized_gate():
    from venom.web import WebConsole

    open_console = WebConsole(token="")
    assert open_console.authorized({})           # no token -> open

    locked = WebConsole(token="s3cret")
    assert not locked.authorized({})
    assert not locked.authorized({"Authorization": "Bearer wrong"})
    assert locked.authorized({"Authorization": "Bearer s3cret"})


# ── session teardown & dead-capture-path detection ───────────────────────────
def test_is_normal_closure():
    from venom.live import is_normal_closure

    class FakeAPIError(Exception):
        def __init__(self, code):
            self.code = code

    class ConnectionClosedOK(Exception):
        pass

    assert is_normal_closure(FakeAPIError(1000))
    assert is_normal_closure(FakeAPIError(1001))
    assert is_normal_closure(ConnectionClosedOK())
    assert not is_normal_closure(FakeAPIError(1011))
    assert not is_normal_closure(RuntimeError("boom"))


def test_silence_tracker_flags_bitexact_silence_only():
    from venom.voice import SilenceTracker

    tracker = SilenceTracker(limit_seconds=1.0, sample_rate=16000)
    zeros = b"\x00" * 8000          # 0.25 s of dead frames (binary-exact)
    noise = b"\x01\x00" * 4000      # 0.25 s with a real noise floor
    for _ in range(3):
        assert not tracker.update(zeros)
    assert tracker.update(zeros)     # 1.0 s of pure zeros -> dead path
    assert not tracker.update(noise)  # any signal resets it
    assert not tracker.update(zeros)


def test_build_system_instruction(tmp_path):
    from venom.live import build_system_instruction

    config = VenomConfig(gemini_api_key="k", memory_path=tmp_path / "m.json",
                         voice=VoiceConfig(user_name="Tushar"))
    memory = MemoryStore(config.memory_path)
    memory.remember("identity", "name", "Tushar")
    text = build_system_instruction(config, memory)
    assert "You are Jarvis" in text
    assert "Tushar" in text
    assert "CURRENT DATE" in text
    assert "WHAT YOU KNOW ABOUT THIS PERSON" in text


# ── voice config parsing ──────────────────────────────────────────────────────
def test_voice_config_from_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "venom.toml"
    path.write_text(
        """
[gemini]
api_key = "baked-key"

[voice]
wake_word = "alexa"
wake_threshold = 0.7
inactivity_timeout = 30
user_name = "Tushar"
""",
        encoding="utf-8",
    )
    from venom.config import load_config

    config = load_config(path)
    assert config.gemini_api_key == "baked-key"
    assert config.voice.wake_word == "alexa"
    assert config.voice.wake_threshold == 0.7
    assert config.voice_ready


def test_voice_not_ready_without_key(tmp_path):
    from venom.config import load_config

    config = load_config(tmp_path / "none.toml")
    assert not config.voice_ready
