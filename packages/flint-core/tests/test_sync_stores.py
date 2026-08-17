"""Syncing the two stores that were left out: the hot memory tier and outcomes.

The archive and projects were covered when sync was written. These two are the
ones a third device made urgent — memory because it is what she actually says
out loud, and outcomes because the evidence threshold means three devices
learning separately may each stay below it forever.
"""

from __future__ import annotations

import pytest

from flint_core.memory import MemoryStore
from flint_core.outcomes import OutcomeLog
from flint_core.sync import MemorySync, OutcomeSync, build_engine, merge_rounds


class Device:
    def __init__(self, memory, outcomes, engine, name):
        self.memory = memory
        self.outcomes = outcomes
        self.engine = engine
        self.name = name


def _device(root, name, clock):
    root.mkdir(parents=True, exist_ok=True)
    memory = MemoryStore(root / "memory.json", clock=clock)
    outcomes = OutcomeLog(root / "outcomes.jsonl", clock=clock)
    engine = build_engine(name, memory=memory, outcomes=outcomes,
                          state_path=root / "sync.json")
    return Device(memory, outcomes, engine, name)


@pytest.fixture()
def phone(tmp_path, fake_clock):
    return _device(tmp_path / "carnage", "carnage", fake_clock)


@pytest.fixture()
def pi(tmp_path, fake_clock):
    return _device(tmp_path / "venom", "venom", fake_clock)


# ── memory: the facts she speaks ────────────────────────────────────────────
def test_a_fact_learned_on_the_phone_is_known_by_the_pi(phone, pi):
    phone.memory.remember("identity", "sister", "Ananya, in Bengaluru")
    merge_rounds(phone.engine, pi.engine)
    assert pi.memory.load()["identity"]["sister"]["value"] == "Ananya, in Bengaluru"


def test_the_fact_arrives_in_the_prompt_not_just_the_file(phone, pi):
    phone.memory.remember("preferences", "chai", "no sugar")
    merge_rounds(phone.engine, pi.engine)
    assert "no sugar" in pi.memory.render_for_prompt()


def test_same_day_edits_resolve_by_time_not_by_device_name(phone, pi, fake_clock):
    """The reason `t` exists.

    Both devices edit the same fact on the same day. With only the `updated`
    date the two would tie and the tie-break — device id — would hand it to
    whichever name sorts higher ('venom' over 'carnage') regardless of which
    edit was actually later. The newer edit has to win.
    """
    phone.memory.remember("preferences", "tea", "masala chai")
    fake_clock.advance(60)
    pi.memory.remember("preferences", "tea", "green tea")
    fake_clock.advance(60)
    merge_rounds(phone.engine, pi.engine)

    assert phone.memory.load()["preferences"]["tea"]["value"] == "green tea"
    assert pi.memory.load()["preferences"]["tea"]["value"] == "green tea"


def test_the_older_edit_wins_nothing_even_when_it_syncs_second(phone, pi,
                                                               fake_clock):
    pi.memory.remember("preferences", "tea", "green tea")
    fake_clock.advance(60)
    phone.memory.remember("preferences", "tea", "masala chai")   # newer
    fake_clock.advance(60)
    merge_rounds(pi.engine, phone.engine)      # older device syncs first
    assert pi.memory.load()["preferences"]["tea"]["value"] == "masala chai"


def test_a_discarded_edit_is_recorded_as_a_conflict(phone, pi, fake_clock):
    phone.memory.remember("notes", "gym", "mornings")
    fake_clock.advance(60)
    pi.memory.remember("notes", "gym", "evenings")
    fake_clock.advance(60)
    left, right = merge_rounds(phone.engine, pi.engine)
    assert left.conflicts or right.conflicts


def test_syncing_twice_changes_nothing_the_second_time(phone, pi):
    phone.memory.remember("identity", "city", "Pune")
    merge_rounds(phone.engine, pi.engine)
    before = pi.memory.load()
    _, second = merge_rounds(phone.engine, pi.engine)
    assert pi.memory.load() == before
    assert second.applied == 0


def test_a_fact_deleted_on_one_device_is_not_deleted_everywhere(phone, pi):
    """Trimming is indistinguishable from deleting, so deletions don't travel."""
    phone.memory.remember("notes", "keep", "worth keeping")
    merge_rounds(phone.engine, pi.engine)
    phone.memory.forget("notes", "keep")
    merge_rounds(phone.engine, pi.engine)
    assert pi.memory.load()["notes"]["keep"]["value"] == "worth keeping"


