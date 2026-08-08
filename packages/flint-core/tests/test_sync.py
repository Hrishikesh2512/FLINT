"""Two devices, one memory — and being honest about what merging discards."""

from __future__ import annotations

import pytest

from flint_core.projects import ProjectStore
from flint_core.recall import FACT, Archive
from flint_core.sync import (
    Change,
    SyncEngine,
    build_engine,
    merge_rounds,
)


@pytest.fixture()
def pi(tmp_path, fake_clock):
    return _device(tmp_path / "pi", "pi", fake_clock)


@pytest.fixture()
def laptop(tmp_path, fake_clock):
    return _device(tmp_path / "laptop", "laptop", fake_clock)


class Device:
    def __init__(self, archive, projects, engine, name):
        self.archive = archive
        self.projects = projects
        self.engine = engine
        self.name = name


def _device(root, name, clock):
    root.mkdir(parents=True, exist_ok=True)
    archive = Archive(root / "archive.db", clock=clock)
    projects = ProjectStore(root / "projects.json", clock=clock)
    engine = build_engine(name, archive=archive, projects=projects,
                          state_path=root / "sync.json")
    return Device(archive, projects, engine, name)


# ── the thing it exists for ─────────────────────────────────────────────────
def test_something_filed_on_one_device_reaches_the_other(pi, laptop):
    pi.archive.remember("Rahul's wedding is in Jaipur", FACT)
    merge_rounds(pi.engine, laptop.engine)
    found = laptop.archive.search("Rahul wedding")
    assert found and "Jaipur" in found[0].text


def test_both_directions_at_once(pi, laptop):
    pi.archive.remember("the kernel landed on Tuesday", FACT)
    laptop.archive.remember("the deploy needs an env var", FACT)
    merge_rounds(pi.engine, laptop.engine)
    assert laptop.archive.search("kernel")
    assert pi.archive.search("env var")


def test_a_task_created_on_the_laptop_shows_up_on_the_pi(pi, laptop):
    laptop.projects.add_task("finish the parser")
    merge_rounds(pi.engine, laptop.engine)
    assert [t["title"] for t in pi.projects.tasks()] == ["finish the parser"]


# ── append-only data cannot conflict ────────────────────────────────────────
def test_the_same_entry_syncing_twice_is_not_two_memories(pi, laptop):
    pi.archive.remember("a thing worth remembering", FACT)
    merge_rounds(pi.engine, laptop.engine)
    merge_rounds(pi.engine, laptop.engine)
    assert len(laptop.archive) == 1


def test_a_replayed_message_is_ignored(pi, laptop):
    pi.archive.remember("a thing", FACT)
    changes = pi.engine.changes_for("laptop")
    laptop.engine.apply(changes, peer="pi")
    laptop.engine.apply(changes, peer="pi")     # the same message again
    assert len(laptop.archive) == 1


def test_concurrent_archive_writes_both_survive(pi, laptop, fake_clock):
    """Append-only means merging is a union, not a choice."""
    pi.archive.remember("what the Pi saw", FACT)
    laptop.archive.remember("what the laptop saw", FACT)
    merge_rounds(pi.engine, laptop.engine)
    assert len(pi.archive) == 2
    assert len(laptop.archive) == 2


# ── keyed data has to choose, and says so ───────────────────────────────────
def test_the_later_edit_wins(pi, laptop, fake_clock):
    task = laptop.projects.add_task("write the docs")
    merge_rounds(pi.engine, laptop.engine)

    fake_clock.advance(100)
    laptop.projects.complete(task["id"])
    merge_rounds(pi.engine, laptop.engine)
    assert pi.projects.tasks() == []            # done, so no longer open


def test_a_conflicting_edit_is_recorded_not_hidden():
    """LWW discards something. The discarding must be visible."""
    class Keyed:
        mode = "keyed"

        def __init__(self):
            self.held = Change(store="s", key="k", data={"v": "mine"},
                               ts=100.0, device="laptop")
            self.applied = []

        def changes_since(self, ts):
            return []

        def current(self, key):
            return self.held

        def apply_change(self, change):
            self.applied.append(change)
            return True

    store = Keyed()
    engine = SyncEngine("pi", {"s": store})
    incoming = Change(store="s", key="k", data={"v": "theirs"}, ts=200.0,
                      device="laptop2").to_dict()
    result = engine.apply([incoming], peer="laptop2")
    assert result.applied == 1
    assert len(result.conflicts) == 1
    assert result.conflicts[0].kept == "laptop2"


def test_an_older_edit_does_not_overwrite_a_newer_one():
    class Keyed:
        mode = "keyed"

        def __init__(self):
            self.applied = []

        def changes_since(self, ts):
            return []

        def current(self, key):
            return Change(store="s", key="k", data={"v": "newer"},
                          ts=500.0, device="laptop")

        def apply_change(self, change):
            self.applied.append(change)
            return True

    store = Keyed()
    engine = SyncEngine("pi", {"s": store})
    stale = Change(store="s", key="k", data={"v": "older"}, ts=100.0,
                   device="phone").to_dict()
    result = engine.apply([stale], peer="phone")
    assert store.applied == []
    assert len(result.conflicts) == 1
    assert result.conflicts[0].kept == "laptop"


