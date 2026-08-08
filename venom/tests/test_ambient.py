"""Ambient awareness: the signals, the gate, and the loop that joins them.

Everything here runs without a Pi, a calendar, or a network — the whole
point of splitting sense/judge/gate is that judging is a pure function.
"""

import asyncio
import datetime as dt
import time

import pytest

from venom.ambient import (
    AmbientGate,
    AmbientLoop,
    AmbientState,
    Nudge,
    World,
    device_health,
    early_start_tomorrow,
    judge,
    mail_pileup,
    quiet_check_in,
    rain_before_event,
)
from venom.config import AmbientConfig, VenomConfig, load_config
from venom.gcal import Event

CFG = AmbientConfig()


def at(hour: int, minute: int = 0, day_offset: int = 0) -> float:
    """Epoch for a local wall-clock time today (+/- whole days)."""
    today = dt.date.today() + dt.timedelta(days=day_offset)
    return time.mktime(dt.datetime.combine(
        today, dt.time(hour, minute)).timetuple())


def event(hour: int, summary: str = "Seminar", day_offset: int = 0) -> Event:
    start = dt.datetime.combine(
        dt.date.today() + dt.timedelta(days=day_offset),
        dt.time(hour)).astimezone()
    return Event(start, start + dt.timedelta(hours=1), summary)


# ── signals ──────────────────────────────────────────────────────────────────
def test_rain_before_event_fires_only_with_both_facts():
    wet = "In Bengaluru: rain showers, 22°C."
    soon = World(now=at(13), idle_seconds=0, weather=wet, events=(event(15),))
    (nudge,) = rain_before_event(soon, CFG)
    assert nudge.kind == "weather"
    assert "umbrella" in nudge.ask

    # Rain, but nowhere to be — not worth interrupting him.
    assert rain_before_event(
        World(now=at(13), idle_seconds=0, weather=wet), CFG) == []
    # Somewhere to be, but clear skies.
    assert rain_before_event(
        World(now=at(13), idle_seconds=0, events=(event(15),),
              weather="In Bengaluru: clear sky, 28°C."), CFG) == []
    # An event beyond the horizon isn't "heading out into this".
    assert rain_before_event(
        World(now=at(8), idle_seconds=0, weather=wet, events=(event(20),)),
        CFG) == []


def test_early_start_tomorrow_only_in_the_evening_window():
    early = event(8, "Flight", day_offset=1)
    tonight = World(now=at(21), idle_seconds=0, events=(early,))
    (nudge,) = early_start_tomorrow(tonight, CFG)
    assert nudge.kind == "day_ahead"
    assert "Flight" in nudge.facts

    # Same fact at lunchtime is just noise — he can't act on it yet.
    assert early_start_tomorrow(
        World(now=at(13), idle_seconds=0, events=(early,)), CFG) == []
    # A civilised 11am start needs no warning.
    assert early_start_tomorrow(
        World(now=at(21), idle_seconds=0,
              events=(event(11, day_offset=1),)), CFG) == []
    # Only the *first* event of tomorrow is judged, not a late one after it.
    assert early_start_tomorrow(
        World(now=at(21), idle_seconds=0,
              events=(event(11, day_offset=1), event(8, day_offset=2))),
        CFG) == []


def test_mail_pileup_needs_a_pile_and_a_long_silence():
    piled = World(now=at(15), idle_seconds=4 * 3600, unread=9)
    (nudge,) = mail_pileup(piled, CFG)
    assert "9 unread" in nudge.facts

    assert mail_pileup(World(now=at(15), idle_seconds=4 * 3600, unread=2), CFG) == []
    assert mail_pileup(World(now=at(15), idle_seconds=60, unread=9), CFG) == []
    # Unknown (mail unreachable) must never read as a pile.
    assert mail_pileup(World(now=at(15), idle_seconds=4 * 3600, unread=-1), CFG) == []


