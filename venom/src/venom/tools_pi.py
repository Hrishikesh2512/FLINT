"""Venom's standalone tool belt — everything the Pi can do with just
Wi-Fi, a headset, and cloud APIs. Registered on flint-core's ToolRegistry,
so declarations/dispatch/docs come from one definition, same as Flint.

Timers are a plain in-memory board the voice loop polls: when one fires,
Venom chimes through the headset and announces it on the next exchange.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import requests

from flint_core.llm.providers import GeminiProvider
from flint_core.memory import MemoryStore
from flint_core.tools import ToolRegistry
from venom.config import VenomConfig

log = logging.getLogger("venom.tools")

# The root control channel the console also uses: writing a keyword here makes
# the privileged venom-control unit run it as root (see provisioning/control.sh).
CONTROL_REQUEST = Path("/run/venom/control.request")


# ── timers ────────────────────────────────────────────────────────────────────
@dataclass
class Timer:
    label: str
    due_at: float


@dataclass
class TimerBoard:
    clock: Callable[[], float] = time.monotonic
    timers: list[Timer] = field(default_factory=list)

    def add(self, minutes: float, label: str) -> Timer:
        timer = Timer(label=label or "timer", due_at=self.clock() + minutes * 60)
        self.timers.append(timer)
        return timer

    def pop_due(self) -> list[Timer]:
        now = self.clock()
        due = [t for t in self.timers if t.due_at <= now]
        self.timers = [t for t in self.timers if t.due_at > now]
        return due

    def pending(self) -> list[tuple[str, float]]:
        now = self.clock()
        return [(t.label, max(0.0, (t.due_at - now) / 60)) for t in self.timers]


# ── weather (open-meteo: keyless, generous limits) ──────────────────────────
_WEATHER_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers",
    81: "rain showers", 82: "violent rain showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}


def fetch_weather(city: str, get=requests.get) -> str:
    geo = get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": city, "count": 1}, timeout=10,
    ).json()
    results = geo.get("results") or []
    if not results:
        return f"I couldn't find a place called {city}."
    place = results[0]
    forecast = get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m",
        },
        timeout=10,
    ).json()
    current = forecast.get("current") or {}
    if not current:
        return f"Weather service returned no data for {city}."
    sky = _WEATHER_CODES.get(int(current.get("weather_code", -1)), "unknown conditions")
    return (
        f"In {place['name']}: {sky}, {current.get('temperature_2m')}°C "
        f"(feels like {current.get('apparent_temperature')}°C), "
        f"humidity {current.get('relative_humidity_2m')}%, "
        f"wind {current.get('wind_speed_10m')} km/h."
    )


# ── personalization ───────────────────────────────────────────────────────────
def home_city(memory: MemoryStore) -> str:
    """The user's city, pulled from whatever they've told Venom to remember."""
    data = memory.load()
    for category in ("identity", "preferences", "notes"):
        for key, entry in (data.get(category) or {}).items():
            if any(word in key.lower()
                   for word in ("home_city", "city", "location", "town", "live")):
                value = entry.get("value") if isinstance(entry, dict) else entry
                if value:
                    return str(value)
    return ""


def build_briefing(memory: MemoryStore, timers: TimerBoard,
                   now: str | None = None, location=None, reminders=None) -> str:
    """Facts for a spoken morning update — the model turns these into speech."""
    now = now or time.strftime("%A, %B %d, %Y, and it's %I:%M %p")
    parts = [f"Today is {now}."]
    city = ""
    if location is not None:
        city = (location.get() or {}).get("city") or ""
    city = city or home_city(memory)
    if city:
        try:
            parts.append("Weather — " + fetch_weather(city))
        except Exception:  # network hiccup shouldn't sink the whole briefing
            parts.append(f"(Couldn't fetch the weather for {city} right now.)")
    else:
        parts.append("(You don't know their city yet — ask where they are, "
                     "then save it with save_memory as identity/home_city.)")
    if reminders is not None:
        upcoming = reminders.pending()
        if upcoming:
            parts.append("Today's reminders: " + "; ".join(
                f"{r['text']} at "
                f"{time.strftime('%I:%M %p', time.localtime(r['due']))}"
                for r in upcoming[:5]))
    pending = timers.pending()
    if pending:
        parts.append("Running timers: " + "; ".join(
            f"{label} ({remaining:.0f} min left)" for label, remaining in pending))
    parts.append("This is the first hello of his morning. Open with it in your "
                 "usual Hinglish — warm, brief, human. Not a list, not a "
                 "weather report; just how a friend would catch him up.")
    return "\n".join(parts)


# ── volume ────────────────────────────────────────────────────────────────────
def set_alsa_volume(percent: int, card_index: int | None = None) -> str:
    percent = max(0, min(100, int(percent)))
    if platform.system() != "Linux":
        return f"Volume set to {percent}% (simulated — not on Linux)."
    cmd = ["amixer"]
    if card_index is not None:
        cmd += ["-c", str(card_index)]
    cmd += ["sset", "PCM", f"{percent}%"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        # USB headsets often expose 'Speaker' or 'Master' instead of 'PCM'
        for control in ("Speaker", "Master", "Headphone"):
            cmd[-2] = control
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                break
    if result.returncode != 0:
        return f"Could not set volume: {result.stderr.strip()[:100]}"
    return f"Volume set to {percent}%."


def set_system_volume(percent: int) -> str:
    """Absolute volume on PipeWire's default sink — the node every stream
    (voice, music, chimes) actually plays through. amixer talks to a raw ALSA
    card, which on this box is behind PipeWire and often the wrong one; wpctl
    moves the volume the user actually hears. ALSA stays as the fallback for
    a PipeWire-less dev box."""
    percent = max(0, min(100, int(percent)))
    if platform.system() != "Linux":
        return f"Volume set to {percent}% (simulated — not on Linux)."
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
             f"{percent / 100:.2f}"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return f"Volume set to {percent}%."
    except (OSError, subprocess.SubprocessError):
        pass
    return set_alsa_volume(percent)


def change_system_volume(delta: int) -> str:
    """Relative volume ('thoda tez karo') on the default sink, ±percent."""
    delta = max(-100, min(100, int(delta)))
    if delta == 0:
        return "Volume unchanged."
    if platform.system() != "Linux":
        return f"Volume nudged by {delta:+d}% (simulated — not on Linux)."
    sign = "+" if delta > 0 else "-"
    try:
        result = subprocess.run(
            ["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@",
             f"{abs(delta)}%{sign}"],
            capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return ("Volume up a bit." if delta > 0 else "Volume down a bit.")
    except (OSError, subprocess.SubprocessError):
        pass
    return "Could not change the volume."


# ── device health ─────────────────────────────────────────────────────────────
def _read_file(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def device_metrics() -> dict[str, float]:
    """Raw device health numbers from /proc and /sys.

    Keys — temp_c, mem_pct, disk_pct, uptime_s — are present only when they
    could actually be read, so this returns {} on a non-Linux dev box rather
    than lying with zeros. The spoken summary below and the ambient loop's
    self-health warnings both read from here, so there is one parser."""
    out: dict[str, float] = {}

    temp = _read_file("/sys/class/thermal/thermal_zone0/temp").strip()
    if temp.isdigit():
        out["temp_c"] = int(temp) / 1000

    mem = {line.split(":")[0]: int(line.split()[1])
           for line in _read_file("/proc/meminfo").splitlines()[:5]
           if ":" in line}
    if "MemTotal" in mem and "MemAvailable" in mem and mem["MemTotal"]:
        out["mem_pct"] = 100 * (1 - mem["MemAvailable"] / mem["MemTotal"])

    try:
        st = os.statvfs("/")  # Linux-only; absent on dev boxes
        out["disk_pct"] = 100 * (1 - st.f_bavail / st.f_blocks)
    except (OSError, AttributeError, ZeroDivisionError):
        pass

    up = _read_file("/proc/uptime").split()
    if up:
        out["uptime_s"] = float(up[0])
    return out


def device_vitals() -> str:
    """A spoken-style health summary of the Pi — temperature, memory, disk,
    uptime. Anything unreadable is simply omitted, so this works (as far as
    it can) on any box."""
    metrics = device_metrics()
    parts: list[str] = []

    if "temp_c" in metrics:
        heat = " — running hot!" if metrics["temp_c"] >= 75 else ""
        parts.append(f"temperature {metrics['temp_c']:.0f}°C{heat}")
    if "mem_pct" in metrics:
        parts.append(f"memory {metrics['mem_pct']:.0f}% used")
    if "disk_pct" in metrics:
        parts.append(f"disk {metrics['disk_pct']:.0f}% full")
    if "uptime_s" in metrics:
        secs = int(metrics["uptime_s"])
        parts.append(f"up {secs // 3600}h {secs % 3600 // 60}m")

    if not parts:
        return "I couldn't read the device's health right now."
    return "Device health: " + ", ".join(parts) + "."


# ── reminder time parsing ──────────────────────────────────────────────────
def parse_reminder_time(minutes_from_now: float | None = None,
                        at_time: str | None = None,
                        now: float | None = None) -> tuple[float, str]:
    """Resolve a reminder's absolute epoch + a human phrase. Accepts either a
    relative `minutes_from_now`, or an absolute `at_time` string the model
    computed from the current time ("YYYY-MM-DD HH:MM", 24h local). Raises
    ValueError if neither is usable."""
    now = time.time() if now is None else now
    if minutes_from_now is not None:
        try:
            mins = float(minutes_from_now)
        except (TypeError, ValueError):
            raise ValueError("minutes_from_now must be a number") from None
        if mins <= 0:
            raise ValueError("minutes_from_now must be positive")
        due = now + mins * 60
        return due, f"in {mins:g} minute(s)"
    if at_time:
        stamp = str(at_time).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%H:%M"):
            try:
                parsed = time.strptime(stamp, fmt)
            except ValueError:
                continue
            if fmt == "%H:%M":  # time only → today, or tomorrow if already past
                lt = time.localtime(now)
                cand = time.struct_time((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                         parsed.tm_hour, parsed.tm_min, 0,
                                         0, 0, -1))
                due = time.mktime(cand)
                if due <= now:
                    due += 86400
            else:
                due = time.mktime(parsed)
            if due <= now:
                raise ValueError("that time is in the past")
            return due, time.strftime("%A %I:%M %p", time.localtime(due))
        raise ValueError("couldn't understand the time")
    raise ValueError("need minutes_from_now or at_time")


# ── registry ─────────────────────────────────────────────────────────────────
def build_pi_registry(config: VenomConfig, memory: MemoryStore,
                      timers: TimerBoard, music=None,
                      reminders=None, notes=None, lists=None,
                      location=None, chess=None, notifications=None,
                      receiver=None, calendar=None, mailbox=None,
                      whatsapp=None, connections=None, lights=None,
                      tv=None, watches=None, jobs=None, audit=None,
                      projects=None, outcomes=None, archive=None,
                      sos=None) -> ToolRegistry:
    reg = ToolRegistry(platform="linux")

    if calendar is not None:
        @reg.tool(
            description=(
                "Reads the user's Google Calendar agenda. Use for 'what's on "
                "today?', 'kal kya hai?', 'am I free tomorrow?', 'what's my "
                "schedule?'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "'today' (default) or 'tomorrow'"},
                },
            },
        )
        def calendar_agenda(day: str = "today") -> str:
            return calendar.feed.agenda(day)

        @reg.tool(
            description=(
                "Tells the user their next upcoming calendar event and how "
                "long until it. Use for 'what's next?', 'when's my next "
                "class/meeting?'."
            ),
        )
        def next_event() -> str:
            return calendar.feed.next_event()

    if mailbox is not None:
        @reg.tool(
            description=(
                "Checks the user's Gmail inbox and summarises unread mail "
                "(count, senders, subjects). Use for 'any new mail?', 'koi "
                "email aaya?', 'check my inbox'. Read-only — nothing is "
                "marked as read."
            ),
        )
        def check_inbox() -> str:
            return mailbox.unread_summary()

        @reg.tool(
            description=(
                "Reads an email aloud: the latest unread one, or the latest "
                "from a specific sender if given. Use for 'read the email', "
                "'what did the placement cell send?'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from_sender": {"type": "string",
                                    "description": "Optional sender name or "
                                                   "address to filter by"},
                },
            },
        )
        def read_latest_email(from_sender: str = "") -> str:
            return mailbox.read_latest(from_sender)

    if music is not None:
        @reg.tool(
            description=(
                "Plays a song, artist, album, or any music/audio from YouTube "
                "through the user's headset. Use whenever the user asks to "
                "play something. Note: playback replaces any current song."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to play, e.g. 'Kesariya', 'lofi beats'"},
                },
                "required": ["query"],
            },
        )
        def play_music(query: str) -> str:
            return music.play(query)

        @reg.tool(
            description="Stops the currently playing music.",
        )
        def stop_music() -> str:
            return music.stop()

        @reg.tool(description="Pauses the currently playing music (resumable).")
        def pause_music() -> str:
            return music.set_paused(True)

        @reg.tool(description="Resumes paused music.")
        def resume_music() -> str:
            return music.set_paused(False)

        @reg.tool(
            description=(
                "Tells what music is currently playing, if any — including "
                "whether it's one of the user's favourites or saved offline. "
                "Use for 'what's playing?', 'kaunsa gaana hai?', 'which song is "
                "this?'."
            ),
        )
        def now_playing() -> str:
            return music.status()

        @reg.tool(
            description=(
                "Skips the current song and plays the next similar one. Use "
                "when the user says 'next', 'skip', 'agla gaana', 'change the "
                "song', or clearly dislikes what's playing."
            ),
        )
        def next_song() -> str:
            return music.skip()

        @reg.tool(
            description=(
                "Turns autoplay of similar songs on or off. When on (the "
                "default), a new similar song plays automatically after each "
                "one finishes. Use when the user asks to keep the music going, "
                "play similar songs, or to stop after the current song."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to keep playing similar songs, false to stop after this one"},
                },
                "required": ["enable"],
            },
        )
        def autoplay_similar(enable: bool) -> str:
            return music.set_autoplay(enable)

        @reg.tool(
            description=(
                "Restarts the current song from the beginning. Use when the "
                "user says 'restart', 'play it again', 'from the start', "
                "'shuru se', 'wapas se chalao'."
            ),
        )
        def restart_song() -> str:
            return music.restart()

        @reg.tool(
            description=(
                "Goes back to the previous song that played this session. Use "
                "for 'previous', 'go back', 'pichla gaana', 'last song wapas', "
                "'the one before this'."
            ),
        )
        def previous_song() -> str:
            return music.play_previous()

        @reg.tool(
            description=(
                "Saves a song to the user's favourites. With no name, favourites "
                "whatever is playing right now; with a name, finds and saves that "
                "song. Use for 'add to favourites', 'favourite this', 'ise "
                "favourite karo', 'save this song', 'I love this one'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Optional song to favourite; omit to "
                                            "favourite the current song"},
                },
            },
        )
        def add_favourite(name: str = "") -> str:
            return music.add_favourite(name)

        @reg.tool(
            description=(
                "Removes a song from the user's favourites by name. Use for "
                "'remove from favourites', 'unfavourite', 'ye favourite se hata "
                "do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The favourite to remove (by title)"},
                },
                "required": ["name"],
            },
        )
        def remove_favourite(name: str) -> str:
            return music.remove_favourite(name)

        @reg.tool(
            description=(
                "Lists the user's favourite songs and how many are saved "
                "offline. Use for 'what are my favourites?', 'meri favourite "
                "list', 'which songs do I have saved?'."
            ),
        )
        def list_favourites() -> str:
            return music.list_favourites()

        @reg.tool(
            description=(
                "Plays through all the user's favourite songs (offline copies "
                "first, so it works with no signal), chaining to the next when "
                "each finishes. Use for 'play my favourites', 'meri favourites "
                "chalao', 'play my saved songs', 'play the songs I like'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "shuffle": {"type": "boolean",
                                "description": "Shuffle the order (default true)"},
                },
            },
        )
        def play_favourites(shuffle: bool = True) -> str:
            return music.play_favourites(shuffle)

        @reg.tool(
            description=(
                "Plays ONE specific saved favourite by name. Prefer this over "
                "play_music when the user names a song they've favourited — it "
                "needs no search, so it plays even offline with no signal (from "
                "the downloaded copy). Use for 'play my saved Kesariya', 'play "
                "that favourite', 'offline mein Kesariya chalao'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The favourite song to play, by title"},
                },
                "required": ["name"],
            },
        )
        def play_favourite(name: str) -> str:
            return music.play_favourite(name)

        @reg.tool(
            description=(
                "Downloads all of the user's favourite songs for offline play, "
                "so they keep working through dead zones with no signal — do "
                "this before a long trip. Runs in the background. Use for "
                "'download my favourites', 'save my songs offline', 'make my "
                "favourites available offline', 'offline ke liye download karo'."
            ),
        )
        def download_favourites() -> str:
            return music.download_favourites()

    if chess is not None:
        @reg.tool(
            description=(
                "Starts a new chess game the user plays by voice against you. "
                "Use when they ask to play chess. The board is tracked for you; "
                "afterwards call play_chess_move for each of their moves."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "color": {"type": "string",
                              "description": "The colour the USER plays: 'white' or 'black'. Default white."},
                    "difficulty": {"type": "integer",
                                   "description": "Search depth 1 (easy) to 3 (hard). Default 2."},
                },
            },
        )
        def start_chess_game(color: str = "white", difficulty: int | None = None) -> str:
            return chess.new_game(color, difficulty)

        @reg.tool(
            description=(
                "Applies the user's chess move and returns your reply. Pass the "
                "move in standard algebraic notation, converting their speech: "
                "'knight to f3'->'Nf3', 'e4', 'bishop takes e5'->'Bxe5', "
                "'castle kingside'->'O-O', 'e8 promote to queen'->'e8=Q'. UCI "
                "like 'e2e4' also works. Illegal moves are rejected with a hint."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "move": {"type": "string",
                             "description": "The user's move, e.g. 'Nf3', 'e4', 'O-O', 'exd5'"},
                },
                "required": ["move"],
            },
        )
        def play_chess_move(move: str) -> str:
            return chess.human_move(move)

        @reg.tool(description="Resigns/ends the current chess game.")
        def resign_chess() -> str:
            return chess.resign()

    if notifications is not None and notifications.enabled:
        @reg.tool(
            description=(
                "Reads out the user's new phone notifications (WhatsApp). Use "
                "when they ask things like 'any messages?', 'kya notification "
                "aaya?', 'read my WhatsApp', or 'what did I miss?'. A chime "
                "already played when each one arrived; this reads them aloud and "
                "marks them seen."
            ),
        )
        def read_notifications() -> str:
            return notifications.read_unread()

    if whatsapp is not None and config.whatsapp.ready:
        @reg.tool(
            description=(
                "Sends a WhatsApp message on the user's behalf. Use when they "
                "say 'WhatsApp X to <name>', 'message <name>', 'text <name>', "
                "'reply to <name>', or after reading a message 'reply: ...'. "
                "Pass the message body as `text`, converting their spoken words "
                "into a natural written message. Set `to` to the contact's name "
                "(or a phone number); LEAVE `to` EMPTY to reply to whoever last "
                "messaged. If several contacts match a name, this asks which one "
                "— relay that and call again with a clearer name. Always confirm "
                "the message and recipient with the user before sending anything "
                "they didn't dictate word-for-word."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "The message to send"},
                    "to": {"type": "string",
                           "description": "Contact name or phone number; omit to "
                                          "reply to the most recent chat"},
                },
                "required": ["text"],
            },
        )
        def send_whatsapp(text: str, to: str = "") -> str:
            # Resolve a name against the user's own Connections first, so
            # 'message Rahul' works from saved numbers even before WhatsApp
            # syncs its contact list. A number/blank is passed through as-is.
            target = to
            if to and connections is not None:
                bare = to.strip().lstrip("+").replace(" ", "").replace("-", "")
                if not bare.isdigit():
                    num = connections.phone_for(to)
                    if num:
                        target = num
            return whatsapp.send(text, target)

        @reg.tool(
            description=(
                "Turns AUTO-REPLY mode for WhatsApp on or off. When ON, you "
                "answer NEW incoming WhatsApp messages yourself, on the user's "
                "behalf, without them dictating each reply — until they turn it "
                "off. Existing unread chats are left alone; only messages that "
                "arrive after it's switched on are answered. Use when the user "
                "says 'auto message mode on/off', 'auto reply on', 'reply to "
                "messages automatically', 'answer my WhatsApp for me', or 'stop "
                "auto replying'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to start auto-replying, "
                                              "false to stop"},
                },
                "required": ["enable"],
            },
        )
        def auto_reply_mode(enable: bool) -> str:
            return whatsapp.set_auto_reply(enable)

        @reg.tool(
            description=(
                "Turns on/off answering when someone writes '@jarvis' in the "
                "user's WhatsApp chats — groups included. This is ON by "
                "default and is NOT the same as auto_reply_mode: this one "
                "answers only when summoned by name, and never speaks for the "
                "user otherwise. Use for 'stop replying in my groups', "
                "'@jarvis wala reply band kar do', 'start answering when "
                "people tag you'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "enable": {"type": "boolean",
                               "description": "true to answer mentions, false to stop"},
                },
                "required": ["enable"],
            },
        )
        def mention_reply_mode(enable: bool) -> str:
            return whatsapp.set_mention_reply(enable)

    if sos is not None:
        @reg.tool(
            description=(
                "EMERGENCY SOS. WhatsApps every saved emergency contact that "
                "the user needs help, with their approximate location and the "
                "time, then stays in emergency mode and resends the location "
                "every few minutes until it's called off. Call this the moment "
                "the user asks for emergency help — 'SOS', 'emergency', 'help "
                "me', 'bachao', 'I'm in danger', 'call for help', 'send my "
                "location to my family' — in ANY language, and act first: alert "
                "now, reassure while it sends. Do NOT call it for a drill or a "
                "hypothetical ('what would happen if…', 'test the SOS') — use "
                "emergency_contacts with action 'test' for that. If you are "
                "genuinely unsure they mean it, ask one short question first. "
                "Put anything they said about what's happening in `note` — it "
                "goes to the contacts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {"type": "string",
                             "description": "What's happening, in the user's "
                                            "own words — sent to the contacts"},
                },
            },
        )
        def emergency_sos(note: str = "") -> str:
            return sos.start(note)

        @reg.tool(
            description=(
                "Ends emergency mode and sends an 'all clear — I'm safe' "
                "WhatsApp to everyone who was alerted. Use for 'I'm safe', "
                "'cancel the SOS', 'emergency over', 'call it off', 'main "
                "theek hoon'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {"type": "string",
                                "description": "Optional extra line for the "
                                               "all-clear message"},
                },
            },
        )
        def end_emergency(message: str = "") -> str:
            return sos.stop(message)

        @reg.tool(
            description=(
                "Tells whether emergency mode is on and who the SOS would "
                "alert. Use for 'is the SOS on?', 'who do you alert in an "
                "emergency?'."
            ),
        )
        def sos_status() -> str:
            return sos.status()

        @reg.tool(
            description=(
                "Manages WHO gets the emergency SOS — a contact list separate "
                "from normal messaging, so the SOS can go to different people "
                "than the user usually WhatsApps. Actions: 'add' (name, plus "
                "`to` = their phone number with country code, or the exact "
                "WhatsApp contact name; `label` = who they are, e.g. father), "
                "'remove', 'list', 'enable'/'disable' (keep someone saved but "
                "skip them), 'set_message' (custom SOS wording for that one "
                "contact — leave `name` empty to change the wording everyone "
                "gets), and 'test' (sends a clearly-marked drill to everyone so "
                "the user can confirm it works). Use for 'add my father to my "
                "emergency contacts', 'SOS mein Priya ko daal do', 'who's on my "
                "SOS list', 'test the emergency alert'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action":  {"type": "string",
                                "description": "add | remove | list | enable | "
                                               "disable | set_message | test"},
                    "name":    {"type": "string",
                                "description": "The contact's name"},
                    "to":      {"type": "string",
                                "description": "Phone number with country code, "
                                               "or exact WhatsApp contact name"},
                    "label":   {"type": "string",
                                "description": "Who they are: father, sister, "
                                               "flatmate, doctor…"},
                    "message": {"type": "string",
                                "description": "Custom SOS wording (set_message)"},
                },
                "required": ["action"],
            },
        )
        def emergency_contacts(action: str, name: str = "", to: str = "",
                               label: str = "", message: str = "") -> str:
            from venom.sos import manage_contacts

            return manage_contacts(sos, action, name=name, to=to,
                                   label=label, message=message)

    if connections is not None:
        @reg.tool(
            description=(
                "Saves or updates what you know about a PERSON in the user's "
                "Connections — their phone number, nickname, Instagram, an "
                "interest, or any note. Call SILENTLY whenever the user shares "
                "someone's details, even one detail at a time: 'Rahul ka number "
                "98765 43210 hai', 'my friend Priya loves painting', 'Amit's "
                "insta is amit.k', 'Rahul ko bhai bulao'. Everything merges into "
                "that one person, so partial info is fine. Pass the person's "
                "name plus whichever fields were mentioned."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name":      {"type": "string",
                                  "description": "The person's name"},
                    "phone":     {"type": "string",
                                  "description": "Phone number, any format"},
                    "nickname":  {"type": "string",
                                  "description": "A nickname/alias for them"},
                    "instagram": {"type": "string",
                                  "description": "Instagram handle"},
                    "interest":  {"type": "string",
                                  "description": "Something they're into"},
                    "note":      {"type": "string",
                                  "description": "Any other fact to remember"},
                },
                "required": ["name"],
            },
        )
        def save_connection(name: str, phone: str = "", nickname: str = "",
                            instagram: str = "", interest: str = "",
                            note: str = "") -> str:
            rec = connections.save(name, phone=phone, nickname=nickname,
                                   instagram=instagram, interest=interest,
                                   note=note)
            if not rec:
                return "I need a name to save that against."
            return f"Saved — {rec['name']} is in your connections."

        @reg.tool(
            description=(
                "Recalls everything saved about a person in Connections — their "
                "number, nickname, Instagram, interests and notes. Use for 'what "
                "do you know about Rahul', 'Priya ka number kya hai', 'tell me "
                "about Amit', 'Rahul ka insta?'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Who to look up (name or nickname)"},
                },
                "required": ["name"],
            },
        )
        def get_connection(name: str) -> str:
            return connections.describe(name)

        @reg.tool(
            description=("Lists the people saved in the user's Connections. Use "
                         "for 'who all do you have saved', 'list my contacts'."),
        )
        def list_connections() -> str:
            names = connections.all_names()
            if not names:
                return "You haven't saved anyone in connections yet."
            return "You have saved: " + ", ".join(names) + "."

        @reg.tool(
            description=("Removes a person from the user's Connections. Use for "
                         "'forget Rahul', 'delete Amit from contacts'."),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Who to remove"},
                },
                "required": ["name"],
            },
        )
        def forget_connection(name: str) -> str:
            return (f"Removed {name} from your connections."
                    if connections.forget(name)
                    else f"I don't have anyone saved as {name}.")

    if receiver is not None:
        @reg.tool(
            description=(
                "Makes this wearable discoverable as a Bluetooth headset named "
                "'venom' for about two minutes, so the user's laptop or phone "
                "can connect, play its audio through the earpiece, and use the "
                "earpiece mic for its calls. Use when they say 'pair my "
                "laptop', 'connect my laptop audio', 'play my phone through "
                "you', or 'bluetooth pairing on'. Tell the user exactly what "
                "this returns — it has the connection steps."
            ),
        )
        def pair_bluetooth_device() -> str:
            return receiver.open_pairing()

        @reg.tool(
            description=(
                "Reports whether a laptop or phone is currently streaming "
                "audio through the earpiece, and whether pairing is open. Use "
                "for 'is my laptop connected?', 'kya connected hai?'."
            ),
        )
        def bluetooth_audio_status() -> str:
            return receiver.status()

        @reg.tool(
            description=(
                "Disconnects the laptop/phone that is streaming audio through "
                "the earpiece. Use when the user says 'disconnect my laptop', "
                "'stop the laptop audio', 'bluetooth disconnect karo'. Does "
                "NOT touch the user's headset or stop your own music."
            ),
        )
        def disconnect_bluetooth_audio() -> str:
            return receiver.disconnect_all()

    if lights is not None:
        @reg.tool(
            description=(
                "Turns the user's smart lights ON or OFF. `where` names a room "
                "or a single light ('bedroom', 'kitchen lamp'); leave it EMPTY "
                "for every light. Use for 'lights on/off', 'turn off the "
                "bedroom', 'lights band karo', 'sab lights on kar do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to switch on, false to switch off"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["on"],
            },
        )
        def set_lights(on: bool, where: str = "") -> str:
            return lights.power(on, where)

        @reg.tool(
            description=(
                "Sets smart-light BRIGHTNESS to an absolute level (also turns "
                "the light on). Use for 'dim the lights', 'bedroom 20%', 'full "
                "brightness', 'thoda tez/kam kar do'. For relative nudges pick a "
                "sensible new percent. `where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "percent": {"type": "integer",
                                "description": "Brightness 1-100"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["percent"],
            },
        )
        def set_light_brightness(percent: int, where: str = "") -> str:
            return lights.brightness(percent, where)

        @reg.tool(
            description=(
                "Sets smart-light COLOUR or white tone (also turns it on). Pass "
                "a colour name — red, orange, yellow, green, teal, cyan, blue, "
                "indigo, violet, purple, magenta, pink — or a white: 'warm', "
                "'neutral', 'cool'/'daylight'. Map the user's spoken colour onto "
                "the closest of these. Use for 'make it blue', 'warm white kar "
                "do', 'lights red kar do'. `where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "color": {"type": "string",
                              "description": "A colour name, or warm/neutral/cool white"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["color"],
            },
        )
        def set_light_color(color: str, where: str = "") -> str:
            return lights.colour(color, where)

        @reg.tool(
            description=(
                "Applies a preset lighting SCENE (colour + brightness together). "
                "Known scenes: relax, reading, movie, focus, night, party, "
                "romantic, sunset. Use when the user names a mood/scene like "
                "'movie mode', 'reading light', 'party lights', 'night mode'. "
                "`where` omitted = all lights."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scene": {"type": "string",
                              "description": "relax|reading|movie|focus|night|party|romantic|sunset"},
                    "where": {"type": "string",
                              "description": "Room or light name; omit for all lights"},
                },
                "required": ["scene"],
            },
        )
        def set_light_scene(scene: str, where: str = "") -> str:
            return lights.scene(scene, where)

        @reg.tool(
            description=("Lists the smart lights the user has set up, by name and "
                         "room. Use for 'what lights do you control', 'list my "
                         "lights', 'kaun kaun si lights hain'."),
        )
        def list_lights() -> str:
            return lights.list_lights()

    if tv is not None:
        @reg.tool(
            description=(
                "Turns the user's TV ON or OFF. Use for 'TV on kar do', 'turn "
                "off the TV', 'TV band karo'. Switching ON uses Wake-on-LAN and "
                "takes a few seconds, so don't immediately follow it with "
                "another TV command — give it a moment."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to switch on, false to switch off"},
                },
                "required": ["on"],
            },
        )
        def set_tv_power(on: bool) -> str:
            return tv.power(on)

        @reg.tool(
            description=(
                "Makes the TV LOUDER or QUIETER by a few steps. This is the "
                "normal way to change TV volume — use it for 'volume up', "
                "'thoda tez karo', 'turn it down', 'awaaz kam karo'. Only use "
                "set_tv_volume when the user names an exact number."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "direction": {"type": "string",
                                  "description": "'up' or 'down'"},
                    "steps": {"type": "integer",
                              "description": "How many notches, 1-30 (default 3)"},
                },
                "required": ["direction"],
            },
        )
        def nudge_tv_volume(direction: str, steps: int = 3) -> str:
            return tv.nudge_volume(direction, steps)

        @reg.tool(
            description=(
                "Sets the TV to an EXACT volume level, 0-100. Only use when the "
                "user names a number ('TV volume 20'). Not all TVs support "
                "this; if it fails, fall back to nudge_tv_volume."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "percent": {"type": "integer", "description": "Volume 0-100"},
                },
                "required": ["percent"],
            },
        )
        def set_tv_volume(percent: int) -> str:
            return tv.set_volume(percent)

        @reg.tool(
            description=(
                "Mutes or unmutes the TV. Omit `on` to simply toggle mute, "
                "which is the most reliable. Use for 'mute the TV', 'chup "
                "karao', 'unmute'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "on": {"type": "boolean",
                           "description": "true to mute, false to unmute; omit to toggle"},
                },
            },
        )
        def mute_tv(on: bool | None = None) -> str:
            return tv.mute(on)

        @reg.tool(
            description=(
                "Presses a button on the TV remote. Use for playback and "
                "navigation: play, pause, stop, forward, rewind, up, down, "
                "left, right, ok, back, home, menu, exit, source, channel up, "
                "channel down. Handles 'pause karo', 'aage badhao', 'go back', "
                "'thoda aage'. For skipping ahead, send forward a few times."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "key": {"type": "string",
                            "description": "Button name, e.g. pause|forward|ok|back|home"},
                    "times": {"type": "integer",
                              "description": "How many presses, 1-20 (default 1)"},
                },
                "required": ["key"],
            },
        )
        def press_tv_key(key: str, times: int = 1) -> str:
            return tv.press(key, times)

        @reg.tool(
            description=(
                "Opens an app on the TV: Netflix, YouTube, Prime Video, "
                "Hotstar, Disney+, Spotify, Apple TV, JioCinema, SonyLIV, Zee5 "
                "or the browser. Use for 'Netflix kholo', 'put on YouTube', "
                "'open Prime on the TV'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app": {"type": "string",
                            "description": "App name, e.g. netflix|youtube|prime video"},
                },
                "required": ["app"],
            },
        )
        def open_tv_app(app: str) -> str:
            return tv.launch_app(app)

        @reg.tool(
            description=(
                "Tries to play a named title on a TV streaming app — 'play Dune "
                "on Netflix', 'YouTube pe lo-fi lagao'. Jumping straight to a "
                "title often isn't possible, in which case this opens the app "
                "and says so; relay that honestly instead of claiming it's "
                "playing. Default app is Netflix if the user didn't name one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string",
                              "description": "What to play, as the user said it"},
                    "app": {"type": "string",
                            "description": "App to play it on (default netflix)"},
                },
                "required": ["title"],
            },
        )
        def play_on_tv(title: str, app: str = "netflix") -> str:
            return tv.play_title(title, app)

        @reg.tool(
            description=("Lists the apps installed on the TV. Use for 'what "
                         "apps are on the TV', 'TV pe kya kya hai'."),
        )
        def list_tv_apps() -> str:
            return tv.list_apps()

        @reg.tool(
            description=("Checks whether the TV is on, in standby, or "
                         "unreachable. Use for 'is the TV on?', 'TV chalu hai?'."),
        )
        def tv_status() -> str:
            return tv.status()

    if watches is not None:
        @reg.tool(
            description=(
                "Takes on a background job: keep checking something on the web "
                "and INTERRUPT the user later, on your own, the moment it "
                "happens. Use whenever he delegates something with a 'tell me "
                "when' / 'let me know if' / 'keep an eye on' shape — 'tell me "
                "when the match turns', 'batao jab result aa jaye', 'let me "
                "know if the price drops'.\n"
                "`what` must be self-contained enough to search for on its own "
                "hours from now — resolve 'it' and 'that' into real names "
                "before calling. `condition` is what makes it worth "
                "interrupting him for; leave it EMPTY to be told on any real "
                "change. Set `urgent` only if he wants waking at night.\n"
                "Each check costs a web search, so pick an honest "
                "`check_every_minutes`: minutes for a live match, an hour for "
                "a result that lands sometime today. Tell him you'll come back "
                "to him — do NOT keep checking within this conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string",
                             "description": "What to keep checking, self-contained"},
                    "condition": {"type": "string",
                                  "description": "What makes it worth telling him; "
                                                 "omit to fire on any real change"},
                    "check_every_minutes": {
                        "type": "integer",
                        "description": "Minutes between checks (min 2, default 10)"},
                    "for_hours": {"type": "number",
                                  "description": "Give up after this long (default 24)"},
                    "urgent": {"type": "boolean",
                               "description": "true to interrupt even at night"},
                },
                "required": ["what"],
            },
        )
        def watch_for(what: str, condition: str = "",
                      check_every_minutes: int = 10, for_hours: float = 24.0,
                      urgent: bool = False) -> str:
            try:
                entry = watches.add(what, condition,
                                    interval=max(2, int(check_every_minutes or 10)) * 60,
                                    ttl_hours=for_hours, urgent=urgent)
            except ValueError as exc:
                return str(exc)
            every = int(entry["interval"] // 60)
            return (f"Watching that now — checking every {every} minutes. "
                    "I'll come back to you when it happens.")

        @reg.tool(
            description=("Says what background watches are running. Use for "
                         "'what are you watching?', 'kya track kar rahi ho?'."),
        )
        def list_watches() -> str:
            return watches.summary()

        @reg.tool(
            description=(
                "Stops a background watch. Pass a few words of the thing being "
                "watched; omit `what` to stop every watch. Use for 'stop "
                "watching the match', 'sab band kar do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string",
                             "description": "Words identifying the watch; omit for all"},
                },
            },
        )
        def stop_watching(what: str = "") -> str:
            dropped = watches.cancel(what)
            if not dropped:
                return "I wasn't watching anything matching that."
            if not what.strip():
                return f"Stopped all {dropped} watches."
            return f"Stopped {dropped} watch{'es' if dropped > 1 else ''}."

    # ── background jobs (work that outlives this conversation) ───────────────
    if jobs is not None:
        @reg.tool(
            description=(
                "Hands a whole question to your background worker to go and "
                "research properly — several web searches, then a written "
                "answer — and comes back to him with it later. Use when he "
                "asks you to look into / research / dig into something, or "
                "when a real answer plainly needs more than one search: "
                "'iske baare mein pata karo', 'research this properly', "
                "'find out everything about X and tell me'.\n"
                "NOT for a quick fact — use web_search for anything one search "
                "answers, because that comes back inside this conversation.\n"
                "`goal` must be self-contained enough to work on an hour from "
                "now: resolve 'it', 'that' and 'him' into real names first. "
                "Tell him you'll go and do it and come back — then move on. Do "
                "NOT wait for it or keep asking about it in this conversation."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string",
                             "description": "The question to research, self-contained"},
                },
                "required": ["goal"],
            },
        )
        def research_in_background(goal: str) -> str:
            try:
                jobs.submit("research", goal, origin="voice")
            except ValueError as exc:      # at the per-type ceiling
                return str(exc)
            except Exception as exc:       # noqa: BLE001 — never a spoken traceback
                return f"I couldn't start that: {exc}"
            return ("On it — I'll go and look into that properly and come back "
                    "to you when I have the answer.")

        @reg.tool(
            description=("Says what background work is running and how far "
                         "along it is. Use for 'what are you working on?', "
                         "'us research ka kya hua?', 'any progress?'."),
        )
        def background_jobs() -> str:
            return jobs.summary()

        @reg.tool(
            description=(
                "Stops background work. Pass a few words of what he wants "
                "stopped; omit `what` to stop everything. Use for 'stop that "
                "research', 'sab cancel kar do'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string",
                             "description": "Words identifying the job; omit for all"},
                },
            },
        )
        def cancel_background_job(what: str = "") -> str:
            stopped = jobs.cancel_matching(what)
            if not stopped:
                return "I'm not working on anything matching that."
            if not what.strip():
                return f"Stopped all {stopped} background jobs."
            return f"Stopped {stopped} job{'s' if stopped > 1 else ''}."

    if jobs is not None:
        register_build_tools(reg, jobs, default_dir=config.build_dir)

    if config.dev.repos:
        register_dev_tools(reg, config.dev, jobs=jobs)

    if archive is not None:
        register_recall_tools(reg, archive)

    if config.documents_dir:
        register_document_tools(reg, config.documents_dir)

    if projects is not None:
        register_project_tools(reg, projects)

    if outcomes is not None:
        register_learning_tools(reg, outcomes)

    if audit is not None:
        @reg.tool(
            description=(
                "Says what she has actually done recently — the real record of "
                "tool calls, including anything she was refused. Use for 'what "
                "have you been doing?', 'kya kiya tune?', 'did you do anything "
                "while I was away?'. This is the log, not her memory: report "
                "exactly what it returns and never pad it out."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "count": {"type": "integer",
                              "description": "How many recent actions (default 5)"},
                },
            },
        )
        def recent_activity(count: int = 5) -> str:
            return audit.summary(max(1, min(int(count or 5), 20)))

    @reg.tool(
        description=(
            "Searches the web (Google) for current, real information. Use for "
            "ANY question about facts, news, prices, people, places, or anything "
            "you are not certain about. Never guess when you can search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "A clear, focused search query"},
            },
            "required": ["query"],
        },
    )
    def web_search(query: str) -> str:
        provider = GeminiProvider(config.gemini_api_key)
        return provider.grounded_search(query)

    @reg.tool(
        description=("Gets the current weather. If no city is given, uses the "
                     "user's current location."),
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string",
                         "description": "City name; omit for current location"},
            },
        },
    )
    def weather_report(city: str = "") -> str:
        city = (city or "").strip()
        if not city and location is not None:
            loc = location.get()
            city = (loc or {}).get("city") or ""
        if not city:
            city = home_city(memory)
        if not city:
            return "Which city? I couldn't tell where you are right now."
        return fetch_weather(city)

    if location is not None:
        @reg.tool(
            description=("Gets the user's current approximate location (city, "
                         "region, country) from network geolocation. Use for "
                         "'where am I' and to ground local questions."),
        )
        def where_am_i() -> str:
            loc = location.get()
            if not loc:
                return "I can't determine your location right now."
            desc = ", ".join(p for p in (loc.get("city"), loc.get("region"),
                                         loc.get("country")) if p)
            return f"You appear to be in {desc}."

    @reg.tool(description="Gets the current date and time.")
    def current_time() -> str:
        return time.strftime("It is %A, %B %d, %Y — %I:%M %p.")

    @reg.tool(
        description=(
            "Sets a countdown timer. When it finishes, a chime plays in the "
            "user's headset and you announce it."
        ),
        parameters={
            "type": "object",
            "properties": {
                "minutes": {"type": "number", "description": "Duration in minutes"},
                "label":   {"type": "string", "description": "What the timer is for"},
            },
            "required": ["minutes"],
        },
    )
    def set_timer(minutes: float, label: str = "") -> str:
        timer = timers.add(minutes, label)
        return f"Timer '{timer.label}' set for {minutes:g} minute(s)."

    @reg.tool(description="Lists the currently running timers.")
    def check_timers() -> str:
        pending = timers.pending()
        if not pending:
            return "No timers are running."
        return "; ".join(f"'{label}' — {remaining:.1f} min left"
                         for label, remaining in pending)

    @reg.tool(
        description="Sets the headset volume to an absolute level.",
        parameters={
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Volume 0-100"},
            },
            "required": ["percent"],
        },
    )
    def set_volume(percent: int) -> str:
        return set_system_volume(percent)

    @reg.tool(
        description=(
            "Turns the volume up or down by a relative step. Use for 'louder', "
            "'volume badhao', 'thoda kam karo', 'turn it down a bit'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "delta": {"type": "integer",
                          "description": "Signed percent change, e.g. 10 or -10"},
            },
            "required": ["delta"],
        },
    )
    def change_volume(delta: int) -> str:
        return change_system_volume(delta)

    @reg.tool(
        description=(
            "Reports the wearable device's own health — temperature, memory, "
            "disk, uptime. Use when the user asks how the device is doing, if "
            "it's hot, or why it feels slow."
        ),
    )
    def device_status() -> str:
        return device_vitals()

    @reg.tool(
        description=(
            "Save an important personal fact about the user to long-term memory. "
            "Call silently whenever the user reveals something worth remembering. "
            "Values in English regardless of conversation language."
        ),
        parameters={
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": ("identity | preferences | projects | "
                                    "relationships | places | wishes | notes"),
                },
                "key":      {"type": "string", "description": "Short snake_case key"},
                "value":    {"type": "string", "description": "Concise value in English"},
            },
            "required": ["category", "key", "value"],
        },
    )
    def save_memory(category: str, key: str, value: str) -> str:
        return memory.remember(category, key, value)

    if reminders is not None:
        @reg.tool(
            description=(
                "Sets a persistent reminder that survives reboots and fires at "
                "a wall-clock time (unlike set_timer, which is a short relative "
                "countdown). Use for 'remind me...' at a date/time or later "
                "today/tomorrow. When it's due, a chime plays and you announce "
                "it. Pass EITHER minutes_from_now for short delays, OR at_time "
                "as 'YYYY-MM-DD HH:MM' (24-hour, local) computed from the "
                "current date/time you were given."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": "What to remind about, e.g. 'call mom'"},
                    "minutes_from_now": {"type": "number",
                                         "description": "Delay in minutes (for soon)"},
                    "at_time": {"type": "string",
                                "description": "Absolute 'YYYY-MM-DD HH:MM' local"},
                },
                "required": ["text"],
            },
        )
        def set_reminder(text: str, minutes_from_now: float | None = None,
                         at_time: str | None = None) -> str:
            try:
                due, phrase = parse_reminder_time(minutes_from_now, at_time)
            except ValueError as exc:
                return f"I couldn't set that reminder: {exc}."
            reminders.add(text, due)
            return f"Reminder set: '{text.strip()}' {phrase}."

        @reg.tool(description="Lists all upcoming persistent reminders.")
        def list_reminders() -> str:
            pending = reminders.pending()
            if not pending:
                return "No reminders are set."
            return "; ".join(
                f"'{r['text']}' at "
                f"{time.strftime('%a %I:%M %p', time.localtime(r['due']))}"
                for r in pending)

        @reg.tool(
            description="Cancels reminders matching some text.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to match"},
                },
                "required": ["text"],
            },
        )
        def cancel_reminder(text: str) -> str:
            n = reminders.cancel(text)
            return f"Cancelled {n} reminder(s)." if n else "No matching reminder."

    if notes is not None:
        @reg.tool(
            description=("Saves a quick voice note for the user to review later. "
                         "Use for 'note that...', 'take a note', 'jot down...'."),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The note content"},
                },
                "required": ["text"],
            },
        )
        def add_note(text: str) -> str:
            notes.add(text)
            return "Noted."

        @reg.tool(description="Reads back all saved voice notes.")
        def read_notes() -> str:
            items = notes.all()
            if not items:
                return "You have no notes."
            return " • ".join(n["text"] for n in items if n.get("text"))

        @reg.tool(description="Deletes all saved voice notes.")
        def clear_notes() -> str:
            return f"Cleared {notes.clear()} note(s)."

    if lists is not None:
        @reg.tool(
            description=("Adds an item to a named list (default 'shopping'). "
                         "Use for 'add milk to my shopping list', 'add X to "
                         "todo'."),
            parameters={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "Item to add"},
                    "list_name": {"type": "string",
                                  "description": "List name, e.g. shopping, todo"},
                },
                "required": ["item"],
            },
        )
        def add_to_list(item: str, list_name: str = "shopping") -> str:
            return lists.add_item(item, list_name)

        @reg.tool(
            description="Removes an item from a named list (default 'shopping').",
            parameters={
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "Item to remove"},
                    "list_name": {"type": "string", "description": "List name"},
                },
                "required": ["item"],
            },
        )
        def remove_from_list(item: str, list_name: str = "shopping") -> str:
            return lists.remove_item(item, list_name)

        @reg.tool(
            description="Reads back a named list (default 'shopping').",
            parameters={
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "List name"},
                },
            },
        )
        def show_list(list_name: str = "shopping") -> str:
            items = lists.show(list_name)
            if not items:
                return f"The {list_name} list is empty."
            return f"{list_name}: " + ", ".join(items)

        @reg.tool(
            description="Empties a named list (default 'shopping').",
            parameters={
                "type": "object",
                "properties": {
                    "list_name": {"type": "string", "description": "List name"},
                },
            },
        )
        def clear_list(list_name: str = "shopping") -> str:
            return f"Cleared {lists.clear(list_name)} item(s) from {list_name}."

    if config.screen.ready:
        @reg.tool(
            description=(
                "Reads the text currently on the user's laptop screen and returns "
                "it. Use whenever the user asks you to look at, read, check, or "
                "help debug what's on their screen — an error, code, a log, a "
                "message, anything. The laptop does local OCR and sends back the "
                "on-screen text; read it, reason over it, and answer by voice. "
                "It captures the active window, so ask the user to focus what they "
                "want you to see. Text only — you won't get colours or layout."
            ),
        )
        def look_at_screen() -> str:
            sc = config.screen
            try:
                resp = requests.get(
                    f"http://{sc.host}:{sc.port}/screen_text",
                    params={"token": sc.token}, timeout=sc.timeout)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                log.warning("look_at_screen fetch failed: %s", exc)
                return ("I couldn't reach your laptop screen — is the screen "
                        "server running and on the same network?")
            text = (data.get("text") or "").strip()
            if not text:
                return ("I looked, but there's no readable text on the active "
                        "window right now.")
            # Keep the spoken turn snappy — she doesn't need the whole essay.
            if len(text) > 4000:
                text = text[:4000] + " …(truncated)"
            return "This is the text on the screen right now:\n" + text

    if config.laptop.ready:
        @reg.tool(
            description=(
                "Runs a task ON THE USER'S LAPTOP through FLINT, the desktop "
                "assistant running there: open/close apps, play a video or "
                "song on the laptop, browser searches, files, typing, system "
                "settings — anything done at the computer. Use whenever the "
                "user asks for something 'on the laptop / computer / PC'. "
                "Pass ONE clear, self-contained instruction in English. The "
                "reply is what FLINT reports back after doing it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": "The task, e.g. 'open spotify', "
                                               "'search cheap flights to Goa'"},
                },
                "required": ["command"],
            },
        )
        def laptop_task(command: str) -> str:
            from venom.laptop import run_laptop_task
            return run_laptop_task(config.laptop, command)

    if config.camera.ready:
        @reg.tool(
            description=(
                "Looks through the Raspberry Pi camera and tells the user what "
                "is in front of it. Use whenever they ask what you see, what's in "
                "front of them, to identify or read an object, count things, or "
                "any question about the physical scene — 'what do you see?', "
                "'kya dikh raha hai?', 'what is this?', 'read this label'. Pass "
                "their specific question so you look for the right thing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "question": {"type": "string",
                                 "description": "What to look for; omit for a general description"},
                },
            },
        )
        def look_around(question: str = "") -> str:
            from venom import camera
            return camera.describe_scene(config, question)

        @reg.tool(
            description=(
                "Looks for one specific thing through the camera and says "
                "whether it's there and exactly where. Use for 'where are my "
                "keys?', 'can you see my phone?', 'mera wallet dikh raha "
                "hai?'. Different from look_around, which just describes the "
                "scene — use this whenever he's looking FOR something. Report "
                "exactly what comes back, including a plain no."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "thing": {"type": "string",
                              "description": "What to look for, e.g. 'my keys'"},
                },
                "required": ["thing"],
            },
        )
        def find_object(thing: str) -> str:
            from venom import camera
            return camera.find_object(config, thing)

        @reg.tool(
            description=(
                "Takes a photo with the Raspberry Pi camera, sends it to the "
                "user's phone, and describes it aloud. Use when they say 'take a "
                "photo', 'take a shot', 'click a picture', 'photo le lo', or "
                "similar."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "caption": {"type": "string",
                                "description": "Optional caption to send with the photo"},
                },
            },
        )
        def take_photo(caption: str = "") -> str:
            from venom import camera
            return camera.take_photo(config, caption)

    if config.phone.ready:
        @reg.tool(
            description=(
                "Rings the user's phone loudly to help them find it, even if "
                "it's on silent. Use for 'find my phone', 'where's my phone', "
                "'ring my phone', 'make my phone ring' — in any language."
            ),
        )
        def find_my_phone() -> str:
            from venom.phone import find_phone
            return find_phone(config.phone.ntfy_server, config.phone.ntfy_topic)

    @reg.tool(
        description=(
            "Turns live TRANSLATION (interpreter) mode on or off. Call with "
            "enable=true when the user asks to translate, says 'translation "
            "mode', 'interpreter', or 'translate karo'; enable=false when they "
            "say stop / normal / 'band karo'. In this mode you are a two-way "
            "interpreter between Hindi and Kannada/Telugu, nothing else."
        ),
        parameters={
            "type": "object",
            "properties": {
                "enable": {"type": "boolean",
                           "description": "true to start translating, false to stop"},
            },
            "required": ["enable"],
        },
    )
    def translation_mode(enable: bool) -> str:
        if enable:
            return (
                "TRANSLATION MODE ON. You are now a live two-way interpreter, not "
                "Jarvis. For every utterance from here on: if you hear Kannada or "
                "Telugu, say ONLY its Hindi translation; if you hear Hindi, say "
                "ONLY its translation in whichever of Kannada/Telugu the other "
                "person is speaking (the most recent non-Hindi language you "
                "heard). Just the translation — spoken naturally, no greetings, "
                "no commentary, no extra words. Keep going until translation mode "
                "is turned off."
            )
        return ("TRANSLATION MODE OFF. Resume being Jarvis — warm, normal "
                "conversation in your usual Hinglish.")

    @reg.tool(
        description=(
            "Ends the current conversation and returns to wake-word listening. "
            "Call when the user says goodbye, thanks you and is done, or asks "
            "you to stop listening — in any language."
        ),
    )
    def end_conversation() -> str:
        return "Ending conversation."

    @reg.tool(
        description=(
            "Cleanly powers OFF the whole device (not just the conversation). "
            "Call ONLY when the user explicitly says to power off, shut down, "
            "switch you off, or sign out for the day/night. Say a warm goodbye "
            "in your reply before this — the device shuts down right after."
        ),
    )
    def power_off() -> str:
        try:
            CONTROL_REQUEST.parent.mkdir(parents=True, exist_ok=True)
            CONTROL_REQUEST.write_text("poweroff")
        except OSError as exc:
            return f"I couldn't shut down: {exc}"
        return "Powering off. Good night, take care."

    return reg


def register_project_tools(reg, projects, clock=time.time):
    """Tracking real work: tasks, deadlines, and what's actually blocked.

    Deliberately separate from add_to_list/add_note, which are for flat lists
    ("buy milk"). The difference the user feels is that this one can answer
    "what should I do next" — because it knows what is waiting on what.
    """
    def _due_epoch(in_hours: float | None, in_days: float | None) -> float | None:
        if in_hours:
            return clock() + float(in_hours) * 3600
        if in_days:
            return clock() + float(in_days) * 86400
        return None

    @reg.tool(
        description=(
            "Tracks a piece of real work with a deadline and what it's waiting "
            "on. Use for 'remind me to finish X by Friday', 'add a task', "
            "'I need to do X after Y is done'. NOT for shopping items — that's "
            "add_to_list. Set `after` to whatever must happen first."
        ),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What needs doing"},
                "project": {"type": "string", "description": "Which project, if any"},
                "in_hours": {"type": "number", "description": "Due in this many hours"},
                "in_days": {"type": "number", "description": "Due in this many days"},
                "after": {"type": "string",
                          "description": "A few words of the task this waits on"},
            },
            "required": ["title"],
        },
    )
    def add_task(title: str, project: str = "", in_hours: float | None = None,
                 in_days: float | None = None, after: str = "") -> str:
        try:
            task = projects.add_task(
                title, project=project, due=_due_epoch(in_hours, in_days),
                depends_on=[after] if after.strip() else ())
        except Exception as exc:  # noqa: BLE001 — spoken, never a traceback
            return str(exc)
        if task["depends_on"]:
            return f"Added — {title}, once {after} is done."
        return f"Added — {title}."

    @reg.tool(
        description=("Says what he should actually work on: overdue things, "
                     "what's ready to start, what's blocked. Use for 'what's "
                     "on?', 'what should I do next?', 'kya karna hai?'."),
        parameters={
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Limit to one project"},
            },
        },
    )
    def whats_next(project: str = "") -> str:
        return projects.summary(project)

    @reg.tool(
        description=("Explains why a task can't start yet — names the actual "
                     "thing blocking it. Use for 'why can't I start X?', "
                     "'what's X waiting on?'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words of the task"},
            },
            "required": ["task"],
        },
    )
    def why_blocked(task: str) -> str:
        return projects.explain(task)

    @reg.tool(
        description=("Marks a task done. Use for 'I finished X', 'X ho gaya', "
                     "'mark X complete'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "A few words of the task"},
            },
            "required": ["task"],
        },
    )
    def complete_task(task: str) -> str:
        done = projects.complete(task)
        if done is None:
            return f"I don't have a task matching {task}."
        return f"Nice — {done['title']} is done."

    @reg.tool(
        description=("Records that one task has to wait for another. Use for "
                     "'X can't start until Y is done', 'do Y first'."),
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "The task that waits"},
                "after": {"type": "string", "description": "What must happen first"},
            },
            "required": ["task", "after"],
        },
    )
    def block_task(task: str, after: str) -> str:
        try:
            projects.block_on(task, after)
        except Exception as exc:  # noqa: BLE001
            return str(exc)
        return f"Got it — {task} waits for {after}."


def register_learning_tools(reg, outcomes):
    """Reading back her own record: why she chose something, what she's learned."""

    @reg.tool(
        description=(
            "Explains why she actually chose what she chose — reads back the "
            "reason recorded at the time, never a story made up afterwards. "
            "Use for 'why did you do that?', 'why that one?', 'aisa kyun kiya?'. "
            "If nothing was recorded, say so plainly."
        ),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string",
                          "description": "Which kind of decision, e.g. 'which agent'"},
            },
        },
    )
    def why_did_you(about: str = "") -> str:
        return outcomes.explain(about)

    @reg.tool(
        description=("Says what she's picked up about how he likes things done, "
                     "from what actually happened before. Use for 'what have "
                     "you learned about me?', 'what do you know by now?'."),
    )
    def what_have_you_learned() -> str:
        notes = outcomes.advice()
        if not notes:
            return ("Nothing solid yet — I haven't seen enough to call it a "
                    "pattern, and I'd rather not guess.")
        return " ".join(notes)


