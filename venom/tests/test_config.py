import pytest

from venom.config import (
    DEFAULT_CLOUD_CANDIDATES,
    BrainCandidate,
    VenomConfig,
    load_config,
)


def test_defaults_when_file_missing(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config.poll_interval == 30.0  # FIXED (Fix 8): default raised 10 -> 30
    assert config.brains == DEFAULT_CLOUD_CANDIDATES
    assert config.internet_host == "1.1.1.1"


def test_full_config_round_trip(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text(
        """
[venom]
poll_interval = 5.0
probe_timeout = 1.5
status_path = "/tmp/status.json"

[internet]
host = "9.9.9.9"
port = 443

[[brain]]
name = "cloud"
host = "api.example.com"
port = 443
priority = 10

[[brain]]
name = "laptop"
host = "192.168.1.50"
port = 8765
priority = 0
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.poll_interval == 5.0
    assert config.probe_timeout == 1.5
    assert str(config.status_path) in ("/tmp/status.json", "\\tmp\\status.json")
    assert config.internet_host == "9.9.9.9"
    # sorted by priority: laptop first
    assert [b.name for b in config.brains] == ["laptop", "cloud"]


def test_partial_config_keeps_default_brains(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text("[venom]\npoll_interval = 2.0\n", encoding="utf-8")
    config = load_config(path)
    assert config.poll_interval == 2.0
    assert config.brains == DEFAULT_CLOUD_CANDIDATES


def test_invalid_candidate_rejected():
    with pytest.raises(ValueError):
        BrainCandidate(name="bad", host="", port=443)
    with pytest.raises(ValueError):
        BrainCandidate(name="bad", host="x", port=0)


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        VenomConfig(poll_interval=0)
    with pytest.raises(ValueError):
        VenomConfig(brains=())


def test_endpoint_silence_default_and_parse(tmp_path):
    assert load_config(tmp_path / "nope.toml").voice.endpoint_silence_ms == 500
    path = tmp_path / "venom.toml"
    path.write_text("[voice]\nendpoint_silence_ms = 350\n", encoding="utf-8")
    assert load_config(path).voice.endpoint_silence_ms == 350


def test_endpoint_silence_rejects_too_low():
    from venom.config import VoiceConfig
    with pytest.raises(ValueError):
        VoiceConfig(endpoint_silence_ms=50)


def test_turn_detection_types_build():
    # Guards against google-genai API drift for the latency lever.
    from google.genai import types
    cfg = types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            silence_duration_ms=500))
    assert cfg.automatic_activity_detection.silence_duration_ms == 500


def test_thinking_budget_default_and_parse(tmp_path):
    # Default: leave the model's own thinking alone (-1), not forced off.
    assert load_config(tmp_path / "nope.toml").voice.thinking_budget == -1
    path = tmp_path / "venom.toml"
    path.write_text("[voice]\nthinking_budget = 0\n", encoding="utf-8")
    assert load_config(path).voice.thinking_budget == 0


def test_thinking_config_type_builds():
    from google.genai import types
    assert types.ThinkingConfig(thinking_budget=0).thinking_budget == 0


def test_buttons_and_phone_defaults(tmp_path):
    config = load_config(tmp_path / "nope.toml")
    assert config.buttons.dnd_code == 0
    assert config.buttons.wake_code == 0
    assert config.phone.ntfy_server == "https://ntfy.sh"
    assert config.phone.ntfy_topic == ""
    assert config.phone.ready is False


def test_buttons_and_phone_parse(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text(
        "[buttons]\ndnd_code = 115\nwake_code = 114\n"
        "[phone]\nntfy_topic = \"venom-abc123\"\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.buttons.dnd_code == 115
    assert config.buttons.wake_code == 114
    assert config.phone.ntfy_topic == "venom-abc123"
    assert config.phone.ready is True