def test_mail_key_only_changes_when_the_pile_grows_a_full_step():
    def key(unread):
        world = World(now=at(15), idle_seconds=4 * 3600, unread=unread)
        return mail_pileup(world, CFG)[0].key

    assert key(5) == key(9)      # same step — she won't raise it twice
    assert key(5) != key(10)     # doubled — worth mentioning again


def test_device_health_warns_about_her_own_body():
    hot = World(now=at(15), idle_seconds=0,
                vitals={"temp_c": 84.0, "disk_pct": 40.0})
    (nudge,) = device_health(hot, CFG)
    assert nudge.priority == 10          # outranks everything else
    assert "84°C" in nudge.facts

    full = World(now=at(15), idle_seconds=0, vitals={"disk_pct": 95.0})
    assert device_health(full, CFG)[0].key.startswith("disk:")
    # A dev box reads no metrics at all — silence, not a false alarm.
    assert device_health(World(now=at(15), idle_seconds=0), CFG) == []


def test_quiet_check_in_stays_silent_without_a_concrete_hook():
    lonely = World(now=at(15), idle_seconds=6 * 3600)
    assert quiet_check_in(lonely, CFG) == []      # the anti-nag rule

    with_event = World(now=at(15), idle_seconds=6 * 3600, events=(event(17),))
    (nudge,) = quiet_check_in(with_event, CFG)
    assert "Seminar" in nudge.facts and nudge.priority == 90

    with_reminder = World(now=at(15), idle_seconds=6 * 3600,
                          reminders=({"text": "call mom", "due": at(17)},))
    assert "call mom" in quiet_check_in(with_reminder, CFG)[0].facts


def test_judge_orders_by_urgency_and_survives_a_broken_signal():
    def exploding(world, cfg):
        raise RuntimeError("sensor on fire")

    world = World(now=at(15), idle_seconds=6 * 3600, unread=9,
                  events=(event(17),), vitals={"temp_c": 90.0})
    nudges = judge(world, CFG, signals=(exploding, mail_pileup, quiet_check_in,
                                        device_health))
    assert [n.kind for n in nudges] == ["device", "mail", "check_in"]


# ── the gate ─────────────────────────────────────────────────────────────────
@pytest.fixture
def gate(tmp_path):
    state = AmbientState(tmp_path / "ambient.json")
    return AmbientGate(CFG, state), state


def nudge(kind="mail", key="k1", priority=50):
    return Nudge(kind=kind, key=key, priority=priority, facts="something.")


def test_gate_speaks_then_holds_its_tongue(gate):
    g, state = gate
    now = at(15)
    picked = g.choose([nudge()], now=now)
    assert picked is not None
    state.record(picked, now)

    # Same fact, ever again: no.
    assert g.choose([nudge()], now=now + 10 * 3600) is None
    # Different fact, same minute: too soon after the last one.
    assert g.choose([nudge(key="k2")], now=now + 60) is None
    # Different fact, past the gap — but the *kind* is still cooling down.
    assert g.choose([nudge(key="k2")], now=now + 60 * 60) is None
    # Past the kind cooldown too: allowed.
    assert g.choose([nudge(key="k2")], now=now + 4 * 3600) is not None


def test_gate_is_silent_in_quiet_hours(gate):
    g, _ = gate
    assert g.choose([nudge()], now=at(2)) is None      # 02:00
    assert g.choose([nudge()], now=at(6, 59)) is None
    assert g.choose([nudge()], now=at(9)) is not None


def test_quiet_hours_can_be_configured_without_a_midnight_wrap(tmp_path):
    cfg = AmbientConfig(quiet_start_hour=1, quiet_end_hour=7)
    g = AmbientGate(cfg, AmbientState(tmp_path / "s.json"))
    assert g.in_quiet_hours(at(3)) is True
    assert g.in_quiet_hours(at(23)) is False