def register_build_tools(reg, jobs, default_dir: str = ""):
    """Building software by voice — handed to a coding agent as a long job."""

    @reg.tool(
        description=(
            "Builds a working application from a description — writes it, "
            "runs it, and keeps fixing it until it works. Use when he asks "
            "you to build / make / write an app, a script, a tool, a game: "
            "'ek script bana do', 'build me a CLI that...'. This takes many "
            "minutes and happens in the background: say you're on it and "
            "you'll come back, then carry on. Do NOT wait for it. "
            "`where` is the folder to build in — ask him if you don't know."
        ),
        parameters={
            "type": "object",
            "properties": {
                "what": {"type": "string",
                         "description": "What to build, in full — the whole brief"},
                "where": {"type": "string",
                          "description": "Folder to build in, if he named one"},
            },
            "required": ["what"],
        },
    )
    def build_app(what: str, where: str = "") -> str:
        target = (where or default_dir).strip()
        if not target:
            return ("I need to know which folder to build in — tell me where "
                    "and I'll get started.")
        try:
            jobs.submit("build", what, origin="voice",
                        params={"cwd": target, "task": "code"})
        except ValueError as exc:          # already building something
            return str(exc)
        except Exception as exc:           # noqa: BLE001 — never a spoken traceback
            return f"I couldn't start that: {exc}"
        return ("On it — I'll build it, run it, and keep at it until it "
                "works. I'll come back to you when it's done.")


