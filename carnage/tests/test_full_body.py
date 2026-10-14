"""Everything the Pi does, on the phone: music, camera, SOS contacts, timers,
reminders, and handing the rest to the Pi."""

from __future__ import annotations

import json
import time

import pytest

from carnage.config import CarnageConfig, HubConfig
from carnage.platform import AbsentPhone, AndroidPhone, Battery, Fix
from carnage.runtime import Carnage
from carnage.sos import SosStore


class FullPhone:
    """A phone that can do everything, and remembers what it was asked."""

    name = "full"
    sends_directly = True

    def __init__(self):
        self.asked = []
        self.texts = []

    def available(self):
        return True

    def battery(self):
        return Battery(80)

    def locate(self):
        return Fix(18.5, 73.8, accuracy=10.0)

    def send_sms(self, to, text):
        self.texts.append((to, text))
        return True

    def notifications(self):
        return []

    def vibrate(self, milliseconds=400):
        return True

    def play_music(self, query):
        self.asked.append(("play", query))
        return "YouTube Music"

    def media(self, action):
        self.asked.append(("media", action))
        return True

    def volume(self, percent=None):
        self.asked.append(("volume", percent))
        return 40 if percent is None else percent

    def photo(self):
        return "anpeg"

    BOOK = [{"name": "Papa", "number": "+919800000001"},
            {"name": "Rahul Sharma", "number": "+919800000002"},
            {"name": "Rahul Verma", "number": "+919800000003"}]

    def find_contacts(self, query):
        q = query.lower()
        return [c for c in self.BOOK if q in c["name"].lower()]

    def place_call(self, number):
        self.asked.append(("call", number))
        return True

    alarm_ok = True

    def set_alarm(self, hour, minute, label=""):
        self.asked.append(("alarm", hour, minute))
        return self.alarm_ok

    def torch(self, on):
        self.asked.append(("torch", on))
        return True

    def open_app(self, name):
        return "Instagram" if "insta" in name.lower() else None

    def do_not_disturb(self, on):
        return True


@pytest.fixture()
def carnage(tmp_path):
    return Carnage(CarnageConfig(device="carnage", state_dir=tmp_path,
                                 gemini_api_key="k", hub=HubConfig(enabled=False)),
                   phone=FullPhone())


def tool(carnage, tool_name, **args):
    return carnage.registry.dispatch(tool_name, args)


# ── music ───────────────────────────────────────────────────────────────────
def test_music_is_played_and_steered_through_the_phone(carnage):
    assert "YouTube Music" in tool(carnage, "play_music", query="Kishore Kumar")
    assert tool(carnage, "music_control", action="pause") == "Paused."
    assert "40%" in tool(carnage, "set_volume")
    assert "70%" in tool(carnage, "set_volume", percent=70)
    assert ("play", "Kishore Kumar") in carnage.phone.asked
    assert ("media", "pause") in carnage.phone.asked


def test_an_unknown_media_action_never_reaches_the_phone(carnage):
    assert "only" in tool(carnage, "music_control", action="explode")
    assert ("media", "explode") not in carnage.phone.asked


def test_android_clamps_volume_and_filters_actions():
    calls = []
    phone = AndroidPhone(lambda method, **kw: calls.append((method, kw)) or 55)
    assert phone.volume(150) == 55
    assert calls[-1] == ("volume", {"percent": 100})
    assert phone.volume() == 55 and calls[-1] == ("volume", {"percent": -1})
    assert phone.media("explode") is False
    assert phone.media("next") is True


# ── camera ──────────────────────────────────────────────────────────────────
def test_the_camera_photo_goes_to_the_vision_call(carnage, monkeypatch):
    seen = {}

    def describe(prompt, jpeg, system):
        seen.update(prompt=prompt, jpeg=jpeg, system=system)
        return "A cup of chai on a desk."

    from carnage import tools_media

    monkeypatch.setattr(tools_media, "gemini_describer", lambda key: describe)
    fresh = Carnage(carnage.config, phone=FullPhone())
    assert tool(fresh, "look_around") == "A cup of chai on a desk."
    assert seen["jpeg"] == "anpeg"
    tool(fresh, "find_thing", thing="my keys")
    assert "my keys" in seen["prompt"] and "WHERE" in seen["system"]


def test_no_photo_is_an_honest_answer(tmp_path):
    class Blind(FullPhone):
        def photo(self):
            return None

    c = Carnage(CarnageConfig(state_dir=tmp_path, gemini_api_key="k",
                              hub=HubConfig(enabled=False)), phone=Blind())
    assert "couldn't get a picture" in tool(c, "look_around")


