"""Wiring: what this body offers, and the Pi actually syncing to it.

The last test here is the one that matters most — it drives the real hub over
the real JSON wire format, with a Venom-shaped device on the other end, and
checks that a fact learned on the Pi is a fact the phone knows.
"""

from __future__ import annotations

import json

import pytest
from flint_core.memory import MemoryStore
from flint_core.outcomes import OutcomeLog
from flint_core.recall import Archive
from flint_core.sync import build_engine
from flint_core.syncnet import SyncLeaf
from flint_core.syncws import SyncServer

from carnage.capabilities import build_capabilities
from carnage.config import CarnageConfig, HubConfig, load_config
from carnage.platform import AbsentPhone, Battery, Fix
from carnage.runtime import Carnage


class StubPhone:
    name = "stub"

    def available(self):
        return True

    def battery(self):
        return Battery(80, charging=False)

    def locate(self):
        return Fix(18.5, 73.8, accuracy=10.0)

    def send_sms(self, to, text):
        return True

    def notifications(self):
        return []

    def vibrate(self, milliseconds=400):
        return True


@pytest.fixture()
def config(tmp_path):
    return CarnageConfig(device="carnage", state_dir=tmp_path / "state",
                         hub=HubConfig(enabled=False))


# ── capabilities ────────────────────────────────────────────────────────────
def test_a_body_with_no_phone_offers_no_phone_skills(config):
    caps = build_capabilities(config, phone=AbsentPhone())
    assert "phone" not in caps.names()
    assert "sms" not in caps.names()
    assert "emergency_sms" not in caps.names()


def test_a_real_phone_offers_them(config):
    caps = build_capabilities(config, phone=StubPhone())
    assert {"phone", "sms"} <= set(caps.names())


def test_sos_by_sms_needs_a_contact_book_as_well_as_a_radio(config):
    assert "emergency_sms" not in build_capabilities(
        config, phone=StubPhone()).names()

    class Sos:
        def contacts(self, enabled_only=False):
            return [{"name": "Papa", "to": "+9111"}]

    assert "emergency_sms" in build_capabilities(
        config, phone=StubPhone(), sos=Sos()).names()


def test_the_shared_skills_are_the_pi_s_own_code(config, tmp_path):
    """Not a copy — literally the registrars Venom calls."""
    archive = Archive(tmp_path / "a.db")
    caps = build_capabilities(config, phone=AbsentPhone(), archive=archive)
    registry = caps.build_registry()
    assert {"remember_about", "file_away", "forget_about"} <= set(registry.names())


def test_an_inactive_capability_contributes_no_prompt_text(config):
    """A phone with no SMS must not be told how to send one."""
    caps = build_capabilities(config, phone=AbsentPhone())
    assert "TEXT MESSAGES" not in caps.render_prompt()

    caps = build_capabilities(config, phone=StubPhone())
    assert "TEXT MESSAGES" in caps.render_prompt()


def test_phone_tools_inherit_their_capability_s_permissions(config):
    registry = build_capabilities(config, phone=StubPhone()).build_registry()
    assert "messaging" in registry.get("send_text").permissions
    # And the read-only half is not quietly granted the same thing.
    assert "messaging" not in registry.get("phone_battery").permissions


# ── config ──────────────────────────────────────────────────────────────────
def test_a_missing_config_file_yields_defaults(tmp_path):
    loaded = load_config(tmp_path / "nope.json")
    assert loaded.device
    assert loaded.hub.port == 8790


def test_a_broken_config_file_does_not_stop_the_phone_starting(tmp_path):
    """A stray comma must not be the reason he has no assistant."""
    path = tmp_path / "carnage.json"
    path.write_text("{broken,", encoding="utf-8")
    assert load_config(path).device


def test_config_is_read_when_it_is_there(tmp_path):
    path = tmp_path / "carnage.json"
    path.write_text(json.dumps({
        "device": "carnage-pixel", "user_name": "Hrishikesh",
        "hub": {"port": 9001, "token": "shared", "peers": ["venom"]},
        "repos": [["flint", "/data/flint"]],
    }), encoding="utf-8")
    loaded = load_config(path)
    assert loaded.device == "carnage-pixel"
    assert loaded.hub.port == 9001
    assert loaded.hub.peers == ("venom",)
    assert loaded.repo_path("flint") == "/data/flint"
    assert loaded.default_repo == "/data/flint"


