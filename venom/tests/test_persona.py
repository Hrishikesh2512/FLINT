"""Persona behaviours: time-of-day tone, morning-briefing gating, the
voice power-off tool, and briefing content."""

import time

from venom.config import VenomConfig
from venom.live import build_system_instruction, tone_for_time
from venom.session import SessionState
from venom.stores import ReminderStore
from venom.tools_pi import TimerBoard, build_briefing, build_pi_registry


def _at(hour: int) -> float:
    lt = list(time.localtime())
    lt[3], lt[4], lt[5] = hour, 0, 0
    return time.mktime(time.struct_time(tuple(lt)))


# ── time-of-day tone ──────────────────────────────────────────────────────────
def test_tone_shifts_across_the_day():
    assert "Morning" in tone_for_time(_at(8))
    assert "Midday" in tone_for_time(_at(14))
    assert "Evening" in tone_for_time(_at(19))
    night = tone_for_time(_at(23))
    assert "Late night" in night and "loving" in night


def test_system_prompt_carries_name_and_tone():
    cfg = VenomConfig()
    prompt = build_system_instruction(cfg, _DummyMem())
    assert "Hinglish" in prompt
    assert "RIGHT NOW" in prompt          # tone block present
    assert cfg.voice.user_name in prompt  # addresses by name


class _DummyMem:
    def render_for_prompt(self):
        return ""

    def load(self):
        return {}


# ── morning-briefing gating ───────────────────────────────────────────────────
def test_should_brief_only_after_730_and_a_gap(tmp_path):
    s = SessionState(tmp_path / "sess.json")
    # fresh device, 8am → brief
    assert s.should_brief(_at(8)) is True
    # before 7:30 → never
    assert s.should_brief(_at(6)) is False


def test_should_brief_not_twice_same_morning(tmp_path):
    s = SessionState(tmp_path / "sess.json")
    now = _at(8)
    assert s.should_brief(now) is True
    s.mark_briefed(now)
    assert s.should_brief(now + 600) is False   # already briefed today


def test_should_brief_suppressed_if_used_recently(tmp_path):
    s = SessionState(tmp_path / "sess.json")
    s.mark_interaction(_at(8))               # talked at 8:00
    assert s.should_brief(_at(9)) is False   # only 1h gap, not a fresh morning


# ── voice power-off tool ──────────────────────────────────────────────────────
def test_power_off_writes_control_request(tmp_path, monkeypatch):
    req = tmp_path / "control.request"
    import venom.tools_pi as tp
    monkeypatch.setattr(tp, "CONTROL_REQUEST", req)
    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(), timers=TimerBoard())
    assert "power_off" in reg
    out = reg.dispatch("power_off", {})
    assert req.read_text() == "poweroff"
    assert "Good night" in out or "off" in out.lower()


# ── briefing content ──────────────────────────────────────────────────────────
class _FakeLoc:
    def get(self):
        return {"city": "Pune"}


def test_briefing_uses_location_and_reminders(tmp_path, monkeypatch):
    import venom.tools_pi as tp
    monkeypatch.setattr(tp, "fetch_weather", lambda city, **kw: f"sunny in {city}")
    rem = ReminderStore(tmp_path / "r.json")
    rem.add("dentist", time.time() + 3600)
    text = build_briefing(_DummyMem(), TimerBoard(),
                          location=_FakeLoc(), reminders=rem)
    assert "Pune" in text
    assert "dentist" in text
    assert "Hinglish" in text  # delivery instruction
