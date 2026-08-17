"""The tools any body needs: time, timers, search, weather, memory, the log.

Not skills so much as the floor under all of them — the last things left in
Venom's registry builder that were not actually about being a Raspberry Pi.

`register_basic_tools` takes a `search` callable rather than an API key. What
"search the web" means is a provider decision, and a device that resolves it
differently should not have to reimplement the tool to say so.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

from flint_core.memory import MemoryStore


# ────────────────────────────────────────────────────────────────────────── timers
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


# ──────────────────────────────────────────────────────────── weather (open-meteo: keyless)
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


# ──────────────────────────────────────────────────────────────────── personalisation
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


# ────────────────────────────────────────────────────────────────── reminder times
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


# ────────────────────────────────────────────────────────────────────── registrars
def register_basic_tools(reg, *, search: Callable[[str], str],
                         memory: MemoryStore, timers: TimerBoard | None = None,
                         weather: Callable[[str], str] = fetch_weather,
                         current_city: Callable[[], str] | None = None):
    """Search, weather, the clock, timers — the floor every body stands on.

    `current_city` is where he is *now*, as a city name, and it is a callable
    because each body answers it differently: the Pi from a network lookup, the
    phone from GPS. It is tried before the remembered home city — asking for
    the weather while travelling should not report the weather at home.
    """

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
        return search(query)

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
        if not city and current_city is not None:
            try:
                city = current_city() or ""
            except Exception:           # noqa: BLE001 — a lookup, not a crash
                city = ""
        if not city:
            city = home_city(memory)
        if not city:
            return "Which city? I couldn't tell where you are right now."
        return weather(city)

    @reg.tool(description="Gets the current date and time.")
    def current_time() -> str:
        return time.strftime("It is %A, %B %d, %Y — %I:%M %p.")

    if timers is None:
        return

    @reg.tool(
        description=(
            "Sets a countdown timer. When it finishes, a chime plays and you "
            "announce it."
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


def register_memory_tools(reg, memory: MemoryStore):
    """Saving a fact she should still know next week."""

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


def register_audit_tools(reg, audit):
    """What she has actually done, refusals included."""
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
