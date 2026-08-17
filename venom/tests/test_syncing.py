"""The Pi as a leaf: dull when it works, and silent about it when it doesn't.

The behaviour under test is almost entirely the failure behaviour, because
that is what a wearable's network actually does. The rule is that no sync
problem may ever reach the conversation.
"""

from __future__ import annotations

import asyncio

import pytest

from flint_core.memory import MemoryStore
from flint_core.sync import Conflict
from flint_core.syncnet import Exchange, SyncRefused
from venom.config import SyncConfig
from venom.syncing import SyncLoop


@pytest.fixture()
def leaf(tmp_path):
    def build(exchange, **overrides):
        config = SyncConfig(enabled=True, device="venom",
                            hub="ws://carnage.local:8790", token="shared",
                            **overrides)
        return SyncLoop(config, memory=MemoryStore(tmp_path / "memory.json"),
                        state_dir=tmp_path, exchange=exchange)
    return build


# ── config ──────────────────────────────────────────────────────────────────
def test_sync_is_off_until_there_is_a_hub_to_sync_to():
    assert SyncConfig().ready is False
    assert SyncConfig(enabled=True).ready is False          # no hub
    assert SyncConfig(enabled=True, hub="ws://x:1").ready is True


def test_a_silly_interval_is_refused_rather_than_draining_the_battery():
    with pytest.raises(ValueError, match="interval_seconds"):
        SyncConfig(interval_seconds=5)


# ── the ordinary case ───────────────────────────────────────────────────────
def test_a_good_tick_reports_true(leaf):
    loop = leaf(lambda engine, hub, token, relay=None: Exchange(peer="carnage", pushed=2))
    assert asyncio.run(loop.tick()) is True


def test_the_hub_and_token_are_passed_through(leaf):
    seen = {}

    def exchange(engine, hub, token, relay=None):
        seen.update(hub=hub, token=token, device=engine.device)
        return Exchange(peer="carnage")

    asyncio.run(leaf(exchange).tick())
    assert seen == {"hub": "ws://carnage.local:8790", "token": "shared",
                    "device": "venom"}


def test_a_conflict_is_logged_not_swallowed(leaf, caplog):
    result = Exchange(peer="carnage", conflicts=[
        Conflict(store="memory", key="preferences/tea", kept="carnage",
                 discarded="venom", ts=1.0)])
    with caplog.at_level("INFO"):
        asyncio.run(leaf(lambda *a, **k: result).tick())
    assert "preferences/tea" in caplog.text
    assert "discarded venom" in caplog.text


# ── the failure cases, which are the normal ones ────────────────────────────
def test_a_dropped_connection_is_not_fatal(leaf):
    def drops(engine, hub, token, relay=None):
        raise ConnectionError("the hotspot went away")

    assert asyncio.run(leaf(drops).tick()) is False        # and did not raise


def test_a_refusal_is_always_reported_because_it_will_not_self_heal(
        leaf, caplog):
    def refuses(engine, hub, token, relay=None):
        raise SyncRefused("bad token")

    with caplog.at_level("WARNING"):
        for _ in range(10):
            asyncio.run(leaf(refuses).tick())
    assert caplog.text.count("refused by the hub") == 10


def test_a_pi_in_a_drawer_stops_filling_the_journal(leaf, caplog):
    """Repeating the same warning every five minutes for a week helps nobody."""
    def drops(engine, hub, token, relay=None):
        raise ConnectionError("no network")

    loop = leaf(drops)
    with caplog.at_level("WARNING"):
        for _ in range(15):
            asyncio.run(loop.tick())
    assert caplog.text.count("sync failed") == 3        # QUIET_AFTER


def test_recovery_is_announced(leaf, caplog):
    calls = {"n": 0}

    def flaky(engine, hub, token, relay=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ConnectionError("no network")
        return Exchange(peer="carnage")

    loop = leaf(flaky)
    with caplog.at_level("INFO"):
        asyncio.run(loop.tick())
        asyncio.run(loop.tick())
        asyncio.run(loop.tick())
    assert "recovered after 2 failed attempt(s)" in caplog.text


def test_nothing_is_lost_when_a_tick_fails(leaf, tmp_path):
    """The next tick is the retry, and the change is still queued for it."""
    memory = MemoryStore(tmp_path / "memory.json")
    config = SyncConfig(enabled=True, device="venom", hub="ws://x:1")

    def drops(engine, hub, token, relay=None):
        raise ConnectionError("no network")

    loop = SyncLoop(config, memory=memory, state_dir=tmp_path, exchange=drops)
    memory.remember("identity", "name", "Hrishikesh")
    asyncio.run(loop.tick())

    still_queued = loop.engine.changes_for("carnage")
    assert any(c["key"] == "identity/name" for c in still_queued)
