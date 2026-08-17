"""Knowing about her other bodies, and handing work to them.

What is really under test is a behaviour rather than a data structure: that she
never says "I can't do that" about something one of her own bodies can do, and
never claims a device is available when it has been asleep since yesterday.
"""

from __future__ import annotations

import pytest

from flint_core.relay import (
    DONE,
    FAILED,
    PENDING,
    RelayStore,
    carry_out,
    register_relay_tools,
)
from flint_core.roster import Device, Roster, build_roster
from flint_core.tools import ToolRegistry

NOW = 1_700_000_000.0


def roster_of(me="venom", **seen) -> Roster:
    roster = build_roster(me, [
        {"name": "carnage", "body": "on his phone",
         "can": ["send a text", "GPS position", "emergency SMS"]},
        {"name": "flint", "body": "on his desktop",
         "can": ["the screen", "his files and repos"]},
    ])
    for name, when in seen.items():
        roster.devices[name] = Device(
            name=name, body=roster.devices[name].body,
            can=roster.devices[name].can, last_seen=when)
    return roster


# ── the roster ──────────────────────────────────────────────────────────────
def test_she_is_not_listed_among_her_own_other_bodies():
    roster = roster_of("venom")
    assert [d.name for d in roster.others()] == ["carnage", "flint"]

    roster.add(Device(name="venom", body="the one speaking"))
    assert "venom" not in [d.name for d in roster.others()]


def test_the_prompt_block_says_what_each_body_can_do():
    block = roster_of(carnage=NOW).render_for_prompt(now=NOW)
    assert "on his phone (carnage)" in block
    assert "send a text" in block
    assert "Reachable now." in block


def test_it_forbids_the_thing_that_breaks_the_illusion():
    """'I can't' about something the phone in his pocket could do."""
    block = roster_of().render_for_prompt(now=NOW)
    assert "never say you can't" in block.lower()
    assert "These are you, not other assistants" in block


def test_an_empty_roster_renders_nothing_at_all():
    assert Roster(me="venom").render_for_prompt() == ""


def test_a_device_not_heard_from_is_not_claimed_to_be_available():
    """Claiming the laptop is up when it has been shut since yesterday is a
    confident, specific, wrong answer."""
    block = roster_of(carnage=NOW, flint=NOW - 8 * 3600).render_for_prompt(now=NOW)
    assert "Reachable now." in block
    assert "8 hours ago" in block


def test_a_device_never_seen_says_so_rather_than_guessing():
    assert "Not heard from yet." in roster_of().render_for_prompt(now=NOW)


def test_a_long_silence_stops_counting_days():
    roster = roster_of(flint=NOW - 40 * 86400)
    assert roster.devices["flint"].presence(NOW) == "Away."


def test_one_missed_sync_does_not_make_a_device_gone():
    """A five-minute interval on a flaky hotspot misses ticks all the time."""
    roster = roster_of(carnage=NOW - 11 * 60)
    assert roster.devices["carnage"].fresh(NOW)


# ── finding one by whatever she called it ───────────────────────────────────
@pytest.mark.parametrize("said", ["carnage", "Carnage", "phone", "his phone",
                                  "send a text"])
def test_a_device_is_found_by_name_body_or_what_it_does(said):
    assert roster_of().find(said).name == "carnage"


def test_the_desktop_is_found_by_what_lives_on_it():
    assert roster_of().find("repos").name == "flint"


def test_an_unknown_device_is_not_guessed_at():
    assert roster_of().find("the fridge") is None
    assert roster_of().find("") is None


# ── presence, established by syncing ────────────────────────────────────────
def test_syncing_is_what_makes_a_device_present(tmp_path):
    roster = build_roster("carnage", [{"name": "venom", "body": "the wearable"}],
                          path=tmp_path / "roster.json")
    assert not roster.reachable(now=NOW)
    roster.seen("venom", now=NOW)
    assert [d.name for d in roster.reachable(now=NOW)] == ["venom"]


def test_presence_survives_a_restart(tmp_path):
    path = tmp_path / "roster.json"
    first = build_roster("carnage", [{"name": "venom", "body": "the wearable",
                                      "can": ["listen on the walk"]}], path=path)
    first.seen("venom", now=NOW)

    reopened = Roster(me="carnage", path=path)
    assert reopened.devices["venom"].last_seen == NOW
    assert reopened.devices["venom"].can == ("listen on the walk",)


def test_a_check_in_never_overwrites_what_a_device_is(tmp_path):
    roster = roster_of("venom")
    roster.seen("carnage", now=NOW)
    assert roster.devices["carnage"].body == "on his phone"
    assert "send a text" in roster.devices["carnage"].can


def test_an_unconfigured_device_that_syncs_is_still_noticed():
    """She should not be blind to a body that is demonstrably there."""
    roster = Roster(me="venom")
    roster.seen("carnage", now=NOW)
    assert roster.find("carnage") is not None


# ── handing work over ───────────────────────────────────────────────────────
def registry_for(me, roster, relay, now=NOW):
    reg = ToolRegistry()
    register_relay_tools(reg, relay, roster, me, clock=lambda: now)
    return reg