def test_memory_written_before_t_existed_still_syncs(tmp_path, fake_clock):
    """An entry in the old schema has a date and no epoch. It must still move."""
    old = MemoryStore(tmp_path / "old.json", clock=fake_clock)
    old.path.parent.mkdir(parents=True, exist_ok=True)
    old.path.write_text(
        '{"identity": {"name": {"value": "Hrishikesh", "updated": "2020-01-01"}}}',
        encoding="utf-8")
    changes = MemorySync(old, "legacy").changes_since(0.0)
    assert [c.data["value"] for c in changes] == ["Hrishikesh"]
    assert changes[0].ts > 0        # parsed from the date, not left at zero


# ── outcomes: the evidence behind what she has learned ──────────────────────
def test_outcomes_from_both_devices_end_up_in_one_log(phone, pi):
    phone.outcomes.record_outcome("web_search", ok=True, seconds=2.0)
    pi.outcomes.record_outcome("play_music", ok=False, seconds=9.0)
    merge_rounds(phone.engine, pi.engine)
    actions = {r.get("action") for r in phone.outcomes.rows()}
    assert {"web_search", "play_music"} <= actions


def test_the_union_clears_an_evidence_threshold_neither_device_would(phone, pi,
                                                                     fake_clock):
    """Why this is worth syncing at all.

    `preferences()` stays silent below MIN_EVIDENCE rather than guess. Split
    the same decisions across two devices and each stays under it — so she
    learns nothing, forever, from evidence that was always sufficient.
    """
    from flint_core.outcomes import MIN_EVIDENCE

    for i in range(MIN_EVIDENCE):
        target = phone if i % 2 else pi
        target.outcomes.record_decision("which agent", "flint",
                                        "it has the repos", ("cli",))
        fake_clock.advance(1)

    assert phone.outcomes.preferences() == {}      # neither has enough alone
    assert pi.outcomes.preferences() == {}

    merge_rounds(phone.engine, pi.engine)
    assert phone.outcomes.preferences().get("which agent") == "flint"


def test_a_row_relayed_twice_is_stored_once(phone, pi):
    phone.outcomes.record_outcome("web_search", ok=True)
    merge_rounds(phone.engine, pi.engine)
    merge_rounds(phone.engine, pi.engine)
    searches = [r for r in pi.outcomes.rows() if r.get("action") == "web_search"]
    assert len(searches) == 1


def test_a_relayed_row_keeps_the_device_that_made_it(phone, pi):
    """Attribution has to survive the hop, or the row echoes back forever."""
    pi.outcomes.record_outcome("look_around", ok=True)
    merge_rounds(pi.engine, phone.engine)
    relayed = [r for r in phone.outcomes.rows() if r.get("action") == "look_around"]
    assert relayed[0]["device"] == "venom"


def test_dedupe_does_not_reread_the_whole_log_per_change(phone, pi, fake_clock):
    """The index exists so a batch is not quadratic. Prove it is consulted."""
    for _ in range(20):
        pi.outcomes.record_outcome("web_search", ok=True)
        fake_clock.advance(1)
    adapter = OutcomeSync(phone.outcomes, "carnage")
    adapter._index()                     # built once
    reads = {"n": 0}
    original = phone.outcomes.rows

    def counting_rows(*args, **kwargs):
        reads["n"] += 1
        return original(*args, **kwargs)

    phone.outcomes.rows = counting_rows
    for change in OutcomeSync(pi.outcomes, "venom").changes_since(0.0):
        adapter.apply_change(change)
    assert reads["n"] == 0               # never re-read while applying


# ── tied timestamps ─────────────────────────────────────────────────────────
def test_facts_saved_in_the_same_instant_all_survive(phone, pi, fake_clock):
    """A batch write — a contacts import, a loop — shares one timestamp.

    A position held as a bare timestamp cannot tell which of a tied group was
    already sent. Offering only `ts > mark` drops every one but the first, and
    drops it permanently: the mark has moved past it and it is never offered
    again. Nothing reports an error; the fact is simply gone.
    """
    for i in range(5):
        phone.memory.remember("notes", f"imported_{i}", f"contact {i}")
    fake_clock.advance(1)

    merge_rounds(phone.engine, pi.engine)

    landed = pi.memory.load()["notes"]
    assert sorted(landed) == [f"imported_{i}" for i in range(5)]


def test_a_tied_batch_does_not_resend_forever(phone, pi, fake_clock):
    """The other half: the fix must not make the exchange non-terminating."""
    for i in range(3):
        phone.memory.remember("notes", f"tied_{i}", f"value {i}")
    fake_clock.advance(1)
    merge_rounds(phone.engine, pi.engine)

    assert phone.engine.changes_for("venom") == []