def test_the_config_satisfies_the_workspace_protocol():
    """flint_core.skills.dev works against it with no adapter."""
    from flint_core.skills import Workspace

    assert isinstance(CarnageConfig(repos=(("a", "/b"),)), Workspace)


# ── the runtime ─────────────────────────────────────────────────────────────
def test_carnage_builds_and_describes_itself(config):
    carnage = Carnage(config, phone=StubPhone())
    assert "carnage" in carnage.describe()
    assert len(list(carnage.registry)) > 0


def test_the_sync_engine_holds_the_same_store_objects(config):
    """Two MemoryStores on one path means two locks and interleaved writes."""
    carnage = Carnage(config, phone=AbsentPhone())
    carnage.memory.remember("identity", "name", "Hrishikesh")
    changes = carnage.sync.changes_for("venom")
    assert any(c["key"] == "identity/name" for c in changes)


def test_a_sync_conflict_becomes_something_she_can_explain(config):
    """'Why did you do that?' should reach a discarded edit, not just a log."""
    from flint_core.sync import Conflict, SyncResult

    carnage = Carnage(config, phone=AbsentPhone())
    carnage._note_exchange("venom", SyncResult(conflicts=[
        Conflict(store="memory", key="preferences/tea", kept="venom",
                 discarded="carnage", ts=1000.0)]))
    assert "preferences/tea" in carnage.outcomes.explain("which edit wins")


# ── the hub, over the actual wire format ────────────────────────────────────
def test_bad_json_is_answered_not_crashed_on(config):
    server = SyncServer(Carnage(config, phone=AbsentPhone()).sync)
    assert json.loads(server.handle_raw("{not json"))["type"] == "sync_error"
    assert json.loads(server.handle_raw('"a string"'))["type"] == "sync_error"


def test_a_device_off_the_peer_list_is_refused(config):
    server = SyncServer(Carnage(config, phone=AbsentPhone()).sync,
                        peers=("venom",))
    reply = json.loads(server.handle_raw(json.dumps(
        {"type": "sync_hello", "device": "someone-elses-pi", "protocol": 1})))
    assert reply["type"] == "sync_error"
    assert "peer list" in reply["reason"]


def test_a_listed_device_gets_through(config):
    server = SyncServer(Carnage(config, phone=AbsentPhone()).sync,
                        peers=("venom",))
    reply = json.loads(server.handle_raw(json.dumps(
        {"type": "sync_hello", "device": "venom", "protocol": 1})))
    assert reply["type"] == "sync_ready"


def test_the_pi_syncs_a_fact_to_the_phone_over_the_wire(config, tmp_path):
    """End to end: a Venom-shaped leaf, the real hub, real JSON both ways."""
    carnage = Carnage(config, phone=StubPhone())
    server = SyncServer(carnage.sync, token="shared")

    pi_root = tmp_path / "venom"
    pi_root.mkdir(parents=True, exist_ok=True)
    pi_memory = MemoryStore(pi_root / "memory.json")
    pi_outcomes = OutcomeLog(pi_root / "outcomes.jsonl")
    pi_engine = build_engine("venom", memory=pi_memory, outcomes=pi_outcomes,
                             state_path=pi_root / "sync.json")

    pi_memory.remember("relationships", "ananya", "his sister, in Bengaluru")
    carnage.memory.remember("preferences", "tea", "masala chai, no sugar")

    def wire(message: dict) -> dict:
        # Exactly what a websocket would carry: a JSON string each way.
        return json.loads(server.handle_raw(json.dumps(message)))

    result = SyncLeaf(pi_engine, token="shared").exchange(wire)

    assert result.peer == "carnage"
    assert carnage.memory.load()["relationships"]["ananya"]["value"] == \
        "his sister, in Bengaluru"
    assert pi_memory.load()["preferences"]["tea"]["value"] == \
        "masala chai, no sugar"


def test_the_wrong_token_gets_nothing(config, tmp_path):
    carnage = Carnage(config, phone=AbsentPhone())
    server = SyncServer(carnage.sync, token="shared")
    pi_engine = build_engine("venom", memory=MemoryStore(tmp_path / "m.json"),
                             state_path=tmp_path / "s.json")

    from flint_core.syncnet import SyncRefused

    def wire(message):
        return json.loads(server.handle_raw(json.dumps(message)))

    with pytest.raises(SyncRefused):
        SyncLeaf(pi_engine, token="wrong").exchange(wire)
