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