def test_a_tied_change_arriving_later_still_goes(phone, pi, fake_clock):
    """A fact written at the same instant as one already synced, but after it."""
    phone.memory.remember("notes", "first", "sent already")
    merge_rounds(phone.engine, pi.engine)

    # Same frozen instant — the clock has not moved.
    phone.memory.remember("notes", "second", "written after the sync")
    merge_rounds(phone.engine, pi.engine)

    assert pi.memory.load()["notes"]["second"]["value"] == "written after the sync"


# ── the whole point: one assistant, three bodies ────────────────────────────
def test_three_devices_converge_through_a_hub(tmp_path, fake_clock):
    """Carnage is the hub: the Pi and the laptop only ever talk to it.

    Neither leaf ever contacts the other — which is the whole reason for a hub,
    since the Pi on a hotspot and a sleeping laptop cannot reliably reach each
    other at all. A fact learned on one leaf still has to reach the other.
    """
    hub = _device(tmp_path / "hub", "carnage", fake_clock)
    pi = _device(tmp_path / "pi", "venom", fake_clock)
    laptop = _device(tmp_path / "laptop", "flint", fake_clock)

    pi.memory.remember("identity", "dog", "Bruno")
    fake_clock.advance(10)
    laptop.memory.remember("preferences", "editor", "neovim")
    fake_clock.advance(10)

    merge_rounds(pi.engine, hub.engine)
    merge_rounds(laptop.engine, hub.engine)
    merge_rounds(pi.engine, hub.engine)

    for device in (pi, laptop, hub):
        loaded = device.memory.load()
        assert loaded["identity"]["dog"]["value"] == "Bruno"
        assert loaded["preferences"]["editor"]["value"] == "neovim"


def test_a_leaf_that_was_off_catches_up_when_it_returns(tmp_path, fake_clock):
    """The offline case is the normal case for a wearable, not the exception."""
    hub = _device(tmp_path / "hub", "carnage", fake_clock)
    pi = _device(tmp_path / "pi", "venom", fake_clock)
    laptop = _device(tmp_path / "laptop", "flint", fake_clock)

    for i in range(5):
        laptop.memory.remember("notes", f"fact_{i}", f"learned while pi was off {i}")
        fake_clock.advance(60)
        merge_rounds(laptop.engine, hub.engine)

    merge_rounds(pi.engine, hub.engine)        # the Pi comes back
    loaded = pi.memory.load()
    assert all(f"fact_{i}" in loaded["notes"] for i in range(5))


# ── notes and people ────────────────────────────────────────────────────────
def _with_stores(root, name, clock):
    from flint_core.stores import ConnectionStore, NoteStore
    root.mkdir(parents=True, exist_ok=True)
    notes = NoteStore(root / "notes.json", clock=clock)
    people = ConnectionStore(root / "people.json")
    engine = build_engine(name, notes=notes, connections=people,
                          state_path=root / "sync.json")
    return notes, people, engine


def test_a_note_taken_on_one_device_is_readable_on_the_other(tmp_path, fake_clock):
    a_notes, _, a = _with_stores(tmp_path / "a", "carnage", fake_clock)
    b_notes, _, b = _with_stores(tmp_path / "b", "venom", fake_clock)

    a_notes.add("bike service due next week")
    merge_rounds(a, b)
    assert [n["text"] for n in b_notes.all()] == ["bike service due next week"]


def test_the_same_note_does_not_arrive_twice(tmp_path, fake_clock):
    a_notes, _, a = _with_stores(tmp_path / "a", "carnage", fake_clock)
    b_notes, _, b = _with_stores(tmp_path / "b", "venom", fake_clock)
    a_notes.add("one note")
    merge_rounds(a, b)
    merge_rounds(a, b)
    assert len(b_notes.all()) == 1


def test_a_person_saved_on_the_phone_is_known_by_the_pi(tmp_path, fake_clock):
    _, a_people, a = _with_stores(tmp_path / "a", "carnage", fake_clock)
    _, b_people, b = _with_stores(tmp_path / "b", "venom", fake_clock)

    a_people.save("Rahul Sharma", phone="919812345678", nickname="rahul bhai")
    merge_rounds(a, b)
    assert b_people.phone_for("rahul bhai") == "919812345678"


def test_the_later_edit_of_a_person_wins(tmp_path, fake_clock):
    _, a_people, a = _with_stores(tmp_path / "a", "carnage", fake_clock)
    _, b_people, b = _with_stores(tmp_path / "b", "venom", fake_clock)

    a_people.save("Rahul", phone="919800000000")
    merge_rounds(a, b)
    b_people.save("Rahul", instagram="rahul.s")      # later, on the Pi
    merge_rounds(a, b)
    assert a_people.find("Rahul")["instagram"] == "rahul.s"
