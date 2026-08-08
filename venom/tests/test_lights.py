"""Tuya smart-light control — a fake bulb driver records commands, so the
resolution/colour/scene logic is exercised with no hardware or tinytuya."""

import json

from venom.config import VenomConfig, load_config
from venom.lights import LightsController


class FakeBulb:
    """Records the tinytuya calls the controller makes; can play dead."""

    def __init__(self, dev_id, ip, key, fail=False):
        self.dev_id, self.ip, self.key = dev_id, ip, key
        self.fail = fail
        self.version = None
        self.calls: list = []

    def set_version(self, v):
        self.version = v

    def _rec(self, call):
        if self.fail:
            raise RuntimeError("bulb offline")
        self.calls.append(call)

    def turn_on(self):
        self._rec("on")

    def turn_off(self):
        self._rec("off")

    def set_brightness_percentage(self, p):
        self._rec(("brightness", p))

    def set_colour(self, r, g, b):
        self._rec(("colour", r, g, b))

    def set_colourtemp_percentage(self, t):
        self._rec(("temp", t))


class Factory:
    def __init__(self, fail_ids=()):
        self.made: dict[str, FakeBulb] = {}
        self.fail_ids = set(fail_ids)

    def __call__(self, dev_id, ip, key):
        bulb = FakeBulb(dev_id, ip, key, fail=dev_id in self.fail_ids)
        self.made[dev_id] = bulb
        return bulb


DEVICES = [
    {"name": "bedroom lamp", "id": "bed1", "key": "k1", "ip": "10.0.0.11",
     "version": 3.3, "room": "bedroom"},
    {"name": "kitchen", "id": "kit1", "local_key": "k2", "address": "10.0.0.12",
     "ver": 3.4, "room": "kitchen"},
    {"name": "hall strip", "id": "hall1", "key": "k3"},  # no ip → "Auto"
]


def make(tmp_path, devices=DEVICES, wrap=None, factory=None):
    path = tmp_path / "lights.json"
    payload = devices if wrap is None else {wrap: devices}
    path.write_text(json.dumps(payload))
    factory = factory or Factory()
    return LightsController(path, bulb_factory=factory), factory


# ── registry parsing ──────────────────────────────────────────────────────────
def test_registry_accepts_list_or_devices_wrapper(tmp_path):
    c1, _ = make(tmp_path)
    c2, _ = make(tmp_path, wrap="devices")
    assert c1.has_devices() and c2.has_devices()
    assert len(c1._load()) == 3


def test_registry_normalises_key_ip_version_aliases(tmp_path):
    c, _ = make(tmp_path)
    devs = {d["name"]: d for d in c._load()}
    assert devs["kitchen"]["key"] == "k2"          # local_key alias
    assert devs["kitchen"]["ip"] == "10.0.0.12"    # address alias
    assert devs["kitchen"]["version"] == 3.4       # ver alias
    assert devs["hall strip"]["ip"] == "Auto"      # missing ip → Auto


def test_bulb_missing_key_is_dropped(tmp_path):
    c, _ = make(tmp_path, devices=[{"name": "x", "id": "x1"}])  # no key
    assert c.has_devices() is False


def test_missing_file_has_no_devices(tmp_path):
    c = LightsController(tmp_path / "nope.json", bulb_factory=Factory())
    assert c.has_devices() is False
    assert "no lights" in c.list_lights().lower()


# ── resolution ────────────────────────────────────────────────────────────────
def test_empty_where_targets_all_lights(tmp_path):
    c, f = make(tmp_path)
    out = c.power(True, "")
    assert {"bed1", "kit1", "hall1"} == set(f.made)
    assert all("on" in b.calls for b in f.made.values())
    assert "all the lights" in out


def test_resolve_by_room_and_by_name(tmp_path):
    c, f = make(tmp_path)
    assert "turned off bedroom" in c.power(False, "bedroom").lower()
    assert f.made["bed1"].calls == ["off"]
    assert "kit1" not in f.made  # kitchen bulb never touched

    c2, f2 = make(tmp_path)
    c2.power(True, "hall strip")
    assert set(f2.made) == {"hall1"}


def test_unknown_where_is_reported(tmp_path):
    c, f = make(tmp_path)
    out = c.power(True, "garage")
    assert "couldn't find any light" in out.lower()
    assert f.made == {}


# ── brightness / colour / scene ───────────────────────────────────────────────
def test_brightness_clamps_and_turns_on(tmp_path):
    c, f = make(tmp_path)
    c.brightness(500, "bedroom")           # clamps to 100
    assert f.made["bed1"].calls == ["on", ("brightness", 100)]
    c2, f2 = make(tmp_path)
    c2.brightness(0, "bedroom")            # clamps up to 1
    assert ("brightness", 1) in f2.made["bed1"].calls


def test_named_colour_sets_rgb(tmp_path):
    c, f = make(tmp_path)
    c.colour("blue", "kitchen")
    assert ("colour", 0, 60, 255) in f.made["kit1"].calls


def test_white_preset_uses_colour_temp(tmp_path):
    c, f = make(tmp_path)
    c.colour("warm", "kitchen")
    assert ("temp", 0) in f.made["kit1"].calls
    c2, f2 = make(tmp_path)
    c2.colour("cool", "kitchen")
    assert ("temp", 100) in f2.made["kit1"].calls


def test_unknown_colour_is_rejected(tmp_path):
    c, f = make(tmp_path)
    out = c.colour("chartreuse", "kitchen")
    assert "don't know the colour" in out.lower()
    assert f.made == {}  # nothing sent


def test_scene_applies_colour_and_brightness(tmp_path):
    c, f = make(tmp_path)
    c.scene("movie", "bedroom")
    calls = f.made["bed1"].calls
    assert "on" in calls and ("colour", 80, 0, 160) in calls
    assert ("brightness", 20) in calls


def test_unknown_scene_lists_options(tmp_path):
    c, _ = make(tmp_path)
    out = c.scene("disco", "")
    assert "movie" in out and "reading" in out


# ── partial failure ───────────────────────────────────────────────────────────
def test_dead_bulb_is_reported_not_raised(tmp_path):
    c, f = make(tmp_path, factory=Factory(fail_ids={"kit1"}))
    out = c.power(True, "")  # bed1+hall1 succeed, kit1 fails
    assert "didn't respond" in out and "kitchen" in out
    assert f.made["bed1"].calls == ["on"]


# ── config + registry wiring ──────────────────────────────────────────────────
def test_lights_config_ready_only_with_a_keyed_bulb(tmp_path):
    assert VenomConfig().lights.ready is False  # default path won't exist

    reg = tmp_path / "lights.json"
    reg.write_text(json.dumps(DEVICES))
    path = tmp_path / "venom.toml"
    path.write_text(f'[lights]\nregistry_path = "{reg.as_posix()}"\n')
    config = load_config(path)
    assert config.lights.ready is True
    assert config.lights.registry_path == reg


def test_light_tools_registered_only_when_present(tmp_path):
    from flint_core.memory import MemoryStore
    from venom.tools_pi import TimerBoard, build_pi_registry

    base = dict(gemini_api_key="k", memory_path=tmp_path / "m.json")
    mem = MemoryStore(base["memory_path"])

    off = build_pi_registry(VenomConfig(**base), mem, TimerBoard())
    assert "set_lights" not in off.names()

    controller, _ = make(tmp_path)
    on = build_pi_registry(VenomConfig(**base), mem, TimerBoard(),
                           lights=controller)
    for name in ("set_lights", "set_light_brightness", "set_light_color",
                 "set_light_scene", "list_lights"):
        assert name in on.names()
