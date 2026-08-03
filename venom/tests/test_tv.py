"""Samsung TV control — a fake WebSocket client records the keys and app
launches, so power/volume/app logic is exercised with no hardware, no network
and no samsungtvws installed."""

import pytest

from venom.config import TVConfig, load_config
from venom.tv import TVController, _send_magic_packet


class FakeTV:
    """Records the samsungtvws calls the controller makes; can play dead."""

    def __init__(self, *, fail=False, apps=None, deep_link=True, info=None, **kw):
        self.kw = kw
        self.fail = fail
        self.keys: list[str] = []
        self.launched: list[tuple] = []
        self._apps = apps
        self._deep_link = deep_link
        self._info = info

    def _live(self):
        if self.fail:
            raise OSError("TV unreachable")

    def send_key(self, key):
        self._live()
        self.keys.append(key)

    def app_list(self):
        self._live()
        if self._apps is None:
            raise OSError("no app list")
        return self._apps

    def run_app(self, app_id, *args):
        self._live()
        if args and not self._deep_link:
            raise TypeError("run_app() takes 2 positional arguments")
        self.launched.append((app_id, *args))

    def rest_device_info(self):
        self._live()
        if self._info is None:
            raise OSError("no info")
        return self._info


class Factory:
    """Hands out one shared FakeTV, so calls accumulate across commands."""

    def __init__(self, **kw):
        self.tv = None
        self.kw = kw
        self.built = 0

    def __call__(self, **kw):
        self.built += 1
        if self.tv is None:
            self.tv = FakeTV(**self.kw, **kw)
        return self.tv


APPS = [
    {"appId": "111299001912", "name": "YouTube"},
    {"appId": "3201907018807", "name": "Netflix"},
    {"appId": "3201910019365", "name": "Prime Video"},
]


def build(tmp_path, wol=None, upnp=None, **kw):
    factory = Factory(**kw)
    sent: list[str] = []
    calls: list[tuple] = []

    def _wol(mac):
        sent.append(mac)

    def _upnp(service, action, args):
        calls.append((service, action, args))
        return upnp

    tv = TVController("10.0.0.5", mac="aa:bb:cc:dd:ee:ff",
                      token_path=tmp_path / "token.txt",
                      client_factory=factory,
                      wol_sender=wol or _wol,
                      upnp_call=_upnp)
    return tv, factory, sent, calls


# ── power ────────────────────────────────────────────────────────────────────
def test_power_on_uses_wake_on_lan_not_a_key(tmp_path):
    tv, factory, sent, _ = build(tmp_path)
    reply = tv.power(True)
    assert sent == ["aa:bb:cc:dd:ee:ff"]  # WoL, because the WS server is off
    assert factory.tv is None or factory.tv.keys == []
    assert "waking" in reply.lower()


def test_power_on_without_mac_explains_itself(tmp_path):
    tv = TVController("10.0.0.5", token_path=tmp_path / "t.txt",
                      client_factory=Factory())
    reply = tv.power(True)
    assert "mac" in reply.lower()


