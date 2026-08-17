"""The hub, the leaves, and what happens when the connection drops halfway.

Merging is tested in test_sync*.py. What is tested here is the thing the
network layer exists for: that a failed exchange costs a repeated batch and
never a silently missing one.
"""

from __future__ import annotations

import pytest

from flint_core.memory import MemoryStore
from flint_core.outcomes import OutcomeLog
from flint_core.sync import build_engine
from flint_core.syncnet import (
    PROTOCOL,
    Exchange,
    SyncHub,
    SyncLeaf,
    SyncRefused,
)


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
def hub_device(tmp_path, fake_clock):
    return _device(tmp_path / "carnage", "carnage", fake_clock)


@pytest.fixture()
def pi(tmp_path, fake_clock):
    return _device(tmp_path / "venom", "venom", fake_clock)


@pytest.fixture()
def laptop(tmp_path, fake_clock):
    return _device(tmp_path / "flint", "flint", fake_clock)


class Wire:
    """A `call` that reaches a hub, and can be told to fail on cue."""

    def __init__(self, hub: SyncHub):
        self.hub = hub
        self.sent: list[dict] = []
        self.fail_on: str | None = None
        self.fail_after: int = 0

    def __call__(self, message: dict) -> dict:
        self.sent.append(message)
        if self.fail_on and message.get("type") == self.fail_on:
            if self.fail_after <= 0:
                raise ConnectionError("the hotspot dropped")
            self.fail_after -= 1
        return self.hub.handle(message)

    def count(self, kind: str) -> int:
        return sum(1 for m in self.sent if m.get("type") == kind)


# ── the ordinary case ───────────────────────────────────────────────────────
def test_a_leaf_pushes_and_pulls_in_one_exchange(hub_device, pi):
    pi.memory.remember("identity", "name", "Hrishikesh")
    hub_device.memory.remember("preferences", "tea", "masala chai")

    result = SyncLeaf(pi.engine).exchange(Wire(SyncHub(hub_device.engine)))

    assert result.peer == "carnage"
    assert result.pushed == 1
    assert pi.memory.load()["preferences"]["tea"]["value"] == "masala chai"
    assert hub_device.memory.load()["identity"]["name"]["value"] == "Hrishikesh"


def test_two_leaves_reach_each_other_only_through_the_hub(hub_device, pi, laptop):
    pi.memory.remember("notes", "from_pi", "spoken on the walk")
    laptop.memory.remember("notes", "from_laptop", "typed at the desk")

    wire = Wire(SyncHub(hub_device.engine))
    SyncLeaf(pi.engine).exchange(wire)
    SyncLeaf(laptop.engine).exchange(wire)
    SyncLeaf(pi.engine).exchange(wire)

    assert "from_laptop" in pi.memory.load()["notes"]
    assert "from_pi" in laptop.memory.load()["notes"]


def test_a_second_exchange_moves_nothing(hub_device, pi):
    pi.memory.remember("identity", "city", "Pune")
    wire = Wire(SyncHub(hub_device.engine))
    SyncLeaf(pi.engine).exchange(wire)

    again = SyncLeaf(pi.engine).exchange(wire)
    assert again.pushed == 0
    assert again.applied == 0


def test_the_hub_stops_offering_what_was_acknowledged(hub_device, pi):
    """Without the ack the hub re-sends its last batch on every sync, forever."""
    hub_device.memory.remember("notes", "fact", "already delivered")
    wire = Wire(SyncHub(hub_device.engine))
    SyncLeaf(pi.engine).exchange(wire)

    offered = hub_device.engine.changes_for("venom")
    assert offered == []


# ── the case the layer exists for ───────────────────────────────────────────
def test_a_drop_mid_push_resends_rather_than_losing(hub_device, pi):
    pi.memory.remember("identity", "sister", "Ananya")
    wire = Wire(SyncHub(hub_device.engine))
    wire.fail_on = "sync_push"

    with pytest.raises(ConnectionError):
        SyncLeaf(pi.engine).exchange(wire)

    # Nothing arrived, and — the part that matters — the watermark did not move.
    assert hub_device.memory.load()["identity"] == {}
    assert pi.engine.watermark("carnage") == 0.0

    wire.fail_on = None
    SyncLeaf(pi.engine).exchange(wire)
    assert hub_device.memory.load()["identity"]["sister"]["value"] == "Ananya"


def test_a_drop_mid_pull_resends_rather_than_losing(hub_device, pi):
    hub_device.memory.remember("notes", "important", "do not lose this")
    wire = Wire(SyncHub(hub_device.engine))
    wire.fail_on = "sync_pull"

    with pytest.raises(ConnectionError):
        SyncLeaf(pi.engine).exchange(wire)
    assert pi.memory.load()["notes"] == {}

    wire.fail_on = None
    SyncLeaf(pi.engine).exchange(wire)
    assert pi.memory.load()["notes"]["important"]["value"] == "do not lose this"


