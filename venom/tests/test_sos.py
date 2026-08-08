"""Emergency SOS — contact book, alert fan-out, emergency mode, and the
honesty of what it reports.

A fake WhatsApp bridge records every send (and can refuse specific
recipients), so the whole path is exercised with no network, no Pi, and
nobody's phone buzzing.
"""

import pytest

from venom.config import VenomConfig
from venom.sos import DEFAULT_MESSAGE, EmergencySos, SosStore, manage_contacts
from venom.tools_pi import TimerBoard, build_pi_registry


class FakeWhatsApp:
    """Records sends; recipients in `refuse` come back as undelivered."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.sent: list[tuple[str, str]] = []   # (to, text)

    def send_detail(self, text, to=""):
        self.sent.append((to, text))
        if to in self.refuse:
            return False, "I couldn't send that: not on WhatsApp."
        return True, f"Sent to {to}."


class FakeLocation:
    def __init__(self, loc=None):
        self.loc = loc if loc is not None else {
            "city": "Pune", "region": "Maharashtra", "country": "IN",
            "lat": 18.52, "lon": 73.85}
        self.calls = 0

    def get(self, force=False):
        self.calls += 1
        return self.loc


class FakeConnections:
    def __init__(self, phones=None):
        self.phones = phones or {}

    def phone_for(self, name):
        return self.phones.get(name.lower(), "")


def _sos(tmp_path, whatsapp=None, location=None, connections=None,
         spawn=None) -> EmergencySos:
    return EmergencySos(SosStore(tmp_path / "sos.json"),
                        whatsapp or FakeWhatsApp(),
                        location=location, connections=connections,
                        user_name="Hrishikesh", clock=lambda: 1_700_000_000.0,
                        spawn=spawn or (lambda fn: None))


# ── contact book ─────────────────────────────────────────────────────────────
def test_add_list_and_persist_contacts(tmp_path):
    store = SosStore(tmp_path / "sos.json")
    store.add("Papa", to="919812345678", label="father")
    store.add("Riya", to="919999000011", label="sister", enabled=False)

    reloaded = SosStore(tmp_path / "sos.json")          # i.e. after a reboot
    names = [c["name"] for c in reloaded.contacts()]
    assert names == ["Papa", "Riya"]
    assert [c["name"] for c in reloaded.contacts(enabled_only=True)] == ["Papa"]


def test_adding_the_same_name_edits_instead_of_duplicating(tmp_path):
    store = SosStore(tmp_path / "sos.json")
    store.add("Papa", to="919812345678", label="father")
    store.add("papa", message="Papa, mujhe help chahiye.")

    contacts = store.contacts()
    assert len(contacts) == 1
    # the number given earlier survives a later edit that didn't mention it
    assert contacts[0]["to"] == "919812345678"
    assert contacts[0]["label"] == "father"
    assert contacts[0]["message"] == "Papa, mujhe help chahiye."


def test_remove_enable_and_message_report_unknown_names(tmp_path):
    store = SosStore(tmp_path / "sos.json")
    store.add("Papa")
    assert store.remove("Nobody") is False
    assert store.set_enabled("Nobody", False) is False
    assert store.set_message("hi", "Nobody") is False
    assert store.set_enabled("papa", False) is True
    assert store.contacts()[0]["enabled"] is False


def test_corrupt_file_reads_as_empty_not_an_exception(tmp_path):
    path = tmp_path / "sos.json"
    path.write_text("{not json at all")
    assert SosStore(path).contacts() == []


def test_repeat_interval_has_a_floor_and_an_off_switch(tmp_path):
    store = SosStore(tmp_path / "sos.json")
    assert store.set_settings(repeat_minutes=0.05)["repeat_minutes"] == 1.0
    assert store.set_settings(repeat_minutes=0)["repeat_minutes"] == 0.0


# ── firing the alert ─────────────────────────────────────────────────────────
def test_start_alerts_every_enabled_contact_with_location(tmp_path):
    wa, loc = FakeWhatsApp(), FakeLocation()
    sos = _sos(tmp_path, wa, location=loc)
    sos.store.add("Papa", to="919812345678", label="father")
    sos.store.add("Riya", to="919999000011")
    sos.store.add("Old Flat", to="919000000000", enabled=False)

    summary = sos.start("someone is following me")

    assert [to for to, _ in wa.sent] == ["919812345678", "919999000011"]
    body = wa.sent[0][1]
    assert "EMERGENCY" in body and "Hrishikesh" in body
    assert "Pune, Maharashtra, IN" in body
    assert "https://maps.google.com/?q=18.52,73.85" in body
    assert "someone is following me" in body
    assert "Papa and Riya" in summary
    assert sos.active is True


def test_start_without_contacts_says_so_and_sends_nothing(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    result = sos.start()
    assert wa.sent == []
    assert "no emergency contacts" in result.lower()


def test_a_contacts_own_wording_overrides_the_default(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981", message="Papa, {user} ko help chahiye.")
    sos.store.add("Riya", to="91999")
    sos.start()

    assert wa.sent[0][1] == "Papa, Hrishikesh ko help chahiye."
    assert "EMERGENCY" in wa.sent[1][1]


def test_failed_delivery_is_reported_not_swallowed(tmp_path):
    wa = FakeWhatsApp(refuse={"91999"})
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")
    sos.store.add("Riya", to="91999")

    summary = sos.start()
    assert "Papa" in summary
    assert "did NOT reach Riya" in summary


def test_total_failure_tells_the_user_to_call(tmp_path):
    wa = FakeWhatsApp(refuse={"91981"})
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")

    summary = sos.start()
    assert "NOT" in summary and "call them directly" in summary.lower()


def test_a_dead_bridge_fails_the_contact_without_raising(tmp_path):
    class DeadBridge:
        def send_detail(self, text, to=""):
            raise OSError("connection refused")

    sos = _sos(tmp_path, DeadBridge())
    sos.store.add("Papa", to="91981")
    assert "NOT" in sos.start()


def test_location_failure_still_sends_the_alert(tmp_path):
    class BrokenLocation:
        def get(self, force=False):
            raise RuntimeError("no network")

    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa, location=BrokenLocation())
    sos.store.add("Papa", to="91981")
    sos.start()
    assert "Location: unavailable" in wa.sent[0][1]


def test_name_only_contact_resolves_through_connections(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa,
               connections=FakeConnections({"papa": "919812345678"}))
    sos.store.add("Papa")                      # no number of its own
    sos.store.add("Bhaiya")                    # unknown to Connections
    sos.start()
    assert [to for to, _ in wa.sent] == ["919812345678", "Bhaiya"]


# ── emergency mode ───────────────────────────────────────────────────────────
def test_mode_stays_on_and_repeats_location_until_stopped(tmp_path):
    wa, loc = FakeWhatsApp(), FakeLocation()
    spawned = []
    sos = _sos(tmp_path, wa, location=loc, spawn=spawned.append)
    sos.store.add("Papa", to="91981")

    sos.start()
    assert sos.active is True
    assert len(spawned) == 1                   # the repeat loop was launched

    # one lap of the repeat loop, without waiting out the real interval
    sos._stop.wait = lambda timeout: False
    sos._stop.is_set = lambda: False
    original = sos._broadcast
    calls = []

    def once(*args, **kwargs):
        calls.append(kwargs.get("template_key"))
        sos._stop.is_set = lambda: True        # stop after this lap
        return original(*args, **kwargs)

    sos._broadcast = once
    sos._repeat_loop()
    assert calls == ["update"]
    assert "still in an emergency" in wa.sent[-1][1]


def test_second_start_does_not_launch_a_second_repeat_loop(tmp_path):
    spawned = []
    sos = _sos(tmp_path, spawn=spawned.append)
    sos.store.add("Papa", to="91981")
    sos.start()
    sos.start("it's getting worse")
    assert len(spawned) == 1


def test_stop_sends_all_clear_and_leaves_the_mode(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")
    sos.start()
    result = sos.stop("was a false alarm")

    assert sos.active is False
    assert "All clear" in wa.sent[-1][1]
    assert "was a false alarm" in wa.sent[-1][1]
    assert "Papa" in result


def test_failed_all_clear_warns_they_still_think_youre_in_trouble(tmp_path):
    wa = FakeWhatsApp(refuse={"91981"})
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")
    sos.start()
    result = sos.stop()
    assert "still think you're in trouble" in result
    assert sos.active is False          # the mode ends either way


def test_stop_without_an_emergency_sends_nothing(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")
    assert "not in emergency mode" in sos.stop().lower()
    assert wa.sent == []


def test_test_alert_is_clearly_marked_and_does_not_arm_the_mode(tmp_path):
    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    sos.store.add("Papa", to="91981")
    result = sos.test()

    assert "ONLY A TEST" in wa.sent[0][1]
    assert sos.active is False
    assert "Test alert sent to Papa" in result


def test_status_reflects_the_mode(tmp_path):
    sos = _sos(tmp_path)
    sos.store.add("Papa", to="91981", label="father")
    assert "off" in sos.status()
    sos.start()
    assert "Emergency mode has been on" in sos.status()


# ── the voice-facing surface ─────────────────────────────────────────────────
def test_manage_contacts_covers_the_spoken_actions(tmp_path):
    sos = _sos(tmp_path)
    assert "emergency list" in manage_contacts(
        sos, "add", name="Papa", to="919812345678", label="father")
    assert "Papa" in manage_contacts(sos, "list")
    assert "paused" in manage_contacts(sos, "disable", name="Papa")
    assert "will be alerted" in manage_contacts(sos, "enable", name="Papa")
    assert "updated" in manage_contacts(
        sos, "set_message", name="Papa", message="help")
    assert "off your emergency list" in manage_contacts(
        sos, "remove", name="Papa")
    assert "no emergency contacts" in manage_contacts(sos, "list").lower()


def test_manage_contacts_needs_a_name_and_rejects_nonsense(tmp_path):
    sos = _sos(tmp_path)
    assert "need a name" in manage_contacts(sos, "add")
    assert "don't know the SOS action" in manage_contacts(sos, "explode")


def test_default_message_carries_every_placeholder():
    # A template that drops one of these would send a blank-looking alert.
    for token in ("{user}", "{location}", "{time}", "{note}"):
        assert token in DEFAULT_MESSAGE


# ── registry wiring ──────────────────────────────────────────────────────────
SOS_TOOLS = ("emergency_sos", "end_emergency", "sos_status",
             "emergency_contacts")


def test_registry_exposes_sos_tools_and_dispatches(tmp_path):
    class _DummyMem:
        def load(self):
            return {}

        def render_for_prompt(self):
            return ""

    wa = FakeWhatsApp()
    sos = _sos(tmp_path, wa)
    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(),
                            timers=TimerBoard(), sos=sos)
    for name in SOS_TOOLS:
        assert name in reg

    assert "emergency list" in reg.dispatch(
        "emergency_contacts", {"action": "add", "name": "Papa", "to": "91981"})
    assert "Papa" in reg.dispatch("emergency_sos", {"note": "help"})
    assert wa.sent and "EMERGENCY" in wa.sent[0][1]
    assert "Emergency mode" in reg.dispatch("end_emergency", {})
    assert "off" in reg.dispatch("sos_status", {})


def test_sos_tools_absent_without_an_sos_module():
    class _DummyMem:
        def load(self):
            return {}

        def render_for_prompt(self):
            return ""

    reg = build_pi_registry(VenomConfig(), memory=_DummyMem(),
                            timers=TimerBoard())
    for name in SOS_TOOLS:
        assert name not in reg


def test_sos_tools_are_journalled_as_real_actions():
    from venom.live import ACTION_TOOLS

    # Without this she can *say* she sent an SOS and the journal would agree.
    assert {"emergency_sos", "end_emergency"} <= ACTION_TOOLS


@pytest.mark.parametrize("action", ["trigger", "stop", "test"])
def test_console_refuses_to_send_when_the_voice_loop_is_down(tmp_path, action,
                                                             monkeypatch):
    from venom import web

    console = web.WebConsole(port=0)
    monkeypatch.setattr(console, "_sos_store",
                        lambda: SosStore(tmp_path / "sos.json"))
    assert "isn't live" in console.sos_action({"action": action})


def test_console_edits_contacts_with_the_voice_loop_down(tmp_path, monkeypatch):
    from venom import web

    console = web.WebConsole(port=0)
    monkeypatch.setattr(console, "_sos_store",
                        lambda: SosStore(tmp_path / "sos.json"))

    assert "saved" in console.sos_action(
        {"action": "add", "name": "Papa", "to": "91981", "label": "father"})
    snap = console.sos_snapshot()
    assert snap["contacts"][0]["name"] == "Papa"
    assert snap["active"] is False
    assert "removed" in console.sos_action({"action": "remove", "name": "Papa"})