def test_power_off_sends_the_power_key(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    assert "off" in tv.power(False).lower()
    assert factory.tv.keys == ["KEY_POWER"]


def test_dead_tv_degrades_to_a_sentence(tmp_path):
    tv, _, _, _ = build(tmp_path, fail=True)
    assert "couldn't reach" in tv.power(False).lower()


# ── volume ───────────────────────────────────────────────────────────────────
def test_nudge_volume_repeats_the_key(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    tv.nudge_volume("up", 3)
    assert factory.tv.keys == ["KEY_VOLUP"] * 3


def test_nudge_volume_understands_hinglish(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    tv.nudge_volume("kam", 2)
    assert factory.tv.keys == ["KEY_VOLDOWN"] * 2


def test_nudge_volume_clamps_step_count(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    tv.nudge_volume("up", 500)
    assert len(factory.tv.keys) == 30


def test_set_volume_speaks_upnp_and_clamps(tmp_path):
    tv, _, _, calls = build(tmp_path, upnp="<ok/>")
    reply = tv.set_volume(150)
    assert calls == [("RenderingControl", "SetVolume",
                      {"InstanceID": 0, "Channel": "Master", "DesiredVolume": 100})]
    assert "100" in reply


def test_set_volume_without_upnp_says_so(tmp_path):
    tv, _, _, _ = build(tmp_path, upnp=None)
    assert "exact volume" in tv.set_volume(20)


def test_mute_toggle_uses_the_key(tmp_path):
    tv, factory, _, calls = build(tmp_path)
    assert "toggled" in tv.mute().lower()
    assert factory.tv.keys == ["KEY_MUTE"]
    assert calls == []


def test_explicit_mute_prefers_upnp(tmp_path):
    tv, factory, _, calls = build(tmp_path, upnp="<ok/>")
    assert tv.mute(True) == "Muted the TV."
    assert calls[0][1] == "SetMute"
    assert factory.tv is None or factory.tv.keys == []


def test_explicit_mute_falls_back_to_toggle_and_admits_it(tmp_path):
    tv, factory, _, _ = build(tmp_path, upnp=None)
    reply = tv.mute(True)
    assert factory.tv.keys == ["KEY_MUTE"]
    assert "can't tell" in reply


# ── keys ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("spoken,code", [
    ("pause", "KEY_PAUSE"), ("aage", "KEY_FF"), ("ok", "KEY_ENTER"),
    ("back", "KEY_RETURN"), ("home", "KEY_HOME"), ("channel up", "KEY_CHUP"),
])
def test_friendly_key_names_map_to_remote_codes(tmp_path, spoken, code):
    tv, factory, _, _ = build(tmp_path)
    tv.press(spoken)
    assert factory.tv.keys == [code]


def test_unknown_key_lists_what_it_knows(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    reply = tv.press("eject")
    assert "don't know" in reply
    assert factory.tv is None  # never touched the TV


def test_press_repeats(tmp_path):
    tv, factory, _, _ = build(tmp_path)
    tv.press("forward", 4)
    assert factory.tv.keys == ["KEY_FF"] * 4


# ── apps ─────────────────────────────────────────────────────────────────────
def test_launch_prefers_the_tvs_own_app_list(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=APPS)
    assert "Netflix" in tv.launch_app("netflix")
    assert factory.tv.launched == [("3201907018807",)]


def test_launch_resolves_spoken_aliases(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=APPS)
    tv.launch_app("amazon prime")
    assert factory.tv.launched == [("3201910019365",)]


def test_launch_falls_back_to_known_ids_when_list_is_unavailable(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=None)  # app_list() raises
    tv.launch_app("youtube")
    assert factory.tv.launched == [("111299001912",)]


def test_app_with_no_published_id_resolves_off_the_tvs_list(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=[*APPS, {"appId": "999", "name": "Hotstar"}])
    tv.launch_app("jio hotstar")  # alias → hotstar → matched by name
    assert factory.tv.launched == [("999",)]


def test_app_with_no_published_id_never_guesses_when_list_is_unavailable(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=None)
    assert "couldn't find" in tv.launch_app("hotstar")
    assert factory.tv.launched == []  # better to say so than open something random


def test_unknown_app_is_reported_not_guessed(tmp_path):
    tv, _, _, _ = build(tmp_path, apps=APPS)
    assert "couldn't find" in tv.launch_app("blockbuster")


def test_app_list_is_cached_across_calls(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=APPS)
    tv.list_apps()
    tv.list_apps()
    assert factory.tv.launched == []
    assert "Netflix" in tv.list_apps()


# ── titles ───────────────────────────────────────────────────────────────────
def test_play_title_deep_links_when_supported(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=APPS, deep_link=True)
    reply = tv.play_title("Dune", "netflix")
    assert factory.tv.launched == [("3201907018807", "DEEP_LINK", "Dune")]
    assert "Playing Dune" in reply


def test_play_title_without_deep_link_opens_app_and_is_honest(tmp_path):
    tv, factory, _, _ = build(tmp_path, apps=APPS, deep_link=False)
    reply = tv.play_title("Dune", "netflix")
    assert factory.tv.launched == [("3201907018807",)]  # plain launch
    assert "can't jump straight" in reply
    assert "Playing" not in reply  # never claims playback it didn't start


def test_play_title_needs_a_title(tmp_path):
    tv, _, _, _ = build(tmp_path)
    assert tv.play_title("  ") == "What should I play?"


# ── status ───────────────────────────────────────────────────────────────────
def test_status_reads_power_state(tmp_path):
    tv, _, _, _ = build(tmp_path, info={"device": {"name": "Living Room",
                                                   "PowerState": "standby"}})
    assert tv.status() == "Living Room is in standby."


def test_status_when_off(tmp_path):
    tv, _, _, _ = build(tmp_path, fail=True)
    assert "probably off" in tv.status()


# ── wake-on-lan ──────────────────────────────────────────────────────────────
def test_magic_packet_rejects_a_bad_mac():
    with pytest.raises(ValueError, match="bad MAC"):
        _send_magic_packet("not-a-mac")


# ── config ───────────────────────────────────────────────────────────────────
def test_tv_is_not_ready_until_a_host_is_set():
    assert not TVConfig().ready
    assert TVConfig(host="10.0.0.5").ready
    assert not TVConfig(host="10.0.0.5", enabled=False).ready


def test_config_loads_the_tv_section(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text(
        '[tv]\nhost = "10.0.0.5"\nmac = "AA:BB:CC:DD:EE:FF"\n'
        'token_path = "/tmp/tok"\nport = 8001\n',
        encoding="utf-8")
    cfg = load_config(path)
    assert cfg.tv.host == "10.0.0.5"
    assert cfg.tv.mac == "AA:BB:CC:DD:EE:FF"
    assert cfg.tv.port == 8001
    assert cfg.tv.ready


def test_missing_tv_section_defaults_to_off(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text("[venom]\npoll_interval = 30\n", encoding="utf-8")
    assert not load_config(path).tv.ready
