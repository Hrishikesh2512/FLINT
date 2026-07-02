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
        "check_timers", "set_volume", "save_memory", "end_conversation",
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


def test_build_system_instruction(tmp_path):
    from venom.live import build_system_instruction

    config = VenomConfig(gemini_api_key="k", memory_path=tmp_path / "m.json",
                         voice=VoiceConfig(user_name="Tushar"))
    memory = MemoryStore(config.memory_path)
    memory.remember("identity", "name", "Tushar")
    text = build_system_instruction(config, memory)
    assert "You are Venom" in text
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
