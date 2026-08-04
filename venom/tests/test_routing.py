"""Tests for the PipeWire routing logic (pure parsing parts)."""

from venom.audio.routing import (
    enum_profiles,
    find_bluez_card,
    find_bluez_nodes,
    pick_headset_profile,
)

GRAPH = [
    {"id": 48, "info": {"props": {"media.class": "Audio/Device", "device.api": "alsa"}}},
    {
        "id": 70,
        "info": {
            "props": {"media.class": "Audio/Device", "device.api": "bluez5"},
            "params": {
                "EnumProfile": [
                    {"index": 0, "name": "off"},
                    {"index": 1, "name": "a2dp-sink"},
                    {"index": 2, "name": "headset-head-unit"},
                    {"index": 3, "name": "headset-head-unit-msbc"},
                ]
            },
        },
    },
    {"id": 71, "info": {"props": {"media.class": "Audio/Sink", "device.id": 70}}},
    {"id": 72, "info": {"props": {"media.class": "Audio/Source", "device.id": 70}}},
    {"id": 35, "info": {"props": {"media.class": "Audio/Sink", "device.id": 48}}},
]


def test_find_bluez_card():
    assert find_bluez_card(GRAPH) == 70
    assert find_bluez_card(GRAPH[:1]) is None


def test_enum_profiles():
    names = [p["name"] for p in enum_profiles(GRAPH, 70)]
    assert "headset-head-unit" in names
    assert enum_profiles(GRAPH, 999) == []


def test_pick_headset_profile_prefers_msbc():
    profiles = enum_profiles(GRAPH, 70)
    assert pick_headset_profile(profiles)["name"] == "headset-head-unit-msbc"


def test_pick_headset_profile_none_when_a2dp_only():
    assert pick_headset_profile([{"index": 1, "name": "a2dp-sink"}]) is None


def test_find_bluez_nodes():
    nodes = find_bluez_nodes(GRAPH, 70)
    assert nodes == {"sink": 71, "source": 72}
    assert find_bluez_nodes(GRAPH, 48) == {"sink": 35}


def test_pin_skips_profile_switch_when_mic_already_live(monkeypatch):
    """Re-pinning must not tear down a working link with a profile switch."""
    from venom.audio import routing

    calls: list[list[str]] = []
    monkeypatch.setattr(routing, "pw_dump", lambda: GRAPH)
    monkeypatch.setattr(routing, "_run",
                        lambda args, timeout=10: calls.append(args) or "")

    assert routing.pin_bluetooth_audio(wait=0, attempts=1) is True
    assert all(call[:2] != ["wpctl", "set-profile"] for call in calls)
    defaults = [call for call in calls if call[:2] == ["wpctl", "set-default"]]
    assert {call[2] for call in defaults} == {"71", "72"}


# ── A2DP release: giving the microphone up to get the airtime back ──────────
def test_pick_a2dp_profile_prefers_plain_over_xq():
    from venom.audio.routing import pick_a2dp_profile

    profiles = [
        {"index": 0, "name": "off"},
        {"index": 2, "name": "a2dp-sink-sbc_xq"},
        {"index": 1, "name": "a2dp-sink"},
        {"index": 3, "name": "headset-head-unit"},
    ]
    # sbc_xq is a higher-bitrate SBC — the wrong trade when the entire point
    # of switching to A2DP is to stop spending 2.4GHz airtime.
    assert pick_a2dp_profile(profiles)["name"] == "a2dp-sink"


def test_pick_a2dp_profile_absent():
    from venom.audio.routing import pick_a2dp_profile

    assert pick_a2dp_profile([{"index": 0, "name": "headset-head-unit"}]) is None


def test_a2dp_and_headset_profiles_are_distinct():
    from venom.audio.routing import pick_a2dp_profile

    profiles = enum_profiles(GRAPH, 70)
    a2dp = pick_a2dp_profile(profiles)
    headset = pick_headset_profile(profiles)
    assert a2dp["name"] == "a2dp-sink"
    assert "headset-head-unit" in headset["name"]
    assert a2dp["index"] != headset["index"]


def test_force_skips_the_mic_already_live_fast_path(monkeypatch):
    """A leftover source node (or a stale pw-dump) made the fast path decide
    the mic was live and skip the switch back from A2DP — she chimed and then
    heard nothing. force=True must always issue the profile switch."""
    from venom.audio import routing

    calls: list[list[str]] = []
    monkeypatch.setattr(routing, "pw_dump", lambda: GRAPH)
    monkeypatch.setattr(routing, "_run", lambda args, timeout=10: calls.append(args) or "")
    monkeypatch.setattr(routing.time, "sleep", lambda _s: None)

    # GRAPH has a source (id 72), so the fast path would normally fire.
    assert routing.pin_bluetooth_audio(0, 1, "", False) is True
    assert not any("set-profile" in a for a in calls), "fast path should not switch"

    calls.clear()
    assert routing.pin_bluetooth_audio(0, 1, "", True) is True
    profile_calls = [a for a in calls if "set-profile" in a]
    assert profile_calls, "force must issue a profile switch"
    # and it must target the headset profile, not A2DP
    headset = routing.pick_headset_profile(routing.enum_profiles(GRAPH, 70))
    assert str(headset["index"]) in profile_calls[0]
