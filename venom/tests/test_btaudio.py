"""Bluetooth automation tests — fake bluetoothctl runner + scanner, no hardware."""

import pytest

from venom.btaudio import BluetoothHeadset, normalize_mac, parse_devices, parse_info
from venom.config import AudioConfig, load_config

MAC = "AA:BB:CC:DD:EE:FF"

INFO_IN_RANGE_FRESH = """Device AA:BB:CC:DD:EE:FF (public)
	Name: My Buds
	Paired: no
	Trusted: no
	Connected: no
	RSSI: -55
"""

INFO_IN_RANGE_PAIRED = INFO_IN_RANGE_FRESH.replace(
    "Paired: no", "Paired: yes").replace("Trusted: no", "Trusted: yes")

INFO_CONNECTED = INFO_IN_RANGE_PAIRED.replace("Connected: no", "Connected: yes")

INFO_OUT_OF_RANGE = """Device AA:BB:CC:DD:EE:FF (public)
	Name: My Buds
	Paired: no
	Trusted: no
	Connected: no
"""

DEVICES_OUT = """Device AA:BB:CC:DD:EE:FF My Buds
Device 11:22:33:44:55:66 Some TV
"""


class FakeScan:
    def __init__(self):
        self.terminated = False

    def terminate(self):
        self.terminated = True


def fake_scanner_factory(created: list):
    def factory():
        scan = FakeScan()
        created.append(scan)
        return scan
    return factory


class ScriptedRunner:
    """Replays canned `info` outputs in order; records all commands."""

    def __init__(self, info_sequence):
        self.calls = []
        self.info_sequence = list(info_sequence)

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[0] == "info":
            return self.info_sequence.pop(0) if self.info_sequence else INFO_CONNECTED
        if args[0] == "devices":
            return DEVICES_OUT
        return "ok"

    def flat(self):
        return [" ".join(c) for c in self.calls]


def make(runner, mac=MAC, name=""):
    scans = []
    headset = BluetoothHeadset(mac=mac, name=name, runner=runner,
                               scanner=fake_scanner_factory(scans))
    return headset, scans


def test_normalize_mac():
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == MAC
    with pytest.raises(ValueError):
        normalize_mac("not-a-mac")


def test_parse_devices():
    devices = parse_devices(DEVICES_OUT)
    assert devices[MAC] == "My Buds"
    assert len(devices) == 2


def test_parse_info_in_range_flag():
    assert parse_info(INFO_IN_RANGE_FRESH)["in_range"] is True
    assert parse_info(INFO_OUT_OF_RANGE)["in_range"] is False
    assert parse_info(INFO_CONNECTED)["connected"] is True


def test_direct_connect_when_idle_no_pairing_mode():
    # not connected -> direct page connect succeeds (real-hardware behavior)
    runner = ScriptedRunner([
        INFO_OUT_OF_RANGE,   # initial status
        INFO_CONNECTED,      # status after the direct connect attempt
    ])
    headset, scans = make(runner)
    assert headset.ensure_connected() is True
    assert scans == []  # never needed discovery
    assert any(c.startswith("connect") for c in runner.flat())


def test_full_pairing_flow_with_continuous_scan():
    # not connected -> direct connect fails -> visible in range -> pair -> connect
    runner = ScriptedRunner([
        INFO_OUT_OF_RANGE,     # initial status: not connected
        INFO_OUT_OF_RANGE,     # status after failed direct connect
        INFO_IN_RANGE_FRESH,   # wait_visible poll: broadcasting now
        INFO_IN_RANGE_FRESH,   # state before pair
        INFO_IN_RANGE_PAIRED,  # connected check before connect
        INFO_CONNECTED,        # final status
    ])
    headset, scans = make(runner)
    assert headset.ensure_connected() is True
    flat = runner.flat()
    assert any(c.startswith("pair") for c in flat)
    assert any(c.startswith("trust") for c in flat)
    assert any(c.startswith("connect") for c in flat)
    assert scans and scans[0].terminated  # continuous scan started and stopped
    assert "scan off" in flat


def test_already_connected_fast_path():
    runner = ScriptedRunner([INFO_CONNECTED])
    headset, scans = make(runner)
    assert headset.ensure_connected() is True
    assert scans == []  # no scan needed
    assert not any(c.startswith("pair") for c in runner.flat())


def test_not_broadcasting_returns_false_quickly():
    runner = ScriptedRunner([INFO_OUT_OF_RANGE] + [INFO_OUT_OF_RANGE] * 50)
    headset, _ = make(runner)
    now = [0.0]

    def clock():
        return now[0]

    def sleep(seconds):
        now[0] += seconds

    assert headset.wait_visible(timeout=10, poll=2, sleep=sleep, clock=clock) is False


def test_discovery_by_name_during_scan():
    runner = ScriptedRunner([INFO_IN_RANGE_FRESH])
    headset, _ = make(runner, mac="", name="my buds")
    assert headset.wait_visible(timeout=10, poll=1, sleep=lambda s: None) is True
    assert headset.mac == MAC


def test_wait_for_connection_retries():
    calls = []

    class Flaky(ScriptedRunner):
        def __call__(self, args, timeout):
            if args[0] == "info":
                calls.append(1)
                return INFO_CONNECTED if len(calls) > 3 else INFO_OUT_OF_RANGE
            return super().__call__(args, timeout)

    headset, _ = make(Flaky([]))
    # patch wait_visible to avoid the long scan in this unit test
    headset.wait_visible = lambda **kw: True
    assert headset.wait_for_connection(attempts=4, delay=0, sleep=lambda s: None)


def test_needs_mac_or_name():
    with pytest.raises(ValueError):
        BluetoothHeadset()


# ── config + device selection integration (unchanged behavior) ───────────────
def test_audio_config_modes():
    assert not AudioConfig().use_bluetooth
    assert AudioConfig(bluetooth_mac=MAC).use_bluetooth
    assert AudioConfig(output="bluetooth").use_bluetooth
    assert not AudioConfig(output="usb", bluetooth_mac=MAC).use_bluetooth
    with pytest.raises(ValueError):
        AudioConfig(output="loudspeaker")


def test_audio_config_from_toml(tmp_path):
    path = tmp_path / "venom.toml"
    path.write_text(
        '[audio]\nbluetooth_mac = "AA:BB:CC:DD:EE:FF"\nbluetooth_name = "My Buds"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.audio.bluetooth_mac == MAC
    assert config.audio.use_bluetooth


def test_device_pick_bluetooth_prefers_pipewire():
    from venom.audio.devices import pick_devices

    table = [
        {"name": "bcm2835 Headphones", "max_input_channels": 0, "max_output_channels": 2},
        {"name": "pipewire", "max_input_channels": 32, "max_output_channels": 32},
        {"name": "USB PnP Sound Device", "max_input_channels": 1, "max_output_channels": 2},
    ]
    bt = pick_devices(table, bluetooth=True)
    assert bt.input_index == 1 and bt.output_index == 1
    # USB mode also routes through PipeWire now (it resamples our rates that a
    # raw USB DAC open rejects); the USB node is pinned as default separately.
    usb = pick_devices(table, bluetooth=False)
    assert usb.input_index == 1