def test_no_phone_means_no_music_or_camera(tmp_path):
    c = Carnage(CarnageConfig(state_dir=tmp_path, gemini_api_key="k",
                              hub=HubConfig(enabled=False)), phone=AbsentPhone())
    names = c.capabilities.names()
    assert "music" not in names and "camera" not in names


# ── SOS ─────────────────────────────────────────────────────────────────────
def test_emergency_contacts_are_managed_by_voice_and_texted(carnage):
    assert "emergency contact" in tool(carnage, "add_emergency_contact",
                                       name="Papa", number="+919800000000")
    assert "Papa" in tool(carnage, "list_emergency_contacts")
    reply = tool(carnage, "sos_sms", note="fell on the stairs")
    assert "Papa" in reply
    [(to, text)] = carnage.phone.texts
    assert to == "+919800000000" and "fell on the stairs" in text


def test_the_sos_book_reads_venoms_file_format(tmp_path):
    path = tmp_path / "sos.json"
    path.write_text(json.dumps({"contacts": [
        {"name": "Ma", "to": "+91111", "label": "mother", "message": "",
         "enabled": True},
        {"name": "Old", "to": "+91222", "enabled": False}],
        "repeat_minutes": 10}))
    store = SosStore(path)
    assert [c["name"] for c in store.contacts(enabled_only=True)] == ["Ma"]
    store.add("ma", "+91333")                   # same person, new number
    assert [c["to"] for c in store.contacts() if c["name"] == "Ma"] == ["+91333"]
    assert json.loads(path.read_text())["repeat_minutes"] == 10   # kept


# ── timers and reminders fire on the phone ──────────────────────────────────
def test_timers_and_reminders_come_due_once(carnage):
    carnage.timers.add(0, "chai")
    carnage.reminders.add("call Ma", time.time() - 1)
    due = carnage.due()
    assert {"kind": "timer", "text": "Your chai timer is done."} in due
    assert {"kind": "reminder", "text": "call Ma"} in due
    assert carnage.due() == []


# ── the rest goes to the Pi ─────────────────────────────────────────────────
def test_she_is_told_whatsapp_lights_and_tv_go_to_the_pi(carnage):
    prompt = carnage.system_instruction()
    assert "WhatsApp" in prompt and "ask_other_device" in prompt


# ── phone actions and the phone book ───────────────────────────────────────
def test_calls_go_to_the_phone_book_number(carnage):
    assert tool(carnage, "call_person", who="papa") == "Calling Papa."
    assert ("call", "+919800000001") in carnage.phone.asked


def test_two_rahuls_means_asking_not_guessing(carnage):
    reply = tool(carnage, "call_person", who="Rahul")
    assert "Which one" in reply and "Rahul Sharma" in reply and "Rahul Verma" in reply
    assert not [a for a in carnage.phone.asked if a[0] == "call"]


def test_a_typed_number_is_dialled_as_it_is(carnage):
    tool(carnage, "call_person", who="+91 98000 00009")
    assert ("call", "+91 98000 00009") in carnage.phone.asked


def test_texts_find_people_in_the_phone_book_too(carnage):
    assert tool(carnage, "send_text", to="Papa", text="late") == "Sent to Papa."
    assert carnage.phone.texts == [("+919800000001", "late")]


def test_an_alarm_the_clock_app_never_got_becomes_a_reminder(carnage):
    carnage.phone.alarm_ok = False
    reply = tool(carnage, "set_alarm", time="in 20 minutes", label="chai")
    assert "remind you myself" in reply
    assert any(r["text"] == "chai" for r in carnage.reminders.pending())


def test_torch_apps_and_dnd(carnage):
    assert tool(carnage, "flashlight", on=True) == "Flashlight on."
    assert tool(carnage, "open_app", name="insta") == "Opening Instagram."
    assert "couldn't open" in tool(carnage, "open_app", name="nothing")
    assert tool(carnage, "do_not_disturb", on=True) == "Do Not Disturb is on."


@pytest.mark.parametrize("said, now, expect", [
    ("6:30", "2026-10-14 05:00", "2026-10-14 06:30"),
    ("6", "2026-10-14 14:00", "2026-10-14 18:00"),     # afternoon: 6 means 6 pm
    ("6", "2026-10-14 19:00", "2026-10-15 06:00"),     # evening: 6 means tomorrow
    ("7 am", "2026-10-14 09:00", "2026-10-15 07:00"),
    ("18:45", "2026-10-14 09:00", "2026-10-14 18:45"),
    ("in 20 minutes", "2026-10-14 09:00", "2026-10-14 09:20"),
])
def test_alarm_times(said, now, expect):
    from datetime import datetime

    from carnage.tools_actions import _parse_time

    fmt = "%Y-%m-%d %H:%M"
    got = _parse_time(said, datetime.strptime(now, fmt))
    assert got.strftime(fmt) == expect