def register_recall_tools(reg, archive):
    """The searchable memory tier — everything that doesn't fit in the prompt."""

    @reg.tool(
        description=(
            "Searches everything she's ever filed away — old conversations, "
            "details about people and projects, things from months ago. Use "
            "when he refers to something you don't already have in front of "
            "you: 'that thing we discussed', 'what did I say about X?', "
            "'us project ka kya scene tha?'. Report only what comes back; if "
            "nothing does, say you don't remember rather than guessing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string",
                          "description": "What to look for — names and specifics work best"},
            },
            "required": ["about"],
        },
    )
    def remember_about(about: str) -> str:
        found = archive.search(about)
        if not found:
            return f"I've got nothing filed about {about}."
        return " ".join(entry.line() for entry in found)

    @reg.tool(
        description=(
            "Files something away for the long term — bigger than a one-line "
            "preference (that's save_memory). Use for details about projects, "
            "people, or anything he says he'll want later."
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "What to remember, in full"},
                "subject": {"type": "string",
                            "description": "Who or what it's about"},
                "kind": {"type": "string",
                         "description": "fact, project, person, or episode"},
            },
            "required": ["text"],
        },
    )
    def file_away(text: str, subject: str = "", kind: str = "fact") -> str:
        if archive.remember(text, kind=kind, subject=subject) is None:
            return "There was nothing there to file."
        return "Filed that away."

    @reg.tool(
        description=("Forgets everything filed about something. Use for "
                     "'forget about X', 'X ke baare mein bhool ja'."),
        parameters={
            "type": "object",
            "properties": {
                "about": {"type": "string", "description": "What to forget"},
            },
            "required": ["about"],
        },
    )
    def forget_about(about: str) -> str:
        dropped = archive.forget_matching(about)
        if not dropped:
            return f"I had nothing filed about {about} anyway."
        return f"Forgotten — dropped {dropped} thing{'s' if dropped > 1 else ''}."


