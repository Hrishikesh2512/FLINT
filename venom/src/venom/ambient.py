"""Ambient awareness — the loop that lets Jarvis open the conversation.

Everything else in Venom is reactive: you say the wake word, she answers.
This module is the other half. On a slow tick it fuses what she already
knows — calendar, weather, mail, reminders, the Pi's own vitals, how long
you've been quiet — and decides whether any of it is worth interrupting
you for. When it is, she wakes *herself* and leads with it.

Three strictly separated stages, because the whole risk of a proactive
assistant is that it becomes a nag:

    sense   ->  gather() builds a World: plain data, no decisions
    judge   ->  each SIGNAL turns a World into zero or more Nudges
    gate    ->  AmbientGate decides whether a Nudge is spoken *now*

Only the gate is allowed to speak, and it is deliberately stingy: quiet
hours, a minimum gap between nudges, a per-kind cooldown, a daily cap, and
a persistent record so the same fact is never raised twice — across
reboots. A signal that has nothing *specific* to say returns nothing; a
vague "sab theek?" check-in is worse than silence.

The nudge itself is only facts plus a framing instruction. Jarvis phrases
it, in her own voice, the same way the WhatsApp announcement works.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from venom.config import AmbientConfig
from venom.stores import _JsonStore

log = logging.getLogger("venom.ambient")

# Weather-code words (from tools_pi._WEATHER_CODES) that mean "you will get
# wet". Matched against the spoken weather line rather than a raw code, so
# this keeps working whatever the weather source phrases it as.
WET_WORDS = ("rain", "drizzle", "shower", "thunderstorm", "snow")


# ── what she's about to say ──────────────────────────────────────────────────
@dataclass(frozen=True)
class Nudge:
    """One thing worth speaking first, unprompted.

    ``kind`` groups nudges for cooldown purposes; ``key`` identifies this
    exact fact and is remembered forever, so a given event or warning is
    raised at most once. ``priority`` is lowest-wins.
    """

    kind: str
    key: str
    priority: int
    facts: str
    ask: str = "ask if he wants you to do something about it."

    @property
    def instruction(self) -> str:
        """The [Proactive] opening handed to the live session."""
        return (
            f"[Proactive] {self.facts} You are opening this conversation "
            f"yourself — he has not said anything and does not know you're "
            f"about to speak. Lead with ONE short, specific Hinglish sentence "
            f"that says the actual thing, then {self.ask} Do not greet him, do "
            f"not ask how he is, do not offer generic help — if you have "
            f"nothing specific beyond this, say only this and stop."
        )


# ── what she knows right now ─────────────────────────────────────────────────
@dataclass(frozen=True)
class World:
    """A snapshot of everything the signals are allowed to read.

    Deliberately plain data: gathering does the blocking work (network,
    /proc) off the event loop, and every signal below is then a pure
    function of this — which is what makes the judging layer testable
    without a Pi, a calendar, or an internet connection.
    """

    now: float
    idle_seconds: float               # since the last real interaction
    events: tuple = ()                # upcoming calendar events, sorted
    weather: str = ""                 # spoken current-conditions line
    unread: int = -1                  # unread mail; -1 = unknown
    reminders: tuple = ()             # pending reminder dicts
    vitals: dict = field(default_factory=dict)  # temp_c / disk_pct / mem_pct

    @property
    def today(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.now))

    @property
    def hour(self) -> int:
        return time.localtime(self.now).tm_hour

    def events_within(self, seconds: float) -> list:
        """Upcoming events starting inside the given horizon."""
        horizon = self.now + seconds
        return [e for e in self.events
                if self.now < e.start.timestamp() <= horizon]


def _in_minutes(event, now: float) -> int:
    return max(1, int((event.start.timestamp() - now) // 60))


def _clock(event) -> str:
    return event.start.strftime("%I:%M %p").lstrip("0")


# ── the signals ──────────────────────────────────────────────────────────────
# Each is World x AmbientConfig -> list[Nudge]. Pure, cheap, and free to
# return nothing — which is the common and correct answer.

def rain_before_event(world: World, cfg: AmbientConfig) -> list[Nudge]:
    """Wet weather + somewhere to be = the umbrella you'd otherwise forget.

    Neither fact is worth a word on its own; together they're the whole
    point of a proactive assistant.
    """
    if not any(word in world.weather.lower() for word in WET_WORDS):
        return []
    upcoming = world.events_within(cfg.weather_horizon_hours * 3600)
    if not upcoming:
        return []
    event = upcoming[0]
    return [Nudge(
        kind="weather",
        key=f"rain:{event.uid}",
        priority=20,
        facts=(f"The weather right now is: {world.weather} And he has "
               f"'{event.summary}' at {_clock(event)}, in "
               f"{_in_minutes(event, world.now)} minutes — so he's heading "
               f"out into this."),
        ask="tell him to carry an umbrella.",
    )]


def early_start_tomorrow(world: World, cfg: AmbientConfig) -> list[Nudge]:
    """An early first thing tomorrow is only useful *tonight*, while he can
    still do something about it — so this fires in the evening window and
    stays quiet the rest of the day."""
    if not (cfg.evening_start_hour <= world.hour < cfg.evening_end_hour):
        return []
    tomorrow = dt.date.fromtimestamp(world.now) + dt.timedelta(days=1)
    for event in world.events:
        if event.start.date() < tomorrow:
            continue
        if event.start.date() > tomorrow:
            return []                       # nothing at all tomorrow
        # The first thing on tomorrow's calendar: worth a warning only if
        # it's early enough to ruin a late night.
        if event.start.hour >= cfg.early_hour:
            return []
        return [Nudge(
            kind="day_ahead",
            key=f"early:{event.uid}",
            priority=30,
            facts=(f"Tomorrow morning he has '{event.summary}' at "
                   f"{_clock(event)} — an early start, and it's evening now."),
            ask="suggest he doesn't stay up too late.",
        )]
    return []


def mail_pileup(world: World, cfg: AmbientConfig) -> list[Nudge]:
    """Unread mail is only interesting once it has *piled up* and he's been
    away from it for a while — otherwise it's just notification noise."""
    if world.unread < cfg.mail_pileup or world.idle_seconds < cfg.mail_idle_hours * 3600:
        return []
    # Re-fires only when the pile grows by another full step, so a stubborn
    # inbox of 6 doesn't get mentioned every single day.
    step = world.unread // cfg.mail_pileup
    return [Nudge(
        kind="mail",
        key=f"mail:{world.today}:{step}",
        priority=50,
        facts=(f"{world.unread} unread emails have piled up in his inbox, and "
               f"he hasn't spoken to you in "
               f"{world.idle_seconds / 3600:.0f} hours."),
        ask="offer to read out the headlines (call check_inbox if he says yes).",
    )]