def test_gate_respects_the_daily_cap(tmp_path):
    cfg = AmbientConfig(max_per_day=2, min_gap_minutes=0,
                        kind_cooldown_minutes=0)
    state = AmbientState(tmp_path / "ambient.json")
    g = AmbientGate(cfg, state)
    now = at(9)
    for i in range(2):
        picked = g.choose([nudge(key=f"k{i}")], now=now)
        assert picked is not None
        state.record(picked, now)
    assert g.choose([nudge(key="k9")], now=now) is None
    # A new day resets the budget.
    assert g.choose([nudge(key="k9")], now=at(9, day_offset=1)) is not None


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "ambient.json"
    AmbientState(path).record(nudge(key="already-said"), at(15))
    assert AmbientState(path).seen("already-said")     # a fresh object, reboot
    assert not AmbientState(path).seen("never-said")


def test_state_prunes_but_keeps_the_newest(tmp_path):
    state = AmbientState(tmp_path / "ambient.json")
    for i in range(AmbientState.MAX_KEYS + 20):
        state.record(nudge(key=f"k{i}"), at(9) + i)
    assert state.seen(f"k{AmbientState.MAX_KEYS + 19}")
    assert not state.seen("k0")


# ── the loop ─────────────────────────────────────────────────────────────────
class FakeSession:
    def __init__(self, last):
        self._last = last

    def last_interaction(self):
        return self._last


def build_loop(tmp_path, *, busy=False, now=None, **kwargs):
    now = now or at(15)
    spoken = []
    config = VenomConfig(memory_path=tmp_path / "memory.json")
    loop = AmbientLoop(config, tmp_path / "ambient.json",
                       speak=spoken.append, is_busy=lambda: busy,
                       session=FakeSession(now - 6 * 3600),
                       clock=lambda: now, **kwargs)
    loop._weather, loop._weather_at = "clear sky", now   # no network in tests
    return loop, spoken


def test_loop_queues_the_opening_for_the_wake_loop(tmp_path):
    loop, spoken = build_loop(tmp_path)
    loop.gather = lambda: World(now=at(15), idle_seconds=6 * 3600,
                                vitals={"temp_c": 88.0})
    picked = asyncio.run(loop.tick())
    assert picked.kind == "device"
    assert len(spoken) == 1
    assert spoken[0].startswith("[Proactive]")
    assert "88°C" in spoken[0]
    # And she never says it twice, even on the very next tick.
    assert asyncio.run(loop.tick()) is None
    assert len(spoken) == 1


def test_loop_says_nothing_while_busy(tmp_path):
    loop, spoken = build_loop(tmp_path, busy=True)
    loop.gather = lambda: World(now=at(15), idle_seconds=6 * 3600,
                                vitals={"temp_c": 88.0})
    assert asyncio.run(loop.tick()) is None
    assert spoken == []


def test_loop_stays_quiet_when_the_world_is_uneventful(tmp_path):
    loop, spoken = build_loop(tmp_path)
    loop.gather = lambda: World(now=at(15), idle_seconds=60, unread=0)
    assert asyncio.run(loop.tick()) is None
    assert spoken == []


def test_gather_degrades_instead_of_failing(tmp_path):
    class DeadCalendar:
        @property
        def feed(self):
            raise OSError("feed unreachable")

    class DeadMailbox:
        def unread_count(self):
            raise OSError("imap down")

    loop, _ = build_loop(tmp_path, calendar=DeadCalendar(),
                         mailbox=DeadMailbox())
    world = loop.gather()
    assert world.events == () and world.unread == -1
    assert world.idle_seconds == pytest.approx(6 * 3600)


# ── config ───────────────────────────────────────────────────────────────────
def test_ambient_config_defaults_and_overrides(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text("[ambient]\nenabled = false\ntick_seconds = 60\n"
                    "quiet_start_hour = 22\n", encoding="utf-8")
    cfg = load_config(path).ambient
    assert cfg.enabled is False
    assert cfg.tick_seconds == 60.0 and isinstance(cfg.tick_seconds, float)
    assert cfg.quiet_start_hour == 22
    assert cfg.max_per_day == AmbientConfig().max_per_day   # untouched default


def test_ambient_config_rejects_nonsense():
    with pytest.raises(ValueError):
        AmbientConfig(tick_seconds=0)
    with pytest.raises(ValueError):
        AmbientConfig(quiet_start_hour=25)
