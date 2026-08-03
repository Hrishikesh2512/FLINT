"""Delegated watches — a fake provider returns canned search text and canned
verdicts, so the store's budget rules, the fire/hold/deliver path and the
quiet-hours behaviour are all exercised with no network and no API key."""

import asyncio
import json

import pytest

from venom.ambient import in_quiet_hours
from venom.config import AmbientConfig, WatchConfig, load_config
from venom.watch import WatchLoop, WatchStore, check_watch, watch_instruction


class FakeProvider:
    """Stands in for GeminiProvider: canned search text + canned verdicts."""

    models = ("fake-model",)

    def __init__(self, verdicts=None, facts="Score is 100/2.",
                 search_fails=False, verdict_raw=None):
        self.verdicts = list(verdicts or [])
        self.facts = facts
        self.search_fails = search_fails
        self.verdict_raw = verdict_raw
        self.searches: list[str] = []
        self.completions: list = []

    def grounded_search(self, query, **kw):
        self.searches.append(query)
        if self.search_fails:
            raise OSError("no network")
        return self.facts

    def complete(self, messages, model, **kw):
        self.completions.append((messages, model, kw))
        if self.verdict_raw is not None:
            return self.verdict_raw
        if not self.verdicts:
            return json.dumps({"met": False, "observation": "nothing yet", "say": ""})
        return json.dumps(self.verdicts.pop(0))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def store(tmp_path, clock=None):
    return WatchStore(tmp_path / "watches.json", clock=clock or Clock())


# ── the store's budget rules ─────────────────────────────────────────────────
def test_add_and_list(tmp_path):
    s = store(tmp_path)
    s.add("India vs Australia score", "India needs under 20")
    assert len(s.active()) == 1
    assert "India vs Australia" in s.summary()
    assert "until India needs under 20" in s.summary()


def test_watch_needs_something_to_watch(tmp_path):
    with pytest.raises(ValueError, match="needs something"):
        store(tmp_path).add("   ")


def test_interval_is_floored_so_checks_cant_run_away(tmp_path):
    s = store(tmp_path)
    entry = s.add("something", interval=5)     # 5s would bill all day
    assert entry["interval"] == WatchStore.MIN_INTERVAL


def test_only_so_many_watches_at_once(tmp_path):
    s = store(tmp_path)
    for i in range(WatchStore.MAX_ACTIVE):
        s.add(f"thing {i}")
    with pytest.raises(ValueError, match="that's the limit"):
        s.add("one too many")