def device_health(world: World, cfg: AmbientConfig) -> list[Nudge]:
    """Venom's own body. She is the only one who can see this, and by the
    time he notices it himself she has already stopped working — so this
    outranks everything else."""
    out: list[Nudge] = []
    temp = world.vitals.get("temp_c")
    if temp is not None and temp >= cfg.temp_warn_c:
        out.append(Nudge(
            kind="device",
            key=f"temp:{world.today}",
            priority=10,
            facts=(f"Your own hardware is running at {temp:.0f}°C — hot enough "
                   f"that the Pi will start throttling and you'll get slow and "
                   f"choppy."),
            ask="tell him to get some air to you or take you out of the case.",
        ))
    disk = world.vitals.get("disk_pct")
    if disk is not None and disk >= cfg.disk_warn_pct:
        out.append(Nudge(
            kind="device",
            key=f"disk:{world.today}",
            priority=10,
            facts=(f"Your storage card is {disk:.0f}% full. Past ~95% you stop "
                   f"being able to save memories, reminders and logs at all."),
            ask="tell him it needs clearing out soon.",
        ))
    return out


def quiet_check_in(world: World, cfg: AmbientConfig) -> list[Nudge]:
    """A long silence, but only spoken with something concrete to hang it on.

    This is the signal most likely to turn her into a nag, so it is the
    strictest: no upcoming event and no pending reminder means she says
    nothing at all. A check-in with no hook is exactly the call-centre
    "sab theek?" the persona forbids.
    """
    if world.idle_seconds < cfg.idle_hours_before_checkin * 3600:
        return []
    hooks: list[str] = []
    for event in world.events_within(cfg.checkin_horizon_hours * 3600):
        hooks.append(f"'{event.summary}' at {_clock(event)}")
        break
    for reminder in world.reminders:
        due = reminder.get("due", 0)
        if world.now < due <= world.now + cfg.checkin_horizon_hours * 3600:
            hooks.append(f"the reminder '{reminder.get('text', '')}' at "
                         f"{time.strftime('%I:%M %p', time.localtime(due)).lstrip('0')}")
            break
    if not hooks:
        return []       # nothing specific to say — stay quiet, on purpose
    hours = int(world.idle_seconds // 3600)
    return [Nudge(
        kind="check_in",
        key=f"checkin:{world.today}:{hours}",
        priority=90,
        facts=(f"He hasn't talked to you in {hours} hours. Coming up: "
               f"{' and '.join(hooks)}."),
        ask="mention it and leave it there.",
    )]


SIGNALS: tuple[Callable[[World, AmbientConfig], list[Nudge]], ...] = (
    device_health,
    rain_before_event,
    early_start_tomorrow,
    mail_pileup,
    quiet_check_in,
)


def judge(world: World, cfg: AmbientConfig,
          signals: Sequence = SIGNALS) -> list[Nudge]:
    """Every nudge the world currently justifies, most urgent first.

    A broken signal must never take the loop down with it — the whole
    feature is optional garnish on a working assistant.
    """
    out: list[Nudge] = []
    for signal in signals:
        try:
            out.extend(signal(world, cfg))
        except Exception:
            log.exception("ambient signal %s failed", getattr(signal, "__name__", signal))
    return sorted(out, key=lambda n: n.priority)


# ── the gate: whether to speak at all ────────────────────────────────────────
def in_quiet_hours(cfg: AmbientConfig, now: float | None = None) -> bool:
    """The hours she stays silent unless spoken to.

    Module-level because it isn't really an ambient rule — it's the house
    rule for *any* unprompted speech, and `watch.py` holds its results to it
    too rather than keeping a second copy of the wrap-around arithmetic.
    """
    hour = time.localtime(time.time() if now is None else now).tm_hour
    start, end = cfg.quiet_start_hour, cfg.quiet_end_hour
    if start == end:
        return False
    if start < end:                 # e.g. 01:00 -> 07:00
        return start <= hour < end
    return hour >= start or hour < end   # the usual 23:00 -> 07:00 wrap


class AmbientState(_JsonStore):
    """What she has already brought up, on disk.

    Persistent because the alternative is a reboot loop re-announcing the
    same warning forever — the failure mode that makes people turn a
    proactive assistant off and never turn it back on.
    """

    MAX_KEYS = 200

    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        super().__init__(path)
        self._clock = clock

    def _data(self) -> dict:
        data = self._load({})
        return data if isinstance(data, dict) else {}

    def seen(self, key: str) -> bool:
        return key in self._data().get("spoken", {})

    def last_spoken(self) -> float:
        return float(self._data().get("last", 0.0) or 0.0)

    def kind_last(self, kind: str) -> float:
        return float(self._data().get("kinds", {}).get(kind, 0.0) or 0.0)

    def count_today(self, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        data = self._data()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        return int(data.get("count", 0)) if data.get("day") == today else 0

    def record(self, nudge: Nudge, now: float | None = None) -> None:
        now = self._clock() if now is None else now
        with self._lock:
            data = self._data()
            today = time.strftime("%Y-%m-%d", time.localtime(now))
            spoken = data.get("spoken", {})
            spoken[nudge.key] = now
            if len(spoken) > self.MAX_KEYS:  # keep the newest, forget the rest
                spoken = dict(sorted(spoken.items(), key=lambda kv: kv[1])
                              [-self.MAX_KEYS:])
            kinds = data.get("kinds", {})
            kinds[nudge.kind] = now
            try:
                self._save({
                    "spoken": spoken,
                    "kinds": kinds,
                    "last": now,
                    "day": today,
                    "count": (data.get("count", 0) + 1
                              if data.get("day") == today else 1),
                })
            except OSError:
                log.warning("could not persist ambient state")


class AmbientGate:
    """The stinginess. Every rule here exists to buy the right to speak."""

    def __init__(self, cfg: AmbientConfig, state: AmbientState,
                 clock: Callable[[], float] = time.time):
        self._cfg = cfg
        self._state = state
        self._clock = clock

    def in_quiet_hours(self, now: float | None = None) -> bool:
        return in_quiet_hours(self._cfg, self._clock() if now is None else now)

    def choose(self, nudges: Sequence[Nudge],
               now: float | None = None) -> Nudge | None:
        """The highest-priority nudge she's earned the right to speak, if any."""
        now = self._clock() if now is None else now
        cfg = self._cfg
        if self.in_quiet_hours(now):
            return None
        if self._state.count_today(now) >= cfg.max_per_day:
            return None
        if now - self._state.last_spoken() < cfg.min_gap_minutes * 60:
            return None
        for nudge in nudges:
            if self._state.seen(nudge.key):
                continue        # this exact fact has already been raised
            if now - self._state.kind_last(nudge.kind) < cfg.kind_cooldown_minutes * 60:
                continue        # this *kind* of thing was raised recently
            return nudge
        return None


# ── the loop ─────────────────────────────────────────────────────────────────
class AmbientLoop:
    """Ties it together and hands the winning nudge to the voice loop.

    Sensing is blocking (network, /proc), so gather() runs off the event
    loop. Speaking is not done here: ``speak`` queues the instruction and
    the wake loop starts a session with it, exactly as an incoming WhatsApp
    does — so there is only one path by which Venom ever opens its mouth
    unprompted.
    """

    def __init__(self, config, state_path: Path, speak: Callable[[str], None],
                 is_busy: Callable[[], bool], *, session=None, calendar=None,
                 mailbox=None, memory=None, location=None, reminders=None,
                 clock: Callable[[], float] = time.time):
        self.config = config
        self.cfg: AmbientConfig = config.ambient
        self.state = AmbientState(state_path, clock=clock)
        self.gate = AmbientGate(self.cfg, self.state, clock=clock)
        self._speak = speak
        self._is_busy = is_busy
        self._session = session          # SessionState: last interaction time
        self._calendar = calendar        # CalendarWatcher
        self._mailbox = mailbox          # Mailbox
        self._memory = memory            # MemoryStore (home city)
        self._location = location        # LocationProvider
        self._reminders = reminders      # ReminderStore
        self._clock = clock
        # Weather is the one signal input that costs a network round trip;
        # the tick is already slow, but a stale-but-cheap read is plenty for
        # "is it raining" and keeps the radio quiet.
        self._weather = ""
        self._weather_at = 0.0

    # ── sensing ──────────────────────────────────────────────────────────────
    def _fetch_weather(self) -> str:
        now = self._clock()
        if self._weather and now - self._weather_at < self.cfg.weather_cache_minutes * 60:
            return self._weather
        from venom.tools_pi import fetch_weather, home_city

        city = ""
        if self._location is not None:
            city = (self._location.cached() or {}).get("city") or ""
        if not city and self._memory is not None:
            city = home_city(self._memory)
        if not city:
            return ""
        try:
            self._weather = fetch_weather(city)
            self._weather_at = now
        except Exception as exc:
            log.debug("ambient weather fetch failed: %s", exc)
        return self._weather

    def gather(self) -> World:
        """Build the snapshot. Blocking — call via asyncio.to_thread.

        Every source is optional and every failure is swallowed: a dead
        calendar feed must degrade the ambient loop, not disable it.
        """
        now = self._clock()
        idle = now
        if self._session is not None:
            last = self._session.last_interaction()
            idle = now - last if last else self.cfg.idle_hours_before_checkin * 3600

        events: tuple = ()
        if self._calendar is not None:
            try:
                events = tuple(e for e in self._calendar.feed.events()
                               if e.start.timestamp() > now)
            except Exception as exc:
                log.debug("ambient calendar read failed: %s", exc)

        unread = -1
        if self._mailbox is not None and self.cfg.mail_pileup > 0:
            try:
                unread = self._mailbox.unread_count()
            except Exception as exc:
                log.debug("ambient mail read failed: %s", exc)

        reminders: tuple = ()
        if self._reminders is not None:
            try:
                reminders = tuple(self._reminders.pending())
            except Exception as exc:
                log.debug("ambient reminder read failed: %s", exc)

        try:
            from venom.tools_pi import device_metrics

            vitals = device_metrics()
        except Exception:
            vitals = {}

        return World(now=now, idle_seconds=idle, events=events,
                     weather=self._fetch_weather(), unread=unread,
                     reminders=reminders, vitals=vitals)

    # ── one pass ─────────────────────────────────────────────────────────────
    async def tick(self) -> Nudge | None:
        """Sense, judge, gate, and (maybe) queue. Returns what was spoken."""
        if self._is_busy():
            return None
        world = await asyncio.to_thread(self.gather)
        nudge = self.gate.choose(judge(world, self.cfg))
        if nudge is None:
            return None
        # Busy again? The gather took real time, and interrupting a
        # conversation that started meanwhile is exactly what she must not do.
        if self._is_busy():
            return None
        self.state.record(nudge, world.now)
        log.info("ambient nudge: %s (%s)", nudge.kind, nudge.key)
        self._speak(nudge.instruction)
        return nudge

    async def run(self) -> None:
        # Don't nudge the instant the daemon boots: a Pi that just came up
        # has a cold calendar, no location and no idea how long he's been
        # quiet. Let the world settle first.
        await asyncio.sleep(self.cfg.warmup_seconds)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ambient tick failed")
            await asyncio.sleep(self.cfg.tick_seconds)
