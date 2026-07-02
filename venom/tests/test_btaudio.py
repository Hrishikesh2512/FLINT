"""Bluetooth automation tests — fake bluetoothctl runner, no hardware."""

import pytest

from venom.btaudio import BluetoothHeadset, normalize_mac, parse_devices, parse_info
from venom.config import AudioConfig, load_config

MAC = "AA:BB:CC:DD:EE:FF"

INFO_DISCONNECTED = """Device AA:BB:CC:DD:EE:FF (public)
	Name: My Buds
	Paired: yes
	Trusted: yes
	Connected: no
"""

INFO_CONNECTED = INFO_DISCONNECTED.replace("Connected: no", "Connected: yes")
INFO_FRESH = INFO_DISCONNECTED.replace("Paired: yes", "Paired: no").replace(
    "Trusted: yes", "Trusted: no")

DEVICES_OUT = """Device AA:BB:CC:DD:EE:FF My Buds
Device 11:22:33:44:55:66 Some TV
"""


def test_normalize_mac():
    assert normalize_mac("aa-bb-cc-dd-ee-ff") == MAC
    with pytest.raises(ValueError):
        normalize_mac("not-a-mac")


def test_parse_devices():
    devices = parse_devices(DEVICES_OUT)
    assert devices[MAC] == "My Buds"
    assert len(devices) == 2


def test_parse_info():
    assert parse_info(INFO_CONNECTED) == {
        "paired": True, "trusted": True, "connected": True}
    assert parse_info(INFO_FRESH) == {
        "paired": False, "trusted": False, "connected": False}
    assert parse_info("") == {"paired": False, "trusted": False, "connected": False}


class ScriptedRunner:
    """Replays canned bluetoothctl outputs; records the command sequence."""

    def __init__(self, info_sequence):
        self.calls = []
        self.info_sequence = list(info_sequence)

    def __call__(self, args, timeout):
        self.calls.append(args)
        command = args[0] if args[0] != "--timeout" else args[2]
        if command == "info":
            return self.info_sequence.pop(0) if self.info_sequence else INFO_CONNECTED
        if command == "devices":
            return DEVICES_OUT
        return "ok"


def test_ensure_connected_full_pairing_flow():
    runner = ScriptedRunner([INFO_FRESH, INFO_DISCONNECTED, INFO_CONNECTED])
    headset = BluetoothHeadset(mac=MAC, runner=runner)
    assert headset.ensure_connected() is True
    flat = [" ".join(c) for c in runner.calls]
    assert any(c.startswith("pair") for c in flat)
    assert any(c.startswith("trust") for c in flat)
    assert any(c.startswith("connect") for c in flat)


def test_ensure_connected_already_connected_is_cheap():
    runner = ScriptedRunner([INFO_CONNECTED, INFO_CONNECTED])
    headset = BluetoothHeadset(mac=MAC, runner=runner)
    assert headset.ensure_connected() is True
    flat = [" ".join(c) for c in runner.calls]
    assert not any(c.startswith("pair") for c in flat)
    assert not any(c.startswith("connect ") for c in flat)


def test_discover_mac_by_name():
    runner = ScriptedRunner([INFO_FRESH, INFO_DISCONNECTED, INFO_CONNECTED])
    headset = BluetoothHeadset(name="my buds", runner=runner)
    assert headset.discover_mac() == MAC


def test_discover_unknown_name_raises():
    headset = BluetoothHeadset(name="nonexistent", runner=ScriptedRunner([]))
    with pytest.raises(LookupError, match="pairing mode"):
        headset.discover_mac()


def test_wait_for_connection_retries():
    attempts = []

    class FlakyRunner(ScriptedRunner):
        def __call__(self, args, timeout):
            if args[0] == "info":
                attempts.append(1)
                return INFO_CONNECTED if len(attempts) > 4 else INFO_FRESH
            return super().__call__(args, timeout)

    headset = BluetoothHeadset(mac=MAC, runner=FlakyRunner([]))
    assert headset.wait_for_connection(attempts=3, delay=0, sleep=lambda _: None)


def test_needs_mac_or_name():
    with pytest.raises(ValueError):
        BluetoothHeadset()


# ── config + device selection integration ────────────────────────────────────
def test_audio_config_modes():
    assert not AudioConfig().use_bluetooth                       # auto, nothing set
    assert AudioConfig(bluetooth_mac=MAC).use_bluetooth          # auto + configured
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
    usb = pick_devices(table, bluetooth=False)
    assert usb.input_index == 2