def register_document_tools(reg, folder: str):
    """Writing things out: notes, spreadsheets, slide decks."""
    from flint_core.documents import (
        list_documents,
        read_document,
        write_document,
        write_presentation,
        write_spreadsheet,
    )

    @reg.tool(
        description=(
            "Writes a document — notes, a summary, a letter, a write-up. "
            "Markdown by default; pass a name ending .txt or .docx for those. "
            "Use for 'write this up', 'make me a note about...', 'draft a...'. "
            "Set overwrite only if he says to replace an existing file."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename, e.g. 'meeting notes'"},
                "content": {"type": "string", "description": "The full body text"},
                "title": {"type": "string", "description": "Heading, for markdown"},
                "overwrite": {"type": "boolean",
                              "description": "true only if replacing on purpose"},
            },
            "required": ["name", "content"],
        },
    )
    def write_note(name: str, content: str, title: str = "",
                   overwrite: bool = False) -> str:
        return write_document(folder, name, content, title=title,
                              overwrite=overwrite).spoken()

    @reg.tool(
        description=(
            "Writes a spreadsheet from rows of values. CSV by default (opens "
            "in Excel); .xlsx if he asks for Excel specifically. Use for "
            "'make a spreadsheet of...', 'track my spending'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename"},
                "headers": {"type": "array", "items": {"type": "string"},
                            "description": "Column names"},
                "rows": {"type": "array",
                         "items": {"type": "array", "items": {"type": "string"}},
                         "description": "Each row as a list of cell values"},
                "overwrite": {"type": "boolean", "description": "Replace on purpose"},
            },
            "required": ["name", "rows"],
        },
    )
    def write_sheet(name: str, rows: list, headers: list | None = None,
                    overwrite: bool = False) -> str:
        return write_spreadsheet(folder, name, rows, headers=headers or (),
                                 overwrite=overwrite).spoken()

    @reg.tool(
        description=(
            "Writes a slide deck. Markdown by default (imports into any slide "
            "tool); .pptx if he asks for PowerPoint. Each slide needs a title "
            "and bullets. Use for 'make me a deck about...'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Filename"},
                "title": {"type": "string", "description": "Deck title"},
                "slides": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "bullets": {"type": "array", "items": {"type": "string"}},
                            "notes": {"type": "string"},
                        },
                    },
                    "description": "The slides, in order",
                },
                "overwrite": {"type": "boolean", "description": "Replace on purpose"},
            },
            "required": ["name", "slides"],
        },
    )
    def write_deck(name: str, slides: list, title: str = "",
                   overwrite: bool = False) -> str:
        return write_presentation(folder, name, slides, title=title,
                                  overwrite=overwrite).spoken()

    @reg.tool(
        description=("Lists the documents she's written, newest first. Use for "
                     "'what have you written?', 'show me my notes'."),
    )
    def list_notes() -> str:
        found = list_documents(folder)
        if not found:
            return "I haven't written anything out yet."
        return "Most recent first: " + ", ".join(found[:10]) + "."

    @reg.tool(
        description=("Reads back a document she wrote, so it can be read out "
                     "or edited. Use for 'read me the meeting notes'."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The filename"},
            },
            "required": ["name"],
        },
    )
    def read_note(name: str) -> str:
        from pathlib import Path

        from flint_core.documents import safe_name

        return read_document(Path(folder) / safe_name(name, "md"))


