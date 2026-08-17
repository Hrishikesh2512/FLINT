"""The phone's own tools, and the emergency path that does not need data."""

from __future__ import annotations

import pytest
from flint_core.tools import ToolRegistry

from carnage.platform import Battery, Fix, Notification
from carnage.tools_phone import (
    register_phone_tools,
    register_sms_tools,
    register_sos_sms,
)


class StubPhone:
    name = "stub"

    def __init__(self, battery=None, fix=None, sms_ok=True, notifications=()):
        self._battery = battery
        self._fix = fix
        self._sms_ok = sms_ok
        self._notifications = list(notifications)
        self.sent: list[tuple[str, str]] = []

    def available(self):
        return True

    def battery(self):
        return self._battery

    def locate(self):
        return self._fix

    def send_sms(self, to, text):
        self.sent.append((to, text))
        return self._sms_ok(to) if callable(self._sms_ok) else self._sms_ok

    def notifications(self):
        return self._notifications

    def vibrate(self, milliseconds=400):
        return True


class StubSos:
    def __init__(self, contacts):
        self._contacts = contacts

    def contacts(self, enabled_only=False):
        if enabled_only:
            return [c for c in self._contacts if c.get("enabled", True)]
        return self._contacts


class StubContacts:
    def __init__(self, book):
        self._book = book

    def phone_for(self, query):
        return self._book.get(query.lower(), "")


def registry_with(phone, **kwargs):
    reg = ToolRegistry()
    register_phone_tools(reg, phone)
    register_sms_tools(reg, phone, contacts=kwargs.get("contacts"))
    if "sos" in kwargs:
        register_sos_sms(reg, phone, kwargs["sos"])
    return reg


# ── battery ─────────────────────────────────────────────────────────────────
def test_battery_is_reported():
    reg = registry_with(StubPhone(battery=Battery(72, charging=True)))
    assert "72%" in reg.dispatch("phone_battery", {})


def test_a_low_battery_gets_a_warning_a_full_one_does_not():
    low = registry_with(StubPhone(battery=Battery(9, charging=False)))
    assert "plugging in" in low.dispatch("phone_battery", {})

    fine = registry_with(StubPhone(battery=Battery(90, charging=False)))
    assert "plugging in" not in fine.dispatch("phone_battery", {})


def test_a_low_but_charging_battery_is_not_nagged_about():
    reg = registry_with(StubPhone(battery=Battery(9, charging=True)))
    assert "plugging in" not in reg.dispatch("phone_battery", {})


def test_an_unreadable_battery_says_so():
    reg = registry_with(StubPhone(battery=None))
    assert "can't read" in reg.dispatch("phone_battery", {})


# ── location ────────────────────────────────────────────────────────────────
def test_a_fix_is_spoken():
    reg = registry_with(StubPhone(fix=Fix(18.52043, 73.85674, accuracy=7.0)))
    assert "18.52043" in reg.dispatch("where_am_i", {})


def test_no_fix_explains_itself():
    reg = registry_with(StubPhone(fix=None))
    assert "location may be off" in reg.dispatch("where_am_i", {})


# ── notifications ───────────────────────────────────────────────────────────
def test_notifications_are_summarised_newest_first():
    reg = registry_with(StubPhone(notifications=[
        Notification("com.whatsapp", "Ma", "khana kha liya?", when=100),
        Notification("com.google.android.gm", "Bank", "statement ready", when=200),
    ]))
    said = reg.dispatch("check_notifications", {})
    assert said.index("Bank") < said.index("Ma")


def test_a_quiet_shade_says_nothing_waiting():
    assert reg_says_nothing(registry_with(StubPhone()))


def reg_says_nothing(reg):
    return reg.dispatch("check_notifications", {}) == "Nothing waiting."


def test_a_flood_is_capped_and_counted():
    many = [Notification("com.x", f"t{i}", "body", when=i) for i in range(20)]
    reg = registry_with(StubPhone(notifications=many))
    said = reg.dispatch("check_notifications", {})
    assert "and 14 more" in said


# ── sending ─────────────────────────────────────────────────────────────────
def test_a_number_is_used_as_given():
    phone = StubPhone()
    reg = registry_with(phone)
    reg.dispatch("send_text", {"to": "+91 98123-45678", "text": "on my way"})
    assert phone.sent == [("+91 98123-45678", "on my way")]


