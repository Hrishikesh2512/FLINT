"""Persistent reminders, notes, and lists — CRUD, persistence across reloads,
wall-clock firing, reminder-time parsing, and registry wiring."""

import time

import pytest

from venom.config import VenomConfig
from venom.stores import ListStore, NoteStore, ReminderStore
from venom.tools_pi import TimerBoard, build_pi_registry, parse_reminder_time


# ── ReminderStore ─────────────────────────────────────────────────────────────
def test_reminder_add_pending_and_persist(tmp_path):
    clk = [1000.0]
    r = ReminderStore(tmp_path / "rem.json", clock=lambda: clk[0])
    r.add("call mom", 2000.0)
    r.add("gym", 1500.0)
    pending = r.pending()
    assert [p["text"] for p in pending] == ["gym", "call mom"]  # sorted by due
    # survives a fresh instance (i.e. a reboot)
    r2 = ReminderStore(tmp_path / "rem.json", clock=lambda: clk[0])
    assert len(r2.pending()) == 2


def test_reminder_pop_due_fires_by_wall_clock(tmp_path):
    clk = [1000.0]
    r = ReminderStore(tmp_path / "rem.json", clock=lambda: clk[0])
    r.add("soon", 1200.0)
    r.add("later", 5000.0)
    assert r.pop_due() == []          # nothing due yet
    clk[0] = 1300.0
    due = r.pop_due()
    assert [d["text"] for d in due] == ["soon"]
    assert r.pop_due() == []          # removed after firing (persisted)
    assert [p["text"] for p in r.pending()] == ["later"]


def test_reminder_cancel(tmp_path):
    r = ReminderStore(tmp_path / "rem.json")
    r.add("call the dentist", time.time() + 3600)
    r.add("buy milk", time.time() + 3600)
    assert r.cancel("dentist") == 1
    assert [p["text"] for p in r.pending()] == ["buy milk"]
    assert r.cancel("nothing") == 0


# ── NoteStore ─────────────────────────────────────────────────────────────────
def test_notes_add_read_clear_persist(tmp_path):
    n = NoteStore(tmp_path / "notes.json")
    n.add("idea one")
    n.add("idea two")
    assert [x["text"] for x in NoteStore(tmp_path / "notes.json").all()] == \
        ["idea one", "idea two"]
    assert n.clear() == 2
    assert n.all() == []


# ── ListStore ─────────────────────────────────────────────────────────────────
def test_list_add_dedupe_remove_show(tmp_path):
    lst = ListStore(tmp_path / "lists.json")
    assert "added milk" in lst.add_item("milk")           # default = shopping
    assert "already" in lst.add_item("MILK")              # case-insensitive dedupe
    lst.add_item("eggs")
    assert lst.show("shopping") == ["milk", "eggs"]
    assert "removed" in lst.remove_item("milk")
    assert lst.show() == ["eggs"]


def test_list_named_and_persist(tmp_path):
    lst = ListStore(tmp_path / "lists.json")
    lst.add_item("torch", "packing")
    lst.add_item("finish report", "todo")
    fresh = ListStore(tmp_path / "lists.json")
    assert fresh.show("packing") == ["torch"]
    assert set(fresh.names()) == {"packing", "todo"}
    assert fresh.clear("packing") == 1
    assert fresh.names() == ["todo"]


# ── reminder time parsing ─────────────────────────────────────────────────────
def test_parse_minutes_from_now():
    due, phrase = parse_reminder_time(minutes_from_now=30, now=1000.0)
    assert due == 1000.0 + 1800
    assert "30" in phrase


def test_parse_absolute_at_time():
    now = time.time()
    future = time.strftime("%Y-%m-%d %H:%M", time.localtime(now + 3600))
    due, _ = parse_reminder_time(at_time=future, now=now)
    assert due > now


def test_parse_time_only_rolls_to_tomorrow_if_past():
    now = time.time()
    past_clock = time.strftime("%H:%M", time.localtime(now - 3600))
    due, _ = parse_reminder_time(at_time=past_clock, now=now)
    assert due > now  # pushed to tomorrow


def test_parse_rejects_past_and_empty():
    with pytest.raises(ValueError):
        parse_reminder_time(minutes_from_now=-5)
    with pytest.raises(ValueError):
        parse_reminder_time(at_time="2000-01-01 00:00", now=time.time())
    with pytest.raises(ValueError):
        parse_reminder_time()


# ── registry wiring ───────────────────────────────────────────────────────────
def _registry(tmp_path):
    return build_pi_registry(
        VenomConfig(), memory=_DummyMem(), timers=TimerBoard(),
        reminders=ReminderStore(tmp_path / "rem.json"),
        notes=NoteStore(tmp_path / "notes.json"),
        lists=ListStore(tmp_path / "lists.json"))


class _DummyMem:
    def load(self):
        return {}

    def render_for_prompt(self):
        return ""

    def remember(self, *a):
        return "ok"


def test_registry_exposes_new_tools_and_dispatches(tmp_path):
    reg = _registry(tmp_path)
    for name in ("set_reminder", "list_reminders", "cancel_reminder",
                 "add_note", "read_notes", "clear_notes",
                 "add_to_list", "remove_from_list", "show_list", "clear_list"):
        assert name in reg

    assert "Noted" in reg.dispatch("add_note", {"text": "buy stamps"})
    assert "buy stamps" in reg.dispatch("read_notes", {})
    assert "milk" in reg.dispatch("add_to_list", {"item": "milk"})
    assert "milk" in reg.dispatch("show_list", {})
    assert "Reminder set" in reg.dispatch(
        "set_reminder", {"text": "standup", "minutes_from_now": 10})


def test_tools_absent_without_stores():
    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(),
                            timers=TimerBoard())
    assert "set_reminder" not in reg
    assert "add_note" not in reg
    assert "add_to_list" not in reg
