"""Typed configuration for the Venom daemon.

Read from a TOML file (default /etc/venom/venom.toml, override with
VENOM_CONFIG or --config). Every field has a working default so the daemon
boots on a freshly provisioned Pi with no config file at all.

Example venom.toml:

    [venom]
    poll_interval = 10.0
    status_path = "/run/venom/status.json"

    [internet]
    host = "1.1.1.1"
    port = 53

    [[brain]]
    name = "laptop"
    host = "192.168.1.50"
    port = 8765
    priority = 0

    [[brain]]
    name = "gemini"
    host = "generativelanguage.googleapis.com"
    port = 443
    priority = 10
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/venom/venom.toml")


@dataclass(frozen=True)
class BrainCandidate:
    """A place the wearable can send its audio/requests to."""

    name: str
    host: str
    port: int
    priority: int = 100  # lower wins

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("brain candidate needs a name")
        if not self.host:
            raise ValueError(f"brain candidate {self.name!r} needs a host")
        if not (0 < self.port < 65536):
            raise ValueError(f"brain candidate {self.name!r} has invalid port {self.port}")


# Cloud fallbacks that exist even with an empty config file: if the laptop
# is not configured or not reachable, any of these being reachable means
# "online, cloud brain available".
DEFAULT_CLOUD_CANDIDATES: tuple[BrainCandidate, ...] = (
    BrainCandidate("gemini", "generativelanguage.googleapis.com", 443, priority=10),
    BrainCandidate("groq", "api.groq.com", 443, priority=11),
    BrainCandidate("openai", "api.openai.com", 443, priority=12),
    BrainCandidate("anthropic", "api.anthropic.com", 443, priority=13),
    BrainCandidate("openrouter", "openrouter.ai", 443, priority=14),
)


@dataclass(frozen=True)
class AudioConfig:
    # "bluetooth": pair/connect the configured headset, route via PipeWire.
    # "usb": pick a USB sound card. "auto": bluetooth if configured, else usb.
    output: str = "auto"
    bluetooth_mac: str = ""
    bluetooth_name: str = ""
    noise_suppression: bool = True   # high-pass + gentle expander on the mic

    def __post_init__(self) -> None:
        if self.output not in ("auto", "bluetooth", "usb"):
            raise ValueError(f"audio.output must be auto|bluetooth|usb, got {self.output!r}")

    @property
    def bluetooth_configured(self) -> bool:
        return bool(self.bluetooth_mac or self.bluetooth_name)

    @property
    def use_bluetooth(self) -> bool:
        if self.output == "bluetooth":
            return True
        return self.output == "auto" and self.bluetooth_configured


@dataclass(frozen=True)
class VoiceConfig:
    enabled: bool = True
    wake_word: str = "hey_jarvis"      # openWakeWord pretrained model name
    wake_threshold: float = 0.6        # detection score 0..1
    inactivity_timeout: float = 45.0   # seconds of silence before session closes
    live_model: str = "models/gemini-2.5-flash-native-audio-preview-12-2025"
    voice_name: str = "Charon"   # warm male voice (Hinglish); Gemini prebuilt set
    user_name: str = "Boss"
    language: str = "en"
    # Silence (ms) after you stop talking before Venom treats your turn as
    # done and replies. Lower = snappier, but too low can cut you off during
    # a natural pause. Gemini's default is conservative (~1s+); 500 feels live.
    endpoint_silence_ms: int = 500
    # Model "thinking" budget in tokens. -1 = leave the model's default alone
    # (native-audio actually replies *worse* with thinking forced off). 0 =
    # force thinking off; a positive number caps it. Only applied when >= 0.
    thinking_budget: int = -1

    def __post_init__(self) -> None:
        if not (0.0 < self.wake_threshold <= 1.0):
            raise ValueError("wake_threshold must be in (0, 1]")
        if self.inactivity_timeout <= 0:
            raise ValueError("inactivity_timeout must be positive")
        if self.endpoint_silence_ms < 100:
            raise ValueError("endpoint_silence_ms must be at least 100")


@dataclass(frozen=True)
class VenomConfig:
    poll_interval: float = 10.0
    probe_timeout: float = 3.0
    status_path: Path = Path("/run/venom/status.json")
    memory_path: Path = Path("/var/lib/venom/memory.json")
    internet_host: str = "1.1.1.1"
    internet_port: int = 53
    web_enabled: bool = True   # browser console on the LAN
    web_port: int = 8787
    web_token: str = ""        # console access PIN; empty = open (dev only)
    gemini_api_key: str = ""
    brains: tuple[BrainCandidate, ...] = field(default=DEFAULT_CLOUD_CANDIDATES)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)

    def __post_init__(self) -> None:
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if self.probe_timeout <= 0:
            raise ValueError("probe_timeout must be positive")
        if not self.brains:
            raise ValueError("at least one brain candidate is required")

    @property
    def voice_ready(self) -> bool:
        return self.voice.enabled and bool(self.gemini_api_key)

    @property
    def internet_targets(self) -> tuple[tuple[str, int], ...]:
        """Reachability targets for the online check: the configured probe
        plus HTTPS fallbacks, so a network that blocks port 53 (common on
        hotspots) is still correctly seen as online."""
        return (
            (self.internet_host, self.internet_port),
            ("1.1.1.1", 443),
            ("8.8.8.8", 443),
            ("google.com", 443),
        )


def _parse_brains(raw: list[dict]) -> tuple[BrainCandidate, ...]:
    brains = [
        BrainCandidate(
            name=str(entry.get("name", "")),
            host=str(entry.get("host", "")),
            port=int(entry.get("port", 0)),
            priority=int(entry.get("priority", 100)),
        )
        for entry in raw
    ]
    return tuple(sorted(brains, key=lambda b: b.priority))


def _read_token_file() -> str:
    """The console PIN provisioning drops in the state dir (survives config
    rewrites and is readable over SSH: `cat /var/lib/venom/web_token`)."""
    try:
        return Path("/var/lib/venom/web_token").read_text().strip()
    except OSError:
        return ""


def load_config(path: Path | None = None) -> VenomConfig:
    """Load config from TOML; missing file or missing keys fall back to defaults."""
    if path is None:
        path = Path(os.environ.get("VENOM_CONFIG", str(DEFAULT_CONFIG_PATH)))

    data: dict = {}
    if path.exists():
        with open(path, "rb") as fh:
            data = tomllib.load(fh)

    # Runtime overrides written by the web console (venom can write its own
    # state dir, but /etc is sealed by systemd hardening). One level deep.
    override_path = Path(os.environ.get("VENOM_OVERRIDE",
                                        "/var/lib/venom/override.toml"))
    try:
        with open(override_path, "rb") as fh:
            for section, values in tomllib.load(fh).items():
                if isinstance(values, dict):
                    data.setdefault(section, {}).update(values)
    except (OSError, tomllib.TOMLDecodeError):
        pass

    if not data:
        return VenomConfig()

    venom = data.get("venom", {})
    internet = data.get("internet", {})
    gemini = data.get("gemini", {})
    voice = data.get("voice", {})
    audio = data.get("audio", {})
    raw_brains = data.get("brain", [])

    brains = _parse_brains(raw_brains) if raw_brains else DEFAULT_CLOUD_CANDIDATES

    voice_defaults = VoiceConfig()
    return VenomConfig(
        poll_interval=float(venom.get("poll_interval", 10.0)),
        probe_timeout=float(venom.get("probe_timeout", 3.0)),
        status_path=Path(venom.get("status_path", "/run/venom/status.json")),
        memory_path=Path(venom.get("memory_path", "/var/lib/venom/memory.json")),
        internet_host=str(internet.get("host", "1.1.1.1")),
        internet_port=int(internet.get("port", 53)),
        web_enabled=bool(data.get("web", {}).get("enabled", True)),
        web_port=int(data.get("web", {}).get("port", 8787)),
        web_token=str(
            os.environ.get("VENOM_WEB_TOKEN", "").strip()
            or data.get("web", {}).get("token", "")
            or _read_token_file()
        ).strip(),
        gemini_api_key=(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or str(gemini.get("api_key", "")).strip()
        ),
        brains=brains,
        voice=VoiceConfig(
            enabled=bool(voice.get("enabled", voice_defaults.enabled)),
            wake_word=str(voice.get("wake_word", voice_defaults.wake_word)),
            wake_threshold=float(voice.get("wake_threshold", voice_defaults.wake_threshold)),
            inactivity_timeout=float(
                voice.get("inactivity_timeout", voice_defaults.inactivity_timeout)),
            endpoint_silence_ms=int(
                voice.get("endpoint_silence_ms", voice_defaults.endpoint_silence_ms)),
            thinking_budget=int(
                voice.get("thinking_budget", voice_defaults.thinking_budget)),
            live_model=str(voice.get("live_model", voice_defaults.live_model)),
            voice_name=str(voice.get("voice_name", voice_defaults.voice_name)),
            user_name=str(voice.get("user_name", voice_defaults.user_name)),
            language=str(voice.get("language", voice_defaults.language)),
        ),
        audio=AudioConfig(
            output=str(audio.get("output", "auto")),
            bluetooth_mac=str(audio.get("bluetooth_mac", "")).strip(),
            bluetooth_name=str(audio.get("bluetooth_name", "")).strip(),
            noise_suppression=bool(audio.get("noise_suppression", True)),
        ),
    )
