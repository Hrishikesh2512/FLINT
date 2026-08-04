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
    # Bluetooth receive: the Pi doubles as a pairable Bluetooth headset — the
    # laptop/phone streams audio into the earphone and can use its mic for
    # calls ("pair my laptop"). Voice keeps working throughout.
    receiver: bool = True
    # Bluetooth focus: while external audio is streaming, keep the shared
    # radio for it — no pre-warmed Gemini session (its periodic re-warm is
    # a Wi-Fi burst you can hear as a stutter) and no wake word; the wake
    # button (or a console prompt) breaks in, and the stream ending
    # restores the normal cycle automatically.
    receiver_focus: bool = True
    # Mic on demand: idle in A2DP (speaker only) and take the microphone back
    # only for a conversation. HFP's SCO link reserves 2.4GHz airtime and, on
    # the Pi's shared antenna, collapses Wi-Fi throughput (~20 KB/s on HFP vs
    # ~370 on A2DP — see audio/routing.release_bluetooth_mic). The trade is
    # the wake word: with no mic there is nothing to hear it, so waking is by
    # button only. Off by default — it is a real change in how she is woken.
    mic_on_demand: bool = False

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
class CalendarConfig:
    """Google Calendar via its 'secret address in iCal format' — read-only,
    no OAuth, never expires. Venom answers 'what's on today?' and chimes
    proactively before events (same machinery as reminders)."""

    enabled: bool = True
    ical_url: str = ""       # the secret .ics URL; empty = feature off
    lead_minutes: int = 30   # announce upcoming events this early
    refresh_minutes: int = 5  # background feed refresh cadence

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.ical_url)


@dataclass(frozen=True)
class MailConfig:
    """Gmail over IMAP with an app password (2FA required) — read-only, no
    OAuth. 'Any new mail?' / 'read the latest email'."""

    enabled: bool = True
    imap_host: str = "imap.gmail.com"
    address: str = ""        # the Gmail address; empty = feature off
    app_password: str = ""   # Google app password (Security -> App passwords)

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.address) and bool(self.app_password)


@dataclass(frozen=True)
class LaptopConfig:
    """FLINT — the desktop assistant on the user's laptop. Venom hands it
    whole tasks ('open spotify', 'search X in the browser') over FLINT's
    remote WebSocket listener and speaks back FLINT's reply, so the
    wearable can drive the laptop. Off until a host is configured; use the
    laptop's mDNS name (e.g. 'EXODUS.local') so it survives IP changes."""

    enabled: bool = True
    host: str = ""          # laptop mDNS name or address; empty = feature off
    port: int = 8765        # FLINT's remote_port (core/ws_listener.py)
    token: str = ""         # FLINT's remote_token; empty = FLINT runs open
    timeout: float = 45.0   # seconds to wait for FLINT's reply

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.host)


