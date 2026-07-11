"""Google Calendar, the zero-OAuth way: the calendar's secret iCal URL.

Read-only and quota-free — the Pi fetches the .ics on a slow cadence and
answers "what's on today?" / "when's my next class?". A background
watcher also chimes ahead of events through the same pending-reminder
machinery reminders use, so "seminar in 30 minutes" arrives even while
Venom is asleep or in Bluetooth focus.

RRULE expansion (recurring lectures!) is handled by recurring-ical-events;
hand-rolling recurrence is how calendar integrations quietly lie to you.
The fetcher is injectable so parsing and the watcher are testable offline.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger("venom.gcal")


@dataclass(frozen=True)
class Event:
    start: dt.datetime  # timezone-aware, local
    end: dt.datetime
    summary: str

    @property
    def uid(self) -> str:
        return f"{self.start.isoformat()}|{self.summary}"


def _default_fetch(url: str) -> bytes:
    import requests

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.content


def parse_events(ics: bytes, start: dt.datetime, end: dt.datetime) -> list[Event]:
    """Expand the feed (including recurrences) into concrete events."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(ics)
    local = dt.datetime.now().astimezone().tzinfo
    out: list[Event] = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        begin = ev.get("DTSTART").dt
        finish = (ev.get("DTEND") or ev.get("DTSTART")).dt
        # All-day events come back as dates — pin them to local midnight.
        if not isinstance(begin, dt.datetime):
            begin = dt.datetime.combine(begin, dt.time.min, tzinfo=local)
        if not isinstance(finish, dt.datetime):
            finish = dt.datetime.combine(finish, dt.time.min, tzinfo=local)
        out.append(Event(begin.astimezone(local), finish.astimezone(local),
                         str(ev.get("SUMMARY", "busy"))))
    out.sort(key=lambda e: e.start)
    return out


class CalendarFeed:
    """Cached window over the iCal feed: yesterday → +14 days."""

    def __init__(self, url: str, fetch=_default_fetch,
                 clock=time.monotonic, now=None):
        self._url = url
        self._fetch = fetch
        self._clock = clock
        self._now = now or (lambda: dt.datetime.now().astimezone())
        self._lock = threading.Lock()
        self._events: list[Event] = []
        self._fetched_at: float | None = None
        self._error = ""

    def refresh(self) -> None:
        try:
            today = self._now()
            events = parse_events(self._fetch(self._url),
                                  today - dt.timedelta(days=1),
                                  today + dt.timedelta(days=14))
            with self._lock:
                self._events = events
                self._fetched_at = self._clock()
                self._error = ""
        except Exception as exc:
            log.warning("calendar refresh failed: %s", exc)
            with self._lock:
                self._error = str(exc)

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    @property
    def healthy(self) -> bool:
        with self._lock:
            return self._fetched_at is not None

    # ── spoken answers ────────────────────────────────────────────────────────
    def agenda(self, day: str = "today") -> str:
        if not self.healthy:
            return ("I couldn't load your calendar yet — I'll keep trying; "
                    "ask me again in a minute.")
        now = self._now()
        target = now.date()
        if day.strip().lower() in ("tomorrow", "kal"):
            target = target + dt.timedelta(days=1)
        todays = [e for e in self.events() if e.start.date() == target]
        label = "today" if target == now.date() else "tomorrow"
        if not todays:
            return f"Nothing on the calendar {label}."
        parts = [f"{e.summary} at {e.start.strftime('%I:%M %p').lstrip('0')}"
                 for e in todays[:8]]
        return f"{label.capitalize()} you have: " + "; ".join(parts) + "."

    def next_event(self) -> str:
        if not self.healthy:
            return ("I couldn't load your calendar yet — ask me again in a "
                    "minute.")
        now = self._now()
        for e in self.events():
            if e.start > now:
                gap = e.start - now
                hours, rem = divmod(int(gap.total_seconds()) // 60, 60)
                when = e.start.strftime("%A %I:%M %p").lstrip("0")
                in_txt = (f"in {rem} minutes" if hours == 0
                          else f"in {hours}h {rem:02d}m")
                return f"Next up: {e.summary}, {when} — {in_txt}."
        return "Nothing upcoming on the calendar in the next two weeks."


class CalendarWatcher:
    """Background refresher + proactive lead-time alerts.

    pop_due() is called from the wake loop every second, so it must never
    block: it only reads the cached event list. The feed refreshes on its
    own daemon thread."""

    def __init__(self, feed: CalendarFeed, lead_minutes: int = 30,
                 refresh_minutes: int = 5, now=None):
        self.feed = feed
        self._lead = dt.timedelta(minutes=max(1, lead_minutes))
        self._refresh_s = max(60, refresh_minutes * 60)
        self._now = now or (lambda: dt.datetime.now().astimezone())
        self._announced: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="calendar-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.feed.refresh()
            self._stop.wait(self._refresh_s)

    def pop_due(self) -> list[str]:
        """Events entering their lead window since the last call — spoken
        lines, each announced exactly once per event occurrence."""
        now = self._now()
        due: list[str] = []
        for e in self.feed.events():
            if e.uid in self._announced or e.start <= now:
                continue
            if e.start - now <= self._lead:
                self._announced.add(e.uid)
                minutes = max(1, int((e.start - now).total_seconds() // 60))
                due.append(f"{e.summary} in {minutes} minutes "
                           f"(at {e.start.strftime('%I:%M %p').lstrip('0')})")
        # keep the announced-set from growing forever
        if len(self._announced) > 512:
            self._announced = set(list(self._announced)[-128:])
        return due