def test_a_name_is_resolved_through_the_contact_book():
    phone = StubPhone()
    reg = registry_with(phone, contacts=StubContacts({"ma": "919812345678"}))
    reg.dispatch("send_text", {"to": "Ma", "text": "reaching in 10"})
    assert phone.sent == [("919812345678", "reaching in 10")]


def test_an_unknown_name_is_not_guessed_at():
    phone = StubPhone()
    reg = registry_with(phone, contacts=StubContacts({}))
    said = reg.dispatch("send_text", {"to": "Rahul", "text": "hi"})
    assert "don't have a number" in said
    assert phone.sent == []


def test_a_failed_send_is_reported_as_failed():
    reg = registry_with(StubPhone(sms_ok=False))
    said = reg.dispatch("send_text", {"to": "+91981", "text": "hi"})
    assert "did not go" in said


# ── SOS: the reason this body exists ────────────────────────────────────────
def test_sos_texts_every_enabled_contact():
    phone = StubPhone(fix=Fix(18.5, 73.8, accuracy=10.0))
    sos = StubSos([{"name": "Papa", "to": "+9111", "enabled": True},
                   {"name": "Ananya", "to": "+9122", "enabled": True}])
    reg = registry_with(phone, sos=sos)

    said = reg.dispatch("sos_sms", {"note": "bike accident"})
    assert len(phone.sent) == 2
    assert "bike accident" in phone.sent[0][1]
    assert "Papa" in said and "Ananya" in said


def test_sos_includes_where_he_is():
    phone = StubPhone(fix=Fix(18.52, 73.85, accuracy=9.0))
    reg = registry_with(phone, sos=StubSos([{"name": "Papa", "to": "+9111"}]))
    reg.dispatch("sos_sms", {"note": "help"})
    assert "18.52" in phone.sent[0][1]


def test_sos_still_sends_without_a_location():
    phone = StubPhone(fix=None)
    reg = registry_with(phone, sos=StubSos([{"name": "Papa", "to": "+9111"}]))
    reg.dispatch("sos_sms", {"note": "help"})
    assert phone.sent


def test_a_disabled_contact_is_not_texted():
    """Switched off in the panel means switched off, especially in an emergency."""
    phone = StubPhone()
    sos = StubSos([{"name": "Papa", "to": "+9111", "enabled": True},
                   {"name": "Old Number", "to": "+9199", "enabled": False}])
    reg = registry_with(phone, sos=sos)
    reg.dispatch("sos_sms", {"note": "help"})
    assert [to for to, _ in phone.sent] == ["+9111"]


def test_a_partial_failure_names_who_was_not_reached():
    """'Sent' that quietly means 'sent to two of five' makes him stop trying."""
    phone = StubPhone(sms_ok=lambda to: to == "+9111")
    sos = StubSos([{"name": "Papa", "to": "+9111"},
                   {"name": "Ananya", "to": "+9122"}])
    reg = registry_with(phone, sos=sos)

    said = reg.dispatch("sos_sms", {"note": "help"})
    assert "Papa" in said
    assert "Could not reach Ananya" in said


def test_a_total_failure_says_to_call():
    phone = StubPhone(sms_ok=False)
    reg = registry_with(phone, sos=StubSos([{"name": "Papa", "to": "+9111"}]))
    said = reg.dispatch("sos_sms", {"note": "help"})
    assert "could not send" in said.lower()
    assert "calling" in said


def test_no_contacts_is_said_plainly():
    reg = registry_with(StubPhone(), sos=StubSos([]))
    assert "don't have any emergency contacts" in reg.dispatch("sos_sms", {})


def test_a_broken_contact_book_does_not_take_sos_down():
    class Broken:
        def contacts(self, enabled_only=False):
            raise OSError("the file is gone")

    reg = registry_with(StubPhone(), sos=Broken())
    assert "emergency contacts" in reg.dispatch("sos_sms", {})


@pytest.mark.parametrize("note", ["", "   "])
def test_sos_without_a_note_still_says_something_useful(note):
    phone = StubPhone()
    reg = registry_with(phone, sos=StubSos([{"name": "Papa", "to": "+9111"}]))
    reg.dispatch("sos_sms", {"note": note})
    assert "he needs help" in phone.sent[0][1]
