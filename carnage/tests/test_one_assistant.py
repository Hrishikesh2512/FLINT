"""The whole point, tested end to end: one assistant, two bodies.

Everything here goes through the real hub and the real JSON wire format, with
a Venom-shaped device on the other end. What is asserted is not that bytes
moved — `test_syncnet.py` covers that — but the three things the user would
actually notice:

    she is the same person on both
    she knows things she was told on the other one
    she does not say "I can't" about something her other body can do
"""

from __future__ import annotations

import json

import pytest
from flint_core.memory import MemoryStore
from flint_core.persona import render_persona
from flint_core.relay import RelayStore, carry_out
from flint_core.roster import build_roster
from flint_core.stores import ConnectionStore, NoteStore
from flint_core.sync import build_engine
from flint_core.syncnet import SyncLeaf
from flint_core.syncws import SyncServer

from carnage.config import CarnageConfig, HubConfig
from carnage.runtime import Carnage


class StubPhone:
    name = "stub"

    def available(self):
        return True

    def battery(self):
        from carnage.platform import Battery
        return Battery(80, charging=False)

    def locate(self):
        from carnage.platform import Fix
        return Fix(18.5, 73.8, accuracy=10.0)

    def send_sms(self, to, text):
        self.sent = getattr(self, "sent", [])
        self.sent.append((to, text))
        return True

    def notifications(self):
        return []

    def vibrate(self, milliseconds=400):
        return True


DEVICES = ({"name": "venom", "body": "the wearable on his body",
            "can": ["listen on the walk", "the earphone"]},)


@pytest.fixture()
def phone(tmp_path):
    config = CarnageConfig(
        device="carnage", user_name="Hrishikesh",
        state_dir=tmp_path / "phone", devices=DEVICES,
        hub=HubConfig(enabled=False))
    return Carnage(config, phone=StubPhone())


class Pi:
    """A Venom-shaped leaf: the same stores, its own disk, its own device id."""

    def __init__(self, root):
        root.mkdir(parents=True, exist_ok=True)
        self.memory = MemoryStore(root / "memory.json")
        self.notes = NoteStore(root / "notes.json")
        self.connections = ConnectionStore(root / "people.json")
        self.relay = RelayStore(root / "relay.json")
        self.engine = build_engine(
            "venom", memory=self.memory, notes=self.notes,
            connections=self.connections, state_path=root / "sync.json")


@pytest.fixture()
def pi(tmp_path):
    return Pi(tmp_path / "pi")


def wire_to(phone: Carnage):
    """A `call` that goes over the real hub, as JSON, both ways."""
    server = SyncServer(phone.sync, token="shared", relay=phone.relay,
                        roster=phone.roster)

    def call(message: dict) -> dict:
        return json.loads(server.handle_raw(json.dumps(message)))

    return call


def sync(pi: Pi, phone: Carnage):
    return SyncLeaf(pi.engine, token="shared", relay=pi.relay).exchange(
        wire_to(phone))


# ── she is the same person ──────────────────────────────────────────────────
def test_the_persona_is_identical_apart_from_one_sentence():
    """Names change; who she is does not."""
    on_phone = render_persona("Hrishikesh", "phone")
    on_pi = render_persona("Hrishikesh", "wearable")

    assert on_phone != on_pi
    # Everything after the opening body sentence is the same text.
    assert on_phone.split("Their name is", 1)[1] == \
        on_pi.split("Their name is", 1)[1]


def test_she_never_introduces_herself_as_a_different_assistant(phone):
    instruction = phone.system_instruction()
    assert "You are Jarvis" in instruction
    assert "Carnage" not in instruction     # the hostname is not her name


def test_the_body_sentence_is_true_of_this_device(phone):
    assert "on Hrishikesh's phone" in phone.system_instruction()


# ── she knows what the other body was told ──────────────────────────────────
def test_a_fact_told_to_the_pi_is_known_by_the_phone(pi, phone):
    pi.memory.remember("relationships", "ananya", "his sister, in Bengaluru")
    sync(pi, phone)
    assert "Bengaluru" in phone.memory.render_for_prompt()


def test_a_fact_told_to_the_phone_reaches_the_pi(pi, phone):
    phone.memory.remember("preferences", "tea", "masala chai, no sugar")
    sync(pi, phone)
    assert pi.memory.load()["preferences"]["tea"]["value"] == \
        "masala chai, no sugar"


