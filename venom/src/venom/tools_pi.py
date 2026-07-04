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

import requests

from flint_core.llm.providers import GeminiProvider
from flint_core.memory import MemoryStore
from flint_core.tools import ToolRegistry
from venom.config import VenomConfig

log = logging.getLogger("venom.tools")


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
                   now: str | None = None) -> str:
    """Facts for a spoken daily update — the model turns these into speech."""
    now = now or time.strftime("%A, %B %d, %Y, and it's %I:%M %p")
    parts = [f"Today is {now}."]
    city = home_city(memory)
    if city:
        try:
            parts.append("Weather — " + fetch_weather(city))
        except Exception:  # network hiccup shouldn't sink the whole briefing
            parts.append(f"(Couldn't fetch the weather for {city} right now.)")
    else:
        parts.append("(You don't know their city yet — ask where they are, "
                     "then save it with save_memory as identity/home_city.)")
    pending = timers.pending()
    if pending:
        parts.append("Running timers: " + "; ".join(
            f"{label} ({remaining:.0f} min left)" for label, remaining in pending))
    parts.append("Deliver this as a brief, warm spoken update — not a list.")
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
                      reminders=None, notes=None, lists=None) -> ToolRegistry:
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
        description="Gets the current weather for a city.",
        parameters={
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
            },
            "required": ["city"],
        },
    )
    def weather_report(city: str) -> str:
        return fetch_weather(city)

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
                                    "relationships | wishes | notes"),
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

    @reg.tool(
        description=(
            "Ends the current conversation and returns to wake-word listening. "
            "Call when the user says goodbye, thanks you and is done, or asks "
            "you to stop listening — in any language."
        ),
    )
    def end_conversation() -> str:
        return "Ending conversation."

    return reg
