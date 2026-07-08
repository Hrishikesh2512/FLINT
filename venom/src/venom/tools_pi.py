"""Venom's standalone tool belt — everything the Pi can do with just
Wi-Fi, a headset, and cloud APIs. Registered on flint-core's ToolRegistry,
so declarations/dispatch/docs come from one definition, same as Flint.

Timers are a plain in-memory board the voice loop polls: when one fires,
Venom chimes through the headset and announces it on the next exchange.
"""

from __future__ import annotations

import logging
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


# ── volume (ALSA) ─────────────────────────────────────────────────────────────
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
                      location=None, chess=None, notifications=None) -> ToolRegistry:
    reg = ToolRegistry(platform="linux")

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

        @reg.tool(description="Tells what music is currently playing, if any.")
        def now_playing() -> str:
            title = music.now_playing
            return f"Now playing: {title}." if title else "Nothing is playing."

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
        description="Sets the headset volume.",
        parameters={
            "type": "object",
            "properties": {
                "percent": {"type": "integer", "description": "Volume 0-100"},
            },
            "required": ["percent"],
        },
    )
    def set_volume(percent: int) -> str:
        return set_alsa_volume(percent)

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