def test_a_person_saved_on_one_body_is_reachable_from_the_other(pi, phone):
    pi.connections.save("Ma", phone="919812345678")
    sync(pi, phone)
    assert phone.connections.phone_for("Ma") == "919812345678"


def test_a_note_taken_on_the_walk_is_there_at_the_desk(pi, phone):
    pi.notes.add("book the bike service")
    sync(pi, phone)
    assert [n["text"] for n in phone.notes.all()] == ["book the bike service"]


def test_it_is_the_prompt_that_changes_not_just_a_file(pi, phone):
    """A synced fact she cannot say is not a synced fact."""
    pi.memory.remember("identity", "sister", "Ananya")
    sync(pi, phone)
    assert "Ananya" in phone.system_instruction()


# ── she knows she has another body ──────────────────────────────────────────
def test_the_phone_is_told_about_the_wearable(phone):
    instruction = phone.system_instruction()
    assert "the wearable on his body (venom)" in instruction
    assert "listen on the walk" in instruction


def test_she_is_told_not_to_refuse_what_her_other_body_can_do(phone):
    assert "never say you can't" in phone.system_instruction().lower()


def test_syncing_is_what_marks_the_other_body_present(pi, phone):
    assert "Not heard from yet." in phone.system_instruction()
    sync(pi, phone)
    assert "Reachable now." in phone.system_instruction()


def test_an_absent_device_is_not_claimed_to_be_reachable(phone):
    assert "Reachable now." not in phone.system_instruction()


# ── work crosses between the bodies ─────────────────────────────────────────
def test_the_pi_asks_the_phone_to_send_a_text_and_it_arrives(pi, phone):
    """The whole illusion in one test.

    The Pi has no cellular radio. It does not say "I can't"; it hands the job
    to the body that does, over the connection that already exists.
    """
    sync(pi, phone)                     # the Pi learns the phone is awake

    pi_roster = build_roster("venom", [
        {"name": "carnage", "body": "on his phone", "can": ["send a text"]}])
    pi_roster.seen("carnage")
    pi.relay.submit("venom", "carnage", "text Ma that I'm running late")

    sync(pi, phone)                     # the request rides up with the sync

    waiting = phone.relay.waiting_for("carnage")
    assert [r.text for r in waiting] == ["text Ma that I'm running late"]

    handled = phone.run_relayed(run=lambda text: "Texted Ma.")
    assert [r.status for r in handled] == ["done"]

    sync(pi, phone)                     # and the answer comes back down
    assert pi.relay.answers_for("venom")[0].answer == "Texted Ma."


def test_a_request_survives_the_phone_being_asleep(pi, phone):
    """Queued, not failed — the leaf sends it whenever it next gets through."""
    pi.relay.submit("venom", "carnage", "text Ma")
    assert pi.relay.waiting_for("carnage")      # held locally, nothing lost

    sync(pi, phone)
    assert phone.relay.waiting_for("carnage")


def test_relay_traffic_moves_even_with_no_memory_changes(pi, phone):
    """A queued request must not wait for an unrelated fact to travel with."""
    sync(pi, phone)                     # drain anything outstanding
    pi.relay.submit("venom", "carnage", "text Ma")

    result = sync(pi, phone)
    assert result.pushed == 0           # no memory changes at all
    assert phone.relay.waiting_for("carnage")


def test_a_failure_on_the_far_body_comes_back_as_a_failure(pi, phone):
    pi.relay.submit("venom", "carnage", "text Ma")
    sync(pi, phone)

    def no_signal(text):
        raise RuntimeError("no cellular signal")

    carry_out(phone.relay, "carnage", run=no_signal)
    sync(pi, phone)
    answer = pi.relay.answers_for("venom")[0]
    assert answer.status == "failed"
    assert "no cellular signal" in answer.answer


# ── the phone's own body ────────────────────────────────────────────────────
def test_the_phone_offers_what_only_a_phone_has(phone):
    names = set(phone.registry.names())
    assert {"send_text", "where_am_i", "phone_battery"} <= names


def test_it_also_offers_the_shared_belt(phone):
    names = set(phone.registry.names())
    assert {"add_note", "add_to_list", "save_memory", "remember_about",
            "save_connection", "add_task"} <= names


def test_and_the_tools_for_being_one_assistant(phone):
    assert {"ask_other_device", "check_other_device"} <= set(
        phone.registry.names())
