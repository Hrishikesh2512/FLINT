"""Typed configuration for the Venom daemon.

Read from a TOML file (default /etc/venom/venom.toml, override with
VENOM_CONFIG or --config). Every field has a working default so the daemon
boots on a freshly provisioned Pi with no config file at all.

Example venom.toml:

    [venom]
    poll_interval = 30.0
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
    voice_name: str = "Leda"     # warm female voice (Hinglish); Gemini prebuilt set
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
    # Native-audio human-realism knobs (Gemini native-audio only).
    # affective_dialog: she hears the *emotion* in your voice (tone, pace,
    #   mood) and adapts how she speaks, not just the words.
    # FIXED (Fix 1): default is now False. affective_dialog forces the whole
    #   session onto the v1alpha preview endpoint (v1beta rejects
    #   enableAffectiveDialog), and v1alpha is measurably higher-latency —
    #   the confirmed 10s-reply culprit. Support is unchanged; it is now
    #   opt-in via `affective_dialog = true` in venom.toml instead of opt-out.
    affective_dialog: bool = False
    # proactive_audio: she decides when NOT to reply — ignores stray noise and
    #   talk not aimed at her instead of dutifully answering everything. More
    #   human, but adds a decision beat, so off by default (snappiness).
    proactive_audio: bool = False
    # Sampling temperature. None = leave the model default. A mild bump adds
    # natural variety so she doesn't say things the same way twice.
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not (0.0 < self.wake_threshold <= 1.0):
            raise ValueError("wake_threshold must be in (0, 1]")
        if self.inactivity_timeout <= 0:
            raise ValueError("inactivity_timeout must be positive")
        if self.endpoint_silence_ms < 100:
            raise ValueError("endpoint_silence_ms must be at least 100")


@dataclass(frozen=True)
class ScreenConfig:
    """The laptop screen-text server the Pi reads on demand.

    Jarvis (native-audio) is blind to images but reads text, so instead of
    streaming a picture we OCR the laptop's active window locally and pull the
    resulting text over the LAN when the user says "look at my screen".
    Off unless a host is configured.
    """

    enabled: bool = True
    host: str = ""          # laptop LAN/Tailscale address; empty = feature off
    port: int = 8766
    token: str = ""         # must match the screen server's --token
    timeout: float = 5.0    # seconds to wait for the OCR round-trip

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.host)


@dataclass(frozen=True)
class ButtonsConfig:
    """Bluetooth camera-shutter remote: two buttons, identified by their evdev
    key code. 0 = not yet mapped — press the button once and read the logged
    `unmapped key code N`, then set it here. The headset button needs no config
    (its play/pause code is already a known wake code)."""

    dnd_code: int = 0    # shutter button 1: toggle do-not-disturb
    wake_code: int = 0   # shutter button 2: wake Venom (a physical wake button)


@dataclass(frozen=True)
class PhoneConfig:
    """Find-my-phone over ntfy. Subscribe your phone's ntfy app to `ntfy_topic`
    (give it a loud/alarm sound) and shutter button 2 rings it. Off until a
    topic is set."""

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    # A SEPARATE topic for phone→Pi notifications (WhatsApp etc.), forwarded by a
    # MacroDroid/Tasker automation on the phone. Off until set. Keep distinct
    # from ntfy_topic so find-my-phone alerts aren't read back as messages.
    notify_topic: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.ntfy_topic)


@dataclass(frozen=True)
class VenomConfig:
    # FIXED (Fix 8): poll_interval 10 -> 30 (the brain checker was probing every
    # 10s, far too often for a stable link and the source of mid-conversation
    # flaps); probe_timeout 3 -> 5 (3s is too tight for an India->US TCP
    # handshake, so a healthy Gemini read as "down" on latency blips).
    poll_interval: float = 30.0
    probe_timeout: float = 5.0
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
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)

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
    screen = data.get("screen", {})
    buttons = data.get("buttons", {})
    phone = data.get("phone", {})
    raw_brains = data.get("brain", [])

    brains = _parse_brains(raw_brains) if raw_brains else DEFAULT_CLOUD_CANDIDATES

    voice_defaults = VoiceConfig()
    return VenomConfig(
        # FIXED (Fix 8): keep TOML fallbacks in step with the dataclass
        # defaults — 30s poll, 5s probe timeout.
        poll_interval=float(venom.get("poll_interval", 30.0)),
        probe_timeout=float(venom.get("probe_timeout", 5.0)),
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
            affective_dialog=bool(
                voice.get("affective_dialog", voice_defaults.affective_dialog)),
            proactive_audio=bool(
                voice.get("proactive_audio", voice_defaults.proactive_audio)),
            temperature=(
                float(voice["temperature"])
                if voice.get("temperature") is not None
                else voice_defaults.temperature),
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
        screen=ScreenConfig(
            enabled=bool(screen.get("enabled", True)),
            host=str(screen.get("host", "")).strip(),
            port=int(screen.get("port", 8766)),
            token=str(screen.get("token", "")).strip(),
            timeout=float(screen.get("timeout", 5.0)),
        ),
        buttons=ButtonsConfig(
            dnd_code=int(buttons.get("dnd_code", 0)),
            wake_code=int(buttons.get("wake_code", 0)),
        ),
        phone=PhoneConfig(
            ntfy_server=str(phone.get("ntfy_server", "https://ntfy.sh")).strip(),
            ntfy_topic=str(phone.get("ntfy_topic", "")).strip(),
            notify_topic=str(phone.get("notify_topic", "")).strip(),
        ),
    )
