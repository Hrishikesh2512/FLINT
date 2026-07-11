"""Bluetooth receive tests — fake bluetoothctl, pw-dump and pw-loopback,
no hardware. The Pi doubles as a Bluetooth headset for the laptop/phone
(their audio → earphone, earphone mic → their calls); these prove the
pairing window, the per-direction bridges, and above all that Venom's own
audio path is re-pinned (never disturbed) when an external device connects."""

from venom.audio.receiver import BluetoothReceiver, find_bt_streams
from venom.audio.routing import find_bluez_card

LAPTOP_MAC = "AA:BB:CC:11:22:33"
LAPTOP_IN_NODE = "bluez_input.AA_BB_CC_11_22_33"
LAPTOP_OUT_NODE = "bluez_output.AA_BB_CC_11_22_33.1"
HEADSET_MAC = "DD:EE:FF:44:55:66"

DEVICES_LAPTOP = "Device AA:BB:CC:11:22:33 HRISHI-LAPTOP\n"
INFO_AUDIO = "\tUUID: Audio Source (0000110a-0000-1000-8000-00805f9b34fb)\n"
INFO_NO_AUDIO = "\tUUID: Human Interface Device (00001812-...)\n"


def bt_node(node_id: int, name: str, media_class: str = "Audio/Source",
            profile: str = "a2dp-source", address: str = "") -> dict:
    props = {"media.class": media_class, "node.name": name,
             "device.api": "bluez5"}
    if profile:
        props["api.bluez5.profile"] = profile
    if address:
        props["api.bluez5.address"] = address
    return {"id": node_id, "info": {"props": props}}


class FakeProc:
    """Stands in for the bluetoothctl agent and pw-loopback bridges."""

    def __init__(self):
        self.writes: list[str] = []
        self.terminated = False
        self._returncode = None
        self.stdin = self

    def write(self, text: str) -> None:
        self.writes.append(text)

    def flush(self) -> None:
        pass

    def poll(self):
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._returncode = 0


class FakeRunner:
    def __init__(self, devices: str = "", info: str = ""):
        self.calls: list[list[str]] = []
        self.devices_out = devices
        self.info_out = info

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == "devices":
            return self.devices_out
        if args[0] == "info":
            return self.info_out
        return ""


def make_receiver(runner=None, dump=None, clock=None, **kwargs):
    spawned: list[tuple[str, str, FakeProc]] = []

    def bridge_factory(name: str, direction: str) -> FakeProc:
        proc = FakeProc()
        spawned.append((name, direction, proc))
        return proc

    dumps = {"count": 0}

    def pw_dump():
        dumps["count"] += 1
        return list(dump or [])

    receiver = BluetoothReceiver(
        runner=runner or FakeRunner(),
        agent_factory=FakeProc,
        bridge_factory=bridge_factory,
        pw_dump=pw_dump,
        clock=clock or (lambda: 0.0),
        **kwargs)
    return receiver, spawned, dumps


# ── pure parsing ──────────────────────────────────────────────────────────────
def test_find_bt_streams_takes_a2dp_in_and_gateway_out():
    objects = [
        bt_node(42, LAPTOP_IN_NODE),                              # their music
        bt_node(43, LAPTOP_OUT_NODE, media_class="Audio/Sink",    # their mic
                profile="headset-audio-gateway"),
        bt_node(50, "bluez_input.DD_EE_FF_44_55_66",              # headset mic
                profile="headset-head-unit"),
        bt_node(60, "alsa_input.usb-xyz", profile=""),            # not bluez
        bt_node(70, "bluez_output.EE_00_00_00_00_01",             # plain A2DP
                media_class="Audio/Sink", profile="a2dp-sink"),   # sink: skip
    ]
    objects[3]["info"]["props"]["device.api"] = "alsa"
    streams = find_bt_streams(objects)
    assert {(s.node_id, s.direction) for s in streams} == {(42, "in"),
                                                           (43, "out")}
    assert all(s.mac == LAPTOP_MAC for s in streams)  # from the node name


def test_find_bt_streams_excludes_headset_by_mac():
    # Even if a headset stack exposes an a2dp-profile source, the configured
    # headset MAC is never bridged (that would be an echo loop).
    objects = [bt_node(42, "bluez_input.DD_EE_FF_44_55_66")]
    assert find_bt_streams(objects, exclude_mac=HEADSET_MAC) == []
    assert len(find_bt_streams(objects)) == 1


def test_find_bluez_card_prefers_headset_mac_over_laptop():
    objects = [
        {"id": 10, "info": {"props": {"media.class": "Audio/Device",
                                      "device.api": "bluez5",
                                      "api.bluez5.address": LAPTOP_MAC}}},
        {"id": 20, "info": {"props": {"media.class": "Audio/Device",
                                      "device.api": "bluez5",
                                      "api.bluez5.address": HEADSET_MAC}}},
    ]
    assert find_bluez_card(objects) == 10                    # legacy: first
    assert find_bluez_card(objects, HEADSET_MAC.lower()) == 20
    assert find_bluez_card(objects, "99:99:99:99:99:99") == 10  # fallback


# ── pairing window ────────────────────────────────────────────────────────────
def test_open_pairing_registers_agent_and_goes_discoverable():
    receiver, _, _ = make_receiver()
    msg = receiver.open_pairing()
    assert "venom" in msg and "Pairing" in msg
    agent = receiver._agent
    sent = "".join(agent.writes)
    for cmd in ("agent NoInputNoOutput", "default-agent",
                "pairable on", "discoverable on"):
        assert cmd in sent