def register_dev_tools(reg, dev, jobs=None):
    """Git and deployment, bounded by the repos and targets named in config.

    Every tool here resolves a *name* against the allowlist rather than taking
    a path or a host. She cannot reach a repo you did not name, and cannot
    deploy anywhere you did not list — which is the whole reason these were
    not wired up until you decided what they may touch.
    """
    from flint_core.deploy import DeployTargets
    from flint_core.vcs import GitRepo

    targets = DeployTargets.from_config(list(dev.deploy_targets))

    def _repo(name: str):
        path = dev.repo_path(name) or (dev.default_repo if not name else "")
        if not path:
            known = ", ".join(dev.repo_names) or "none"
            return None, (f"I don't have a repo called {name or 'that'}. "
                          f"I know about: {known}.")
        return GitRepo(path), ""

    @reg.tool(
        description=(
            "Says what's changed in a repo — branch, modified files, recent "
            "commits. Use for 'what have I changed?', 'kya status hai?', "
            "'what branch am I on?'. Name the repo if he has more than one."
        ),
        parameters={
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
        },
    )
    def code_status(repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        if not git.is_repo():
            return "That folder isn't a git repo."
        changed = git.changed_files()
        branch = git.branch()
        if not changed:
            return f"On {branch}, nothing changed — working tree is clean."
        shown = ", ".join(changed[:5])
        more = f" and {len(changed) - 5} more" if len(changed) > 5 else ""
        return f"On {branch} with {len(changed)} file(s) changed: {shown}{more}."

    @reg.tool(
        description=(
            "Commits the current changes with a message. Refuses on main or "
            "master — make a branch first. Use for 'commit this', 'commit kar "
            "de with message X'. Never invent a message: use his words, or "
            "ask what the change was."
        ),
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The commit message"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["message"],
        },
    )
    def commit_code(message: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        return git.commit(message).text

    @reg.tool(
        description=("Starts a new branch in a repo. Use for 'make a branch "
                     "called X', 'nayi branch banao'."),
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Branch name"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["name"],
        },
    )
    def new_branch(name: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        result = git.create_branch(name)
        return f"You're on {name} now." if result.ok else result.text

    @reg.tool(
        description=("Pushes the current branch and opens a pull request. Use "
                     "for 'push it', 'raise a PR', 'PR bana do'."),
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR title"},
                "repo": {"type": "string", "description": "Which repo, by name"},
            },
            "required": ["title"],
        },
    )
    def open_pull_request(title: str, repo: str = "") -> str:
        git, problem = _repo(repo)
        if problem:
            return problem
        pushed = git.push()
        if not pushed.ok:
            return f"Couldn't push: {pushed.text}"
        return git.pull_request(title).text

    if targets and jobs is not None:
        @reg.tool(
            description=(
                "Deploys a project to one of his configured targets. By "
                "DEFAULT this only says what it would do and changes nothing "
                "— tell him what it reports, then call it again with "
                "confirm=true ONLY if he clearly says go ahead. Never pass "
                "confirm=true on the first try, and never pick a target he "
                "didn't name. Known targets: " + ", ".join(targets.names()) + "."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": "Which target, by name"},
                    "repo": {"type": "string", "description": "Which repo, by name"},
                    "confirm": {"type": "boolean",
                                "description": "true ONLY after he says go ahead"},
                },
                "required": ["target"],
            },
        )
        def deploy_project(target: str, repo: str = "",
                           confirm: bool = False) -> str:
            path = dev.repo_path(repo) or dev.default_repo
            if not path:
                return "I don't know which project to deploy."
            try:
                jobs.submit("deploy", f"deploy to {target}", origin="voice",
                            params={"cwd": path, "target": target,
                                    "confirm": bool(confirm)})
            except ValueError as exc:
                return str(exc)
            except Exception as exc:      # noqa: BLE001
                return f"I couldn't start that: {exc}"
            if confirm:
                return "Deploying now — I'll tell you how it goes."
            return ("Checking what that would do — I'll read it back to you "
                    "before anything ships.")

        @reg.tool(
            description=("Lists the places she's allowed to deploy to. Use for "
                         "'where can you deploy?'."),
        )
        def deploy_targets() -> str:
            return targets.describe()