def test_the_pi_hands_a_text_to_the_phone(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    reg = registry_for("venom", roster_of(carnage=NOW), relay)

    said = reg.dispatch("ask_other_device",
                        {"device": "phone", "task": "text Ma that I'm late"})
    assert "phone" in said
    waiting = relay.waiting_for("carnage")
    assert [r.text for r in waiting] == ["text Ma that I'm late"]


def test_a_sleeping_device_queues_rather_than_failing(tmp_path):
    """Eventual delivery is a real outcome she has to be able to say."""
    relay = RelayStore(tmp_path / "relay.json")
    reg = registry_for("venom", roster_of(flint=NOW - 9 * 3600), relay)

    said = reg.dispatch("ask_other_device",
                        {"device": "desktop", "task": "open the FLINT repo"})
    assert "not awake" in said
    assert "9 hours ago" in said
    assert relay.waiting_for("flint")          # queued, not dropped


def test_asking_an_unknown_device_lists_the_real_ones(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    reg = registry_for("venom", roster_of(), relay)
    said = reg.dispatch("ask_other_device", {"device": "toaster", "task": "x"})
    assert "carnage" in said and "flint" in said


def test_the_far_device_runs_it_and_the_answer_comes_back(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    relay.submit("venom", "carnage", "text Ma that I'm late")

    handled = carry_out(relay, "carnage", run=lambda text: "Sent to Ma.")
    assert [r.status for r in handled] == [DONE]

    reg = registry_for("venom", roster_of(carnage=NOW), relay)
    assert "Sent to Ma." in reg.dispatch("check_other_device", {})


def test_a_failure_on_the_far_side_comes_back_as_a_failure(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    relay.submit("venom", "carnage", "text Ma")

    def explode(text):
        raise RuntimeError("no cellular signal")

    carry_out(relay, "carnage", run=explode)
    assert relay.answers_for("venom")[0].status == FAILED
    assert "no cellular signal" in relay.answers_for("venom")[0].answer


def test_one_failing_request_does_not_stop_the_others(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    relay.submit("venom", "carnage", "first")
    relay.submit("venom", "carnage", "second")

    def half(text):
        if text == "first":
            raise RuntimeError("nope")
        return "did the second"

    handled = carry_out(relay, "carnage", run=half)
    assert len(handled) == 2
    assert {r.status for r in handled} == {DONE, FAILED}


def test_nothing_outstanding_is_said_plainly(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    reg = registry_for("venom", roster_of(), relay)
    assert reg.dispatch("check_other_device", {}) == "Nothing outstanding."


def test_still_waiting_is_reported_as_waiting(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    relay.submit("venom", "carnage", "text Ma")
    reg = registry_for("venom", roster_of(), relay)
    assert "Still waiting" in reg.dispatch("check_other_device", {})


def test_a_request_nobody_picked_up_expires_rather_than_queueing_forever(tmp_path):
    """'Text Ma I'm running late', delivered four hours late, is worse than never."""
    clock = {"t": NOW}
    relay = RelayStore(tmp_path / "relay.json", clock=lambda: clock["t"])
    relay.submit("venom", "carnage", "text Ma that I'm 10 minutes away")

    clock["t"] = NOW + 3 * 3600
    assert relay.waiting_for("carnage") == []
    assert relay.answers_for("venom")[0].status == FAILED


def test_a_request_needs_a_target_and_something_to_do(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    with pytest.raises(ValueError):
        relay.submit("venom", "", "do a thing")
    with pytest.raises(ValueError):
        relay.submit("venom", "carnage", "   ")


# ── merging, which is how it crosses the wire ───────────────────────────────
def test_a_completion_beats_a_pending_copy(tmp_path):
    """Only the device that ran it can complete it, so its version wins."""
    hub = RelayStore(tmp_path / "hub.json")
    hub.submit("venom", "carnage", "text Ma")
    pending = hub.all_dicts()

    phone = RelayStore(tmp_path / "phone.json")
    phone.merge(pending)
    carry_out(phone, "carnage", run=lambda t: "Sent.")

    hub.merge(phone.all_dicts())
    assert hub.answers_for("venom")[0].status == DONE


def test_merging_the_same_thing_twice_changes_nothing(tmp_path):
    hub = RelayStore(tmp_path / "hub.json")
    hub.submit("venom", "carnage", "text Ma")
    entries = hub.all_dicts()

    other = RelayStore(tmp_path / "other.json")
    assert other.merge(entries) == 1
    assert other.merge(entries) == 0


def test_rubbish_in_a_merge_is_skipped(tmp_path):
    relay = RelayStore(tmp_path / "relay.json")
    assert relay.merge([{"nonsense": True}, {}, {"id": "x"}]) == 0


def test_a_relay_file_that_will_not_parse_starts_empty(tmp_path):
    path = tmp_path / "relay.json"
    path.write_text("{broken", encoding="utf-8")
    assert RelayStore(path).all_dicts() == []


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "relay.json"
    RelayStore(path).submit("venom", "carnage", "text Ma")
    assert [r["status"] for r in RelayStore(path).all_dicts()] == [PENDING]