def test_pairing_window_expires_and_trusts_newcomers():
    clock = {"t": 0.0}
    runner = FakeRunner(devices=DEVICES_LAPTOP, info=INFO_NO_AUDIO)
    receiver, _, _ = make_receiver(runner=runner, clock=lambda: clock["t"])
    receiver.open_pairing(window_s=120)
    agent = receiver._agent

    clock["t"] = 5.0
    receiver.poll_once()  # newcomer connected during the window → trusted
    assert ["trust", LAPTOP_MAC] in runner.calls
    assert not agent.terminated

    clock["t"] = 121.0
    receiver.poll_once()  # deadline passed → window closes
    assert agent.terminated
    assert "discoverable off" in "".join(agent.writes)
    assert receiver._agent is None


# ── stream bridges ────────────────────────────────────────────────────────────
def test_bridge_spawns_once_repins_defaults_and_reaps():
    repins = []
    runner = FakeRunner(devices=DEVICES_LAPTOP, info=INFO_AUDIO)
    dump = [bt_node(42, LAPTOP_IN_NODE)]
    receiver, spawned, _ = make_receiver(
        runner=runner, dump=dump, repin=lambda: repins.append(1))

    receiver.poll_once()
    assert [(name, d) for name, d, _ in spawned] == [(LAPTOP_IN_NODE, "in")]
    assert repins == [1]  # Venom's own defaults re-asserted before any bridge
    assert "HRISHI-LAPTOP" in receiver.status()

    receiver.poll_once()  # steady state — no duplicate bridge
    assert len(spawned) == 1

    dump.clear()          # laptop disconnected → node vanished
    receiver.poll_once()
    assert spawned[0][2].terminated
    assert "No external device" in receiver.status()


def test_mic_bridge_follows_the_hands_free_link():
    # Laptop joins a call: HFP replaces A2DP — the inbound bridge is reaped,
    # an inbound call-audio bridge and an outbound mic bridge appear.
    runner = FakeRunner(devices=DEVICES_LAPTOP, info=INFO_AUDIO)
    dump = [bt_node(42, LAPTOP_IN_NODE)]
    receiver, spawned, _ = make_receiver(runner=runner, dump=dump)
    receiver.poll_once()

    dump.clear()
    dump += [bt_node(80, "bluez_input.AA_BB_CC_11_22_33.2",
                     profile="headset-audio-gateway"),
             bt_node(81, LAPTOP_OUT_NODE, media_class="Audio/Sink",
                     profile="headset-audio-gateway")]
    receiver.poll_once()

    assert spawned[0][2].terminated  # the A2DP bridge died with its node
    live = {(name, d) for name, d, proc in spawned if not proc.terminated}
    assert live == {("bluez_input.AA_BB_CC_11_22_33.2", "in"),
                    (LAPTOP_OUT_NODE, "out")}
    status = receiver.status()
    assert "microphone" in status and "HRISHI-LAPTOP" in status


def test_non_audio_gadgets_never_trigger_pw_dump():
    # The camera-shutter remotes are connected BT devices with no A2DP —
    # they must not cost a pw-dump every poll.
    runner = FakeRunner(devices="Device 11:22:33:44:55:66 AB Shutter3\n",
                        info=INFO_NO_AUDIO)
    receiver, spawned, dumps = make_receiver(runner=runner)
    receiver.poll_once()
    receiver.poll_once()
    assert dumps["count"] == 0 and spawned == []


def test_disconnect_all_kicks_only_bridged_devices():
    runner = FakeRunner(devices=DEVICES_LAPTOP, info=INFO_AUDIO)
    receiver, _, _ = make_receiver(runner=runner,
                                   dump=[bt_node(42, LAPTOP_IN_NODE)])
    assert "Nothing" in receiver.disconnect_all()  # no bridges yet
    receiver.poll_once()
    msg = receiver.disconnect_all()
    assert "Disconnected 1 device" in msg
    assert ["disconnect", LAPTOP_MAC] in runner.calls


# ── registry + config wiring ──────────────────────────────────────────────────
def test_receiver_tools_registered_only_when_wired(tmp_path):
    from flint_core.memory import MemoryStore
    from venom.config import VenomConfig
    from venom.tools_pi import TimerBoard, build_pi_registry

    class StubReceiver:
        def open_pairing(self):
            return "pairing open"

        def status(self):
            return "status"

        def disconnect_all(self):
            return "done"

    config = VenomConfig(gemini_api_key="k", memory_path=tmp_path / "m.json")
    tools = {"pair_bluetooth_device", "bluetooth_audio_status",
             "disconnect_bluetooth_audio"}

    off = build_pi_registry(config, MemoryStore(config.memory_path), TimerBoard())
    assert not (tools & set(off.names()))

    on = build_pi_registry(config, MemoryStore(config.memory_path), TimerBoard(),
                           receiver=StubReceiver())
    assert tools <= set(on.names())
    assert on.dispatch("pair_bluetooth_device", {}) == "pairing open"
    assert on.dispatch("disconnect_bluetooth_audio", {}) == "done"


def test_receiver_config_flag(tmp_path):
    from venom.config import VenomConfig, load_config

    assert VenomConfig().audio.receiver is True  # on by default

    path = tmp_path / "venom.toml"
    path.write_text("[audio]\nreceiver = false\n")
    assert load_config(path).audio.receiver is False