@dataclass(frozen=True)
class CameraConfig:
    """The Raspberry Pi camera (CSI ribbon module). Captured with libcamera and
    described by Gemini, since Jarvis herself is blind to images. On by default;
    if no camera is attached, the capture just fails gracefully and she says so.
    Photos from `take_photo` are pushed to `photo_topic` (falling back to the
    find-my-phone ntfy topic) as an image attachment."""

    enabled: bool = True
    photo_topic: str = ""   # ntfy topic for photo pushes; empty = reuse phone topic

    @property
    def ready(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class ButtonsConfig:
    """Bluetooth camera-shutter remote: two buttons, identified by their evdev
    key code. 0 = not yet mapped — press the button once and read the logged
    `unmapped key code N`, then set it here. The headset button needs no config
    (its play/pause code is already a known wake code)."""

    dnd_code: int = 0    # shutter button 1: toggle do-not-disturb
    wake_code: int = 0   # shutter button 2: wake Venom (a physical wake button)
    # Further codes that also wake her, so a second device doesn't have to
    # displace the first — e.g. the shutter remote on wake_code and the
    # glasses' double tap (165) here, both live at once.
    wake_codes: tuple[int, ...] = ()
    # Whether the headset's play/pause family (WAKE_CODES) still wakes her.
    # Set false when the headset's single tap is wanted for music and a
    # distinct gesture is bound to wake_code instead — e.g. the pTron glasses,
    # whose single tap is play/pause (200/201) and double tap is 165.
    play_pause_wakes: bool = True


@dataclass(frozen=True)
class WhatsAppConfig:
    """Self-hosted WhatsApp send, via the Baileys bridge (venom/whatsapp-service).
    The bridge owns the WhatsApp Web session; Venom talks to its localhost HTTP
    API to send messages ('reply to Tushar: ...'). Incoming messages arrive
    through the ntfy notify path, not here. On unless disabled — the bridge
    reports 'not linked yet' until the QR is scanned."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8788
    token: str = ""        # must match the bridge's WA_TOKEN, if set
    timeout: float = 20.0

    @property
    def ready(self) -> bool:
        return self.enabled


@dataclass(frozen=True)
class LightsConfig:
    """Voice control for Tuya / Smart Life smart bulbs over the LAN (venom/
    lights.py). `registry_path` points at a tinytuya-shaped devices JSON (name,
    id, local key, ip) extracted once with `python -m tinytuya wizard`. Ready
    only when enabled AND that file exists with at least one keyed bulb, so an
    unconfigured box simply never offers the light tools."""

    enabled: bool = True
    registry_path: Path = Path("/var/lib/venom/lights.json")

    @property
    def ready(self) -> bool:
        if not self.enabled:
            return False
        try:
            from venom.lights import LightsController
            return LightsController(self.registry_path).has_devices()
        except Exception:  # never let a bad file block startup
            return False


@dataclass(frozen=True)
class TVConfig:
    """Voice control for a Samsung (Tizen) smart TV over the LAN (venom/tv.py).

    `host` is the TV's address — give it a DHCP reservation, because the token
    the TV issues is tied to the client, not the address, and a moved TV just
    looks offline. `mac` is only needed for power-ON: with the panel off the
    TV's WebSocket is off too, so Wake-on-LAN is the only way back in. Ready
    (and so offering its tools) as soon as a host is set — unlike lights there
    is no registry file to check, and the TV may legitimately be off.
    """

    enabled: bool = True
    host: str = ""
    mac: str = ""
    name: str = "Venom"
    port: int = 8002
    timeout: float = 5.0
    token_path: Path = Path("/var/lib/venom/tv-token.txt")

    @property
    def ready(self) -> bool:
        return bool(self.enabled and self.host.strip())


@dataclass(frozen=True)
class WatchConfig:
    """Delegated background watches — "tell me when X happens" (venom/watch.py).

    Needs the Gemini key, since every check is a grounded search plus a small
    verdict call. `tick_seconds` is only how often the loop *looks* for due
    work; each watch carries its own interval, budget and expiry, and the hard
    limits live on WatchStore because they exist to bound the bill, not to be
    tuned casually.
    """

    enabled: bool = True
    tick_seconds: float = 60.0

    @property
    def ready(self) -> bool:
        return bool(self.enabled)


@dataclass(frozen=True)
class AmbientConfig:
    """Ambient awareness — whether Venom is allowed to speak first.

    On a slow tick she fuses calendar, weather, mail, reminders and her own
    vitals, and opens a conversation when the combination is worth it (see
    venom/ambient.py). Every knob here exists to bound how often that can
    happen: the whole feature is only as good as its restraint.
    """

    enabled: bool = True
    tick_seconds: float = 300.0        # how often to look at the world
    warmup_seconds: float = 180.0      # settle after boot before any nudge

    # ── restraint ────────────────────────────────────────────────────────
    quiet_start_hour: int = 23         # never speak first from 23:00...
    quiet_end_hour: int = 7            # ...until 07:00
    min_gap_minutes: float = 45.0      # minimum spacing between any two
    kind_cooldown_minutes: float = 180.0   # per signal family
    max_per_day: int = 6

    # ── signal thresholds ────────────────────────────────────────────────
    weather_horizon_hours: float = 3.0   # "heading out into this" window
    weather_cache_minutes: float = 30.0
    evening_start_hour: int = 20         # early-start warning window
    evening_end_hour: int = 23
    early_hour: int = 9                  # tomorrow before this = an early start
    mail_pileup: int = 5                 # unread count worth mentioning
    mail_idle_hours: float = 3.0
    temp_warn_c: float = 80.0
    disk_warn_pct: float = 90.0
    idle_hours_before_checkin: float = 5.0
    checkin_horizon_hours: float = 4.0   # how far ahead a check-in may look

    def __post_init__(self) -> None:
        if self.tick_seconds <= 0:
            raise ValueError("ambient.tick_seconds must be positive")
        for name in ("quiet_start_hour", "quiet_end_hour", "evening_start_hour",
                     "evening_end_hour", "early_hour"):
            hour = getattr(self, name)
            if not (0 <= hour <= 23):
                raise ValueError(f"ambient.{name} must be an hour 0-23")


@dataclass(frozen=True)
class PhoneConfig:
    """Find-my-phone over ntfy. Subscribe your phone's ntfy app to `ntfy_topic`
    (give it a loud/alarm sound) and shutter button 2 rings it. Off until a
    topic is set."""

    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    # DEPRECATED / unused: incoming WhatsApp is now delivered locally by the
    # bridge over loopback (no public ntfy round-trip), so nothing subscribes to
    # this topic anymore. Kept only so an old venom.toml still parses.
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
    web_enabled: bool = True   # browser console
    web_port: int = 8787
    # Bind address for the console. Loopback by default: the console is a root
    # shell, so it's reached over an SSH tunnel or Tailscale, never raw on an
    # untrusted network. Set to "0.0.0.0" only on a LAN you fully trust.
    web_bind: str = "127.0.0.1"
    web_token: str = ""        # console access PIN; empty = open (dev only)
    gemini_api_key: str = ""
    brains: tuple[BrainCandidate, ...] = field(default=DEFAULT_CLOUD_CANDIDATES)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    screen: ScreenConfig = field(default_factory=ScreenConfig)
    laptop: LaptopConfig = field(default_factory=LaptopConfig)
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
    mail: MailConfig = field(default_factory=MailConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    buttons: ButtonsConfig = field(default_factory=ButtonsConfig)
    phone: PhoneConfig = field(default_factory=PhoneConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    lights: LightsConfig = field(default_factory=LightsConfig)
    tv: TVConfig = field(default_factory=TVConfig)
    watch: WatchConfig = field(default_factory=WatchConfig)
    ambient: AmbientConfig = field(default_factory=AmbientConfig)

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

    # No early-return on empty data: even with no TOML at all, the env
    # (GEMINI_API_KEY, VENOM_WEB_TOKEN) and the provisioned token file must
    # still apply — a bare VenomConfig() would boot the console wide open
    # and voice disabled despite a perfectly good key in the environment.
    venom = data.get("venom", {})
    internet = data.get("internet", {})
    gemini = data.get("gemini", {})
    voice = data.get("voice", {})
    audio = data.get("audio", {})
    screen = data.get("screen", {})
    laptop = data.get("laptop", {})
    calendar = data.get("calendar", {})
    mail = data.get("mail", {})
    camera = data.get("camera", {})
    buttons = data.get("buttons", {})
    phone = data.get("phone", {})
    whatsapp = data.get("whatsapp", {})
    lights = data.get("lights", {})
    tv = data.get("tv", {})
    watch = data.get("watch", {})
    ambient = data.get("ambient", {})
    raw_brains = data.get("brain", [])

    brains = _parse_brains(raw_brains) if raw_brains else DEFAULT_CLOUD_CANDIDATES

    voice_defaults = VoiceConfig()
    ambient_defaults = AmbientConfig()
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
        web_bind=str(data.get("web", {}).get("bind", "127.0.0.1")).strip()
        or "127.0.0.1",
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
            receiver=bool(audio.get("receiver", True)),
            receiver_focus=bool(audio.get("receiver_focus", True)),
            mic_on_demand=bool(audio.get("mic_on_demand", False)),
        ),
        screen=ScreenConfig(
            enabled=bool(screen.get("enabled", True)),
            host=str(screen.get("host", "")).strip(),
            port=int(screen.get("port", 8766)),
            token=str(screen.get("token", "")).strip(),
            timeout=float(screen.get("timeout", 5.0)),
        ),
        laptop=LaptopConfig(
            enabled=bool(laptop.get("enabled", True)),
            host=str(laptop.get("host", "")).strip(),
            port=int(laptop.get("port", 8765)),
            token=str(laptop.get("token", "")).strip(),
            timeout=float(laptop.get("timeout", 45.0)),
        ),
        calendar=CalendarConfig(
            enabled=bool(calendar.get("enabled", True)),
            ical_url=str(calendar.get("ical_url", "")).strip(),
            lead_minutes=int(calendar.get("lead_minutes", 30)),
            refresh_minutes=int(calendar.get("refresh_minutes", 5)),
        ),
        mail=MailConfig(
            enabled=bool(mail.get("enabled", True)),
            imap_host=str(mail.get("imap_host", "imap.gmail.com")).strip(),
            address=str(mail.get("address", "")).strip(),
            app_password=str(mail.get("app_password", "")).replace(" ", ""),
        ),
        camera=CameraConfig(
            enabled=bool(camera.get("enabled", True)),
            photo_topic=str(camera.get("photo_topic", "")).strip(),
        ),
        buttons=ButtonsConfig(
            dnd_code=int(buttons.get("dnd_code", 0)),
            wake_code=int(buttons.get("wake_code", 0)),
            wake_codes=tuple(int(c) for c in buttons.get("wake_codes", []) if int(c)),
            play_pause_wakes=bool(buttons.get("play_pause_wakes", True)),
        ),
        phone=PhoneConfig(
            ntfy_server=str(phone.get("ntfy_server", "https://ntfy.sh")).strip(),
            ntfy_topic=str(phone.get("ntfy_topic", "")).strip(),
            notify_topic=str(phone.get("notify_topic", "")).strip(),
        ),
        whatsapp=WhatsAppConfig(
            enabled=bool(whatsapp.get("enabled", True)),
            host=str(whatsapp.get("host", "127.0.0.1")).strip(),
            port=int(whatsapp.get("port", 8788)),
            token=str(whatsapp.get("token", "")).strip(),
            timeout=float(whatsapp.get("timeout", 20.0)),
        ),
        lights=LightsConfig(
            enabled=bool(lights.get("enabled", True)),
            registry_path=Path(
                lights.get("registry_path", "/var/lib/venom/lights.json")),
        ),
        tv=TVConfig(
            enabled=bool(tv.get("enabled", True)),
            host=str(tv.get("host", "")).strip(),
            mac=str(tv.get("mac", "")).strip(),
            name=str(tv.get("name", "Venom")).strip() or "Venom",
            port=int(tv.get("port", 8002)),
            timeout=float(tv.get("timeout", 5.0)),
            token_path=Path(tv.get("token_path", "/var/lib/venom/tv-token.txt")),
        ),
        watch=WatchConfig(
            enabled=bool(watch.get("enabled", True)),
            tick_seconds=float(watch.get("tick_seconds", 60.0)),
        ),
        # Every ambient knob falls back to the dataclass default, so an empty
        # [ambient] section (or none at all) is the tuned configuration.
        ambient=AmbientConfig(**{
            name: type(getattr(ambient_defaults, name))(ambient[name])
            for name in vars(ambient_defaults)
            if name in ambient
        }),
    )