def test_ties_resolve_the_same_way_on_both_devices():
    """Without a deterministic tie-break the two stores silently diverge."""
    a = Change(store="s", key="k", data={}, ts=100.0, device="alpha")
    b = Change(store="s", key="k", data={}, ts=100.0, device="beta")
    assert b.wins_against(a) is True
    assert a.wins_against(b) is False


# ── robustness ──────────────────────────────────────────────────────────────
def test_our_own_changes_coming_back_are_ignored(pi):
    mine = Change(store="archive", key="x", data={"text": "mine"},
                  ts=10.0, device="pi").to_dict()
    result = pi.engine.apply([mine], peer="laptop")
    assert result.applied == 0
    assert len(pi.archive) == 0


def test_a_malformed_change_is_dropped_not_fatal(pi):
    result = pi.engine.apply([{"nonsense": True}, {"store": "archive"}],
                             peer="laptop")
    assert result.rejected == 2
    assert result.applied == 0


def test_a_store_this_device_does_not_have_is_skipped(fake_clock, tmp_path):
    archive = Archive(tmp_path / "a.db", clock=fake_clock)
    engine = build_engine("pi", archive=archive)      # no projects at all
    change = Change(store="projects", key="t1", data={"title": "x"},
                    ts=10.0, device="laptop").to_dict()
    result = engine.apply([change], peer="laptop")
    assert result.rejected == 1


def test_a_store_that_throws_does_not_stop_the_sync(pi):
    class Broken:
        mode = "append"

        def changes_since(self, ts):
            raise RuntimeError("the store is gone")

        def apply_change(self, change):
            return False

    pi.engine._stores["broken"] = Broken()
    pi.archive.remember("still works", FACT)
    assert len(pi.engine.changes_for("laptop")) == 1      # archive still listed


def test_a_device_needs_an_id():
    with pytest.raises(ValueError, match="needs an id"):
        SyncEngine("  ", {})


# ── watermarks ──────────────────────────────────────────────────────────────
def test_only_new_changes_are_sent_next_time(pi, laptop, fake_clock):
    pi.archive.remember("first", FACT)
    merge_rounds(pi.engine, laptop.engine)

    fake_clock.advance(10)
    pi.archive.remember("second", FACT)
    outgoing = pi.engine.changes_for("laptop")
    assert len(outgoing) == 1
    assert outgoing[0]["data"]["text"] == "second"


def test_an_ordinary_first_sync_reports_no_conflicts(pi, laptop):
    """Nothing was edited twice, so nothing was discarded."""
    pi.archive.remember("from the pi", FACT)
    laptop.projects.add_task("finish the parser")
    left, right = merge_rounds(pi.engine, laptop.engine)
    assert left.conflicts == [] and right.conflicts == []


def test_a_relayed_change_keeps_its_original_author(pi, laptop):
    """Regression: relaying a change as our own made the device that wrote it
    see a foreign edit of its own task — a conflict that never happened, and
    an echo that bounces back and forth forever."""
    laptop.projects.add_task("finish the parser")
    merge_rounds(pi.engine, laptop.engine)

    relayed = [c for c in pi.engine.changes_for("someone-else")
               if c["store"] == "projects"]
    assert relayed and relayed[0]["device"] == "laptop"


def test_sending_does_not_disturb_what_we_expect_to_receive(pi, laptop):
    """Regression: one watermark served both directions, so sending our own
    changes moved the mark used to decide what to ask for — and the reverse
    direction silently stopped working."""
    pi.archive.remember("from the pi", FACT)
    laptop.archive.remember("from the laptop", FACT)

    outgoing = pi.engine.changes_for("laptop")
    pi.engine.note_sent("laptop", outgoing)          # sending moves _sent only

    assert pi.engine.received_upto("laptop") == 0.0  # not this one
    assert len(laptop.engine.changes_for("pi")) == 1  # laptop still has news


def test_the_two_positions_are_stored_separately(tmp_path, fake_clock):
    root = tmp_path / "d"
    root.mkdir()
    archive = Archive(root / "a.db", clock=fake_clock)
    engine = build_engine("pi", archive=archive, state_path=root / "sync.json")
    engine.note_sent("laptop", [{"ts": 900.0}])
    engine.apply([Change(store="archive", key="k", data={"text": "x"},
                         ts=300.0, device="laptop").to_dict()], peer="laptop")

    reloaded = build_engine("pi", archive=archive, state_path=root / "sync.json")
    assert reloaded.watermark("laptop") == 900.0
    assert reloaded.received_upto("laptop") == 300.0


def test_the_position_survives_a_restart(tmp_path, fake_clock):
    root = tmp_path / "pi"
    root.mkdir()
    archive = Archive(root / "a.db", clock=fake_clock)
    first = build_engine("pi", archive=archive, state_path=root / "sync.json")
    first.note_sent("laptop", [{"ts": 500.0}])

    second = build_engine("pi", archive=archive, state_path=root / "sync.json")
    assert second.watermark("laptop") == 500.0


def test_a_result_describes_itself(pi, laptop):
    pi.archive.remember("something", FACT)
    left, _ = merge_rounds(pi.engine, laptop.engine)
    assert "sent 1" in left.summary()