def test_a_drop_after_applying_costs_a_repeat_not_a_hole(hub_device, pi):
    """The leaf applied a batch, then died before acknowledging it.

    At-least-once: the batch comes again, and the merge layer absorbs it.
    """
    hub_device.memory.remember("notes", "fact", "arrived once")
    wire = Wire(SyncHub(hub_device.engine))
    wire.fail_on = "sync_pull"
    wire.fail_after = 1          # first pull succeeds, the acking one dies

    with pytest.raises(ConnectionError):
        SyncLeaf(pi.engine).exchange(wire)
    assert pi.memory.load()["notes"]["fact"]["value"] == "arrived once"

    wire.fail_on = None
    again = SyncLeaf(pi.engine).exchange(wire)
    assert again.pulled == 1                       # re-sent, as designed
    assert pi.memory.load()["notes"]["fact"]["value"] == "arrived once"


# ── refusals ────────────────────────────────────────────────────────────────
def test_a_wrong_token_is_refused(hub_device, pi):
    wire = Wire(SyncHub(hub_device.engine, token="the-real-one"))
    with pytest.raises(SyncRefused, match="token"):
        SyncLeaf(pi.engine, token="a-guess").exchange(wire)


def test_the_right_token_is_accepted(hub_device, pi):
    wire = Wire(SyncHub(hub_device.engine, token="shared"))
    pi.memory.remember("notes", "ok", "got through")
    SyncLeaf(pi.engine, token="shared").exchange(wire)
    assert hub_device.memory.load()["notes"]["ok"]["value"] == "got through"


def test_a_protocol_mismatch_is_refused_rather_than_guessed(hub_device, pi):
    hub = SyncHub(hub_device.engine)
    reply = hub.handle({"type": "sync_hello", "device": "venom",
                        "protocol": PROTOCOL + 1})
    assert reply["type"] == "sync_error"


def test_a_device_syncing_with_itself_is_refused(hub_device):
    """Two installs with the same device id would eat each other's watermarks."""
    hub = SyncHub(hub_device.engine)
    reply = hub.handle({"type": "sync_hello", "device": "carnage",
                        "protocol": PROTOCOL})
    assert reply["type"] == "sync_error"


def test_a_nameless_peer_is_refused(hub_device):
    hub = SyncHub(hub_device.engine)
    assert hub.handle({"type": "sync_hello", "protocol": PROTOCOL})["type"] == \
        "sync_error"


def test_rubbish_does_not_take_the_hub_down(hub_device):
    """Every device's memory is behind this loop. It does not get to crash."""
    hub = SyncHub(hub_device.engine)
    for rubbish in ({}, {"type": "nonsense"}, {"type": "sync_push"},
                    {"type": "sync_push", "device": "venom", "changes": "no"},
                    {"type": "sync_pull", "device": "venom", "limit": "lots"}):
        assert hub.handle(rubbish)["type"] == "sync_error"


def test_a_malformed_change_is_dropped_not_fatal(hub_device):
    hub = SyncHub(hub_device.engine)
    reply = hub.handle({"type": "sync_push", "device": "venom",
                        "changes": [{"nonsense": True}, None]})
    assert reply["type"] == "sync_ack"
    assert reply["applied"] == 0


# ── reporting ───────────────────────────────────────────────────────────────
def test_the_exchange_reports_what_moved(hub_device, pi):
    pi.outcomes.record_outcome("web_search", ok=True)
    hub_device.memory.remember("notes", "x", "y")
    result = SyncLeaf(pi.engine).exchange(Wire(SyncHub(hub_device.engine)))
    assert "sent 1" in result.summary()
    assert "received 1" in result.summary()


def test_the_hub_can_report_each_exchange(hub_device, pi):
    seen = []
    hub = SyncHub(hub_device.engine, on_exchange=lambda p, r: seen.append((p, r)))
    pi.memory.remember("notes", "x", "y")
    SyncLeaf(pi.engine).exchange(Wire(hub))
    assert seen and seen[0][0] == "venom"


def test_a_failing_callback_does_not_break_the_sync(hub_device, pi):
    def explode(peer, result):
        raise RuntimeError("the console was not listening")

    hub = SyncHub(hub_device.engine, on_exchange=explode)
    pi.memory.remember("notes", "x", "y")
    SyncLeaf(pi.engine).exchange(Wire(hub))
    assert hub_device.memory.load()["notes"]["x"]["value"] == "y"


def test_an_empty_exchange_is_cheap(hub_device, pi):
    wire = Wire(SyncHub(hub_device.engine))
    SyncLeaf(pi.engine).exchange(wire)
    assert wire.count("sync_push") == 0      # nothing to say, nothing sent
    assert wire.count("sync_pull") == 1
    assert isinstance(Exchange(), Exchange)