def test_due_respects_each_watchs_interval(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    w = s.add("thing", interval=600)
    assert s.due()                     # never checked → due immediately
    s.record_check(w["id"], "seen it")
    assert not s.due()
    clock.advance(599)
    assert not s.due()
    clock.advance(2)
    assert s.due()


def test_a_watch_that_never_fires_expires(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    s.add("thing", ttl_hours=1)
    assert s.active()
    clock.advance(3601)
    assert not s.active()
    assert s.prune() == 1


def test_a_watch_runs_out_of_checks(tmp_path):
    clock = Clock()
    s = store(tmp_path, clock)
    w = s.add("thing", interval=WatchStore.MIN_INTERVAL)
    for _ in range(WatchStore.MAX_CHECKS):
        s.record_check(w["id"], "still nothing")
    assert not s.active()


def test_cancel_by_words_and_cancel_all(tmp_path):
    s = store(tmp_path)
    s.add("the cricket match")
    s.add("the exam result")
    assert s.cancel("cricket") == 1
    assert len(s.active()) == 1
    assert s.cancel() == 1
    assert s.active() == []


def test_summary_when_idle(tmp_path):
    assert "not watching anything" in store(tmp_path).summary()


# ── the judge ────────────────────────────────────────────────────────────────
def test_check_reports_met_with_a_line_to_say(tmp_path):
    p = FakeProvider([{"met": True, "observation": "India need 18",
                       "say": "India need 18 off 12"}])
    verdict = check_watch(p, {"id": "x", "what": "score", "condition": "under 20"})
    assert verdict["met"]
    assert verdict["say"] == "India need 18 off 12"
    assert p.searches and "score" in p.searches[0]


def test_judge_gets_the_previous_observation(tmp_path):
    p = FakeProvider()
    check_watch(p, {"id": "x", "what": "score", "condition": "",
                    "observation": "was 100/2"})
    prompt = p.completions[0][0][1].content
    assert "was 100/2" in prompt
    assert "FACTS FROM THE WEB" in prompt


def test_judge_runs_at_zero_temperature(tmp_path):
    p = FakeProvider()
    check_watch(p, {"id": "x", "what": "score"})
    assert p.completions[0][2]["temperature"] == 0.0
    assert p.completions[0][2]["json_mode"] is True


def test_met_without_an_observation_is_not_a_fire(tmp_path):
    # A verdict that can't name what it saw is exactly the hallucinated fire
    # the two-call split exists to prevent.
    p = FakeProvider([{"met": True, "observation": "", "say": "it happened!"}])
    assert check_watch(p, {"id": "x", "what": "score"})["met"] is False


def test_met_without_a_line_falls_back_to_the_observation(tmp_path):
    p = FakeProvider([{"met": True, "observation": "India won", "say": ""}])
    assert check_watch(p, {"id": "x", "what": "score"})["say"] == "India won"


def test_a_failed_search_is_not_a_verdict(tmp_path):
    assert check_watch(FakeProvider(search_fails=True), {"id": "x", "what": "s"}) is None


def test_unparseable_verdict_is_not_a_verdict(tmp_path):
    assert check_watch(FakeProvider(verdict_raw="not json"), {"id": "x", "what": "s"}) is None


# ── the loop ─────────────────────────────────────────────────────────────────
def run(coro):
    return asyncio.run(coro)


def loop(tmp_path, provider, *, busy=False, quiet=False, clock=None):
    clock = clock or Clock()
    s = store(tmp_path, clock)
    spoken: list[str] = []
    lp = WatchLoop(s, provider_factory=lambda: provider,
                   speak=spoken.append, is_busy=lambda: busy,
                   in_quiet_hours=lambda: quiet, clock=clock)
    return s, lp, spoken


def test_a_fired_watch_speaks_and_is_done(tmp_path):
    p = FakeProvider([{"met": True, "observation": "India won", "say": "India won"}])
    s, lp, spoken = loop(tmp_path, p)
    s.add("the match", "India wins")
    run(lp.tick())
    assert len(spoken) == 1
    assert "[Proactive]" in spoken[0]
    assert "India won" in spoken[0]
    assert s.all() == []          # spoken once, then gone


def test_an_unmet_watch_stays_and_remembers_what_it_saw(tmp_path):
    p = FakeProvider([{"met": False, "observation": "India need 60", "say": ""}])
    s, lp, spoken = loop(tmp_path, p)
    s.add("the match", "India wins")
    run(lp.tick())
    assert spoken == []
    assert s.active()[0]["observation"] == "India need 60"
    assert s.active()[0]["checks"] == 1


def test_a_failed_check_does_not_burn_the_check_budget(tmp_path):
    s, lp, spoken = loop(tmp_path, FakeProvider(search_fails=True))
    s.add("the match")
    run(lp.tick())
    assert spoken == []
    assert s.all()[0]["checks"] == 0      # nothing was learned, nothing counted
    assert s.all()[0]["last_check"] > 0   # but it won't retry instantly


def test_nothing_is_checked_while_she_is_busy(tmp_path):
    p = FakeProvider()
    s, lp, spoken = loop(tmp_path, p, busy=True)
    s.add("the match")
    run(lp.tick())
    assert p.searches == []               # no API calls mid-conversation
    assert spoken == []


def test_a_result_in_quiet_hours_is_held_then_delivered(tmp_path):
    p = FakeProvider([{"met": True, "observation": "result out", "say": "result out"}])
    s, lp, spoken = loop(tmp_path, p, quiet=True)
    s.add("the result", "it is published")
    run(lp.tick())
    assert spoken == []                   # 3am — hold it
    assert len(s.held()) == 1

    _, awake, spoken2 = loop(tmp_path, p, quiet=False)
    run(awake.tick())
    assert len(spoken2) == 1              # morning — now she says it
    assert "result out" in spoken2[0]


def test_an_urgent_watch_speaks_through_quiet_hours(tmp_path):
    p = FakeProvider([{"met": True, "observation": "SOS", "say": "it happened"}])
    s, lp, spoken = loop(tmp_path, p, quiet=True)
    s.add("the thing", "it happens", urgent=True)
    run(lp.tick())
    assert len(spoken) == 1


def test_only_one_held_result_is_spoken_per_tick(tmp_path):
    s, lp, spoken = loop(tmp_path, FakeProvider())
    for i in range(3):
        w = s.add(f"thing {i}")
        s.mark_held(w["id"], f"result {i}", f"obs {i}")
    run(lp.tick())
    assert len(spoken) == 1               # one interruption at a time
    assert len(s.held()) == 2


def test_delivery_is_skipped_while_she_is_busy(tmp_path):
    s, lp, spoken = loop(tmp_path, FakeProvider(), busy=True)
    w = s.add("thing")
    s.mark_held(w["id"], "done", "obs")
    run(lp.tick())
    assert spoken == []
    assert len(s.held()) == 1             # still owed, delivered later


def test_a_held_result_outlives_its_expiry(tmp_path):
    # The budget stops *checking*; it must never swallow an answer already won.
    clock = Clock()
    s = store(tmp_path, clock)
    w = s.add("thing", ttl_hours=1)
    s.mark_held(w["id"], "the answer", "obs")
    clock.advance(7200)
    assert s.prune() == 0
    assert len(s.held()) == 1


def test_instruction_tells_her_to_remind_him_why(tmp_path):
    text = watch_instruction({"what": "the match", "say": "India won"})
    assert "the match" in text and "India won" in text
    assert "[Proactive]" in text


# ── quiet hours & config ─────────────────────────────────────────────────────
def test_quiet_hours_wrap_around_midnight():
    cfg = AmbientConfig(quiet_start_hour=23, quiet_end_hour=7)
    import time as _t

    def at(hour):
        lt = _t.localtime()
        return _t.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))

    assert in_quiet_hours(cfg, at(2))
    assert in_quiet_hours(cfg, at(23))
    assert not in_quiet_hours(cfg, at(12))


def test_watch_config_defaults_and_toml(tmp_path):
    assert WatchConfig().ready
    assert not WatchConfig(enabled=False).ready
    path = tmp_path / "venom.toml"
    path.write_text("[watch]\nenabled = false\ntick_seconds = 30\n", encoding="utf-8")
    cfg = load_config(path)
    assert cfg.watch.enabled is False
    assert cfg.watch.tick_seconds == 30.0
