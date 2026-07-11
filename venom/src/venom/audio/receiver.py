"""Bluetooth audio receive — the wearable doubles as a Bluetooth headset.

The user's laptop or phone pairs to the Pi like any headset ("pair my
laptop" opens a short discoverable window). Both directions are bridged:

- what the device plays (A2DP, or HFP call audio) lands in the earphone,
  mixed with — never replacing — Venom's own voice
- when the device opens its hands-free link (a call, a meeting app), the
  earphone's microphone is looped back to it, so Venom's mic IS the
  laptop's mic. PipeWire shares one capture source between clients, so
  Venom keeps hearing the wake word at the same time.

Three moving parts:

- a pairing window: one persistent ``bluetoothctl`` process holding a
  NoInputNoOutput agent (auto-accept, Just Works) while discoverable;
  closed on a deadline so the Pi isn't permanently open to pairing
- a poll loop that watches PipeWire for the device's stream nodes and
  spawns a ``pw-loopback`` per direction: their source → default sink
  (earphone), and default source (mic) → their hands-free sink; each
  bridge dies with the connection
- a defaults re-pin hook: fired whenever a bridge appears, so a device
  that just connected can never steal the default sink/source the voice
  loop depends on. Venom's own audio stays untouched.

The headset's own nodes are never bridged (excluded by MAC and by the
``headset-head-unit`` profile), so a Bluetooth-headset setup can't loop
its mic back into its ear. All subprocess seams are injectable; parsing
is pure.
"""

from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from venom.btaudio import Runner, _default_runner, normalize_mac, parse_devices

log = logging.getLogger("venom.receiver")

PAIR_WINDOW_S = 120.0
# 3s, not 5: this is also how fast Bluetooth focus reacts when laptop audio
# starts — the voice loop should go radio-quiet within ~3s of the first note.
POLL_IDLE_S = 3.0     # nothing happening — cheap connected-devices check only
POLL_WINDOW_S = 1.0   # pairing window open — trust newcomers fast

_MAC_IN_NAME = re.compile(r"([0-9A-Fa-f]{2}[_:]){5}[0-9A-Fa-f]{2}")


# ── pure parsing ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BtStream:
    """A Bluetooth stream node in the PipeWire graph worth tracking.

    direction "in"  — audio the device sends us (→ earphone)
    direction "out" — the device's hands-free return channel (earphone
                      mic → it, so Venom's mic is its mic)
    managed — True when WE must run a pw-loopback for it. Verified on the
    Pi: this PipeWire classes incoming bluez nodes as Stream/Output/Audio
    (resp. Stream/Input/Audio), which WirePlumber auto-links to the
    default sink/source like any app stream — looping those ourselves
    would DOUBLE the audio. Only genuine Audio/Source|Sink device nodes
    (other stacks) need our bridge.
    """

    node_id: int
    name: str
    mac: str  # colon form, uppercase; "" when the props don't say
    direction: str  # "in" | "out"
    managed: bool = False


def _node_mac(props: dict, name: str) -> str:
    mac = str(props.get("api.bluez5.address", "")).strip().upper()
    if not mac:
        match = _MAC_IN_NAME.search(name)
        mac = match.group(0).replace("_", ":").upper() if match else ""
    return mac


def find_linked_nodes(objects: list[dict]) -> tuple[set[int], set[int]]:
    """(node ids with outgoing links, node ids with incoming links)."""
    out_linked: set[int] = set()
    in_linked: set[int] = set()
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info", {}) or {}
        if info.get("output-node-id") is not None:
            out_linked.add(info["output-node-id"])
        if info.get("input-node-id") is not None:
            in_linked.add(info["input-node-id"])
    return out_linked, in_linked


def find_default_node_names(objects: list[dict]) -> dict[str, str]:
    """{'sink': name, 'source': name} from the 'default' metadata object."""
    names: dict[str, str] = {}
    for obj in objects:
        if obj.get("type") != "PipeWire:Interface:Metadata":
            continue
        if (obj.get("props", {}) or {}).get("metadata.name") != "default":
            continue
        for entry in obj.get("metadata", []) or []:
            value = entry.get("value") or {}
            if entry.get("key") == "default.audio.sink":
                names["sink"] = str(value.get("name", ""))
            elif entry.get("key") == "default.audio.source":
                names["source"] = str(value.get("name", ""))
    return names


def find_bt_streams(objects: list[dict], exclude_mac: str = "") -> list[BtStream]:
    """Bluetooth stream nodes worth bridging, both directions.

    Inbound: any bluez capture node from a connected laptop/phone — A2DP
    music or HFP call downlink. Outbound: a bluez playback node whose
    profile says the remote device is a hands-free *audio gateway* (it
    opened the link that wants our microphone).

    The configured headset is excluded by MAC, and its own microphone by
    the ``headset-head-unit`` profile — bridging either would loop the
    headset's mic straight back into its ear.
    """
    exclude = exclude_mac.strip().upper().replace("-", ":")
    found: list[BtStream] = []
    for obj in objects:
        props = (obj.get("info", {}) or {}).get("props", {}) or {}
        media_class = str(props.get("media.class", ""))
        if media_class not in ("Audio/Source", "Audio/Sink",
                               "Stream/Output/Audio", "Stream/Input/Audio"):
            continue
        name = str(props.get("node.name", ""))
        if "bluez" not in (str(props.get("device.api", "")) + " " + name):
            continue
        profile = str(props.get("api.bluez5.profile", ""))
        if "headset-head-unit" in profile:  # our own headset's mic path
            continue
        mac = _node_mac(props, name)
        if exclude and mac == exclude:
            continue
        managed = media_class in ("Audio/Source", "Audio/Sink")
        if media_class in ("Audio/Source", "Stream/Output/Audio"):
            found.append(BtStream(obj.get("id"), name, mac, "in", managed))
        elif "gateway" in profile:
            # A playback node toward a hands-free gateway = its mic channel.
            # Plain A2DP sinks (headphones we play TO) are never bridged.
            found.append(BtStream(obj.get("id"), name, mac, "out", managed))
    return found


# ── default subprocess seams ──────────────────────────────────────────────────
def _default_agent_factory() -> subprocess.Popen:
    """A persistent bluetoothctl we feed commands over stdin — a one-shot
    `bluetoothctl agent ...` exits immediately and takes the agent with it,
    so incoming pairing would have nobody to say yes.

    -a registers the NoInputNoOutput (auto-accept, Just Works) agent at
    startup. Registering it by command doesn't work: bluetoothctl auto-
    registers an interactive agent first, and 'agent off' unregisters
    asynchronously, so a follow-up 'agent NoInputNoOutput' is refused as
    'already registered' and the window ends up with NO agent (observed
    live; the laptop got Authentication Failed)."""
    return subprocess.Popen(
        ["bluetoothctl", "-a", "NoInputNoOutput"], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
    )


def _default_bridge_factory(node_name: str, direction: str) -> subprocess.Popen:
    """One pw-loopback per stream. Inbound captures the device's stream and
    plays to the default sink (the earphone); outbound captures the default
    source (the earphone mic) and plays into the device's hands-free sink.
    pw-loopback resamples both sides itself, so the graph rate and the
    earphone's rate never need to agree with the peer's."""
    if direction == "in":
        args = ["pw-loopback", "-n", "venom-bt-bridge", "-C", node_name]
    else:
        args = ["pw-loopback", "-n", "venom-bt-mic-bridge", "-P", node_name]
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _default_pw_dump() -> list[dict]:
    from venom.audio.routing import pw_dump

    return pw_dump()


def _default_linker(output_node: str, input_node: str) -> None:
    """pw-link two nodes by name (channel ports pair up automatically)."""
    subprocess.run(["pw-link", output_node, input_node],
                   capture_output=True, timeout=10)


def _default_player_cmd(mac: str, action: str) -> bool:
    """AVRCP control of the sending device's media player — exactly what an
    earbud's pause button does. action: 'IsPlaying' | 'Pause' | 'Play'."""
    device = "/org/bluez/hci0/dev_" + mac.replace(":", "_")
    tree = subprocess.run(["busctl", "--system", "tree", "org.bluez"],
                          capture_output=True, text=True, timeout=10).stdout
    match = re.search(re.escape(device) + r"/player\d+", tree)
    if not match:
        return False
    player = match.group(0)
    if action == "IsPlaying":
        out = subprocess.run(
            ["busctl", "--system", "get-property", "org.bluez", player,
             "org.bluez.MediaPlayer1", "Status"],
            capture_output=True, text=True, timeout=10).stdout
        return "playing" in out
    result = subprocess.run(
        ["busctl", "--system", "call", "org.bluez", player,
         "org.bluez.MediaPlayer1", action],
        capture_output=True, timeout=10)
    return result.returncode == 0


@dataclass
class _Bridge:
    proc: subprocess.Popen | None  # None: wireplumber links it, we only track
    mac: str
    direction: str  # "in" | "out"


class BluetoothReceiver:
    """Pi-as-Bluetooth-headset: pairing window + per-direction bridges."""

    # bluetoothctl needs a beat after spawn to connect to bluetoothd; commands
    # written before that are lost. Observed live: 'Failed to register agent
    # object' -> its interactive auto-agent answered pairing with a passkey
    # prompt into /dev/null -> the laptop got Authentication Failed (0x05).
    AGENT_SETTLE_S = 2.0

    def __init__(self, headset_mac: str = "", headset_name: str = "",
                 repin: Callable[[], None] | None = None,
                 runner: Runner = _default_runner,
                 agent_factory: Callable[[], subprocess.Popen] = _default_agent_factory,
                 bridge_factory: Callable[[str, str], subprocess.Popen] = _default_bridge_factory,
                 pw_dump: Callable[[], list[dict]] = _default_pw_dump,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 linker: Callable[[str, str], None] = _default_linker,
                 player_cmd: Callable[[str, str], bool] = _default_player_cmd):
        self.headset_mac = normalize_mac(headset_mac) if headset_mac else ""
        self.headset_name = headset_name.strip()
        self._repin = repin
        self._run = runner
        self._agent_factory = agent_factory
        self._bridge_factory = bridge_factory
        self._pw_dump = pw_dump
        self._clock = clock
        self._sleep = sleep
        self._link = linker
        self._player = player_cmd
        self._held: list[str] = []  # devices we AVRCP-paused for a conversation

        self._lock = threading.Lock()
        self._agent: subprocess.Popen | None = None
        self._deadline = 0.0
        self._bridges: dict[str, _Bridge] = {}  # node name -> bridge
        self._names: dict[str, str] = {}        # MAC -> device name
        self._trusted: set[str] = set()
        self._audio_capable: dict[str, bool] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_failed = False  # warn once, then stay quiet (dev boxes)

    @property
    def is_streaming(self) -> bool:
        """True while an external device is actively sending audio into the
        earphone. The A2DP node exists only while audio flows (the device
        suspends it when idle), so this tracks real playback — not a merely
        connected laptop."""
        with self._lock:
            return any(b.direction == "in" for b in self._bridges.values())

    def hold_streams(self) -> None:
        """A conversation is starting: AVRCP-pause every device streaming
        into the earphone. Their audio otherwise bleeds into the shared
        earphone mic and Gemini never hears a clean end-of-speech — the
        user talks, she never replies (observed live). Only devices that
        were actually playing are remembered for release_streams, so a
        manual pause is never resumed over."""
        with self._lock:
            macs = sorted({b.mac for b in self._bridges.values()
                           if b.direction == "in" and b.mac})
        self._held = []
        for mac in macs:
            try:
                if self._player(mac, "IsPlaying") and self._player(mac, "Pause"):
                    self._held.append(mac)
                    log.info("paused %s's media for the conversation",
                             self._names.get(mac, mac))
            except Exception:
                log.exception("AVRCP pause failed for %s", mac)

    def release_streams(self) -> None:
        """Conversation over: resume exactly what hold_streams paused."""
        held, self._held = self._held, []
        for mac in held:
            try:
                if self._player(mac, "Play"):
                    log.info("resumed %s's media", self._names.get(mac, mac))
            except Exception:
                log.exception("AVRCP resume failed for %s", mac)

    # ── voice-tool surface (each returns natural speech) ─────────────────────
    def open_pairing(self, window_s: float = PAIR_WINDOW_S) -> str:
        with self._lock:
            # Deadline FIRST: the poll thread checks (agent, deadline) as a
            # pair; exposing a fresh agent while the previous window's stale
            # deadline is still in the past made poll_once kill the new
            # window the same second it opened (observed live).
            self._deadline = self._clock() + window_s
            try:
                if self._agent is None or self._agent.poll() is not None:
                    agent = self._agent_factory()
                    self._sleep(self.AGENT_SETTLE_S)  # let it reach bluetoothd
                    for cmd in ("power on", "default-agent",
                                "pairable on", "discoverable on"):
                        agent.stdin.write(cmd + "\n")
                        agent.stdin.flush()
                    self._agent = agent
                else:  # window re-opened — just refresh discoverability
                    self._agent.stdin.write("discoverable on\n")
                    self._agent.stdin.flush()
            except OSError as exc:
                log.warning("could not open pairing window: %s", exc)
                self._agent = None
                self._deadline = 0.0
                return ("I couldn't open Bluetooth pairing just now — "
                        "the Bluetooth service may be down.")
        log.info("bluetooth pairing window open for %.0fs", window_s)
        minutes = max(1, round(window_s / 60))
        return (f"Pairing is open for about {minutes} minute"
                f"{'s' if minutes > 1 else ''}. On the laptop or phone, pick "
                f"'venom' in the Bluetooth device list — once it connects, "
                f"its audio plays in your earpiece, and on calls it can use "
                f"this mic too.")

    def status(self) -> str:
        with self._lock:
            streaming = sorted({self._names.get(b.mac, b.mac or "an unnamed device")
                                for b in self._bridges.values()
                                if b.direction == "in"})
            mic_users = sorted({self._names.get(b.mac, b.mac or "an unnamed device")
                                for b in self._bridges.values()
                                if b.direction == "out"})
            window_open = (self._agent is not None
                           and self._clock() < self._deadline)
        parts = []
        if streaming:
            parts.append(f"{' and '.join(streaming)} is streaming audio "
                         f"through your earpiece")
        if mic_users:
            parts.append(f"{' and '.join(mic_users)} is using your "
                         f"earpiece microphone")
        if parts:
            return ", and ".join(parts) + "."
        if window_open:
            return ("Nothing is streaming yet, but pairing is open — pick "
                    "'venom' in the device's Bluetooth list.")
        return "No external device is streaming audio right now."

    def disconnect_all(self) -> str:
        with self._lock:
            macs = sorted({b.mac for b in self._bridges.values() if b.mac})
        if not macs:
            return "Nothing is streaming to disconnect."
        for mac in macs:
            try:
                self._run(["disconnect", mac], 15)
            except Exception as exc:
                log.warning("disconnect %s failed: %s", mac, exc)
        n = len(macs)
        return (f"Disconnected {n} device{'s' if n > 1 else ''} — "
                f"the earpiece is all yours again.")

    # ── background loop ───────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="bt-receiver")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._close_pairing()
        with self._lock:
            for name in list(self._bridges):
                self._kill_bridge(name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
                self._poll_failed = False
            except Exception as exc:
                # First failure is worth a warning; a box without
                # bluetoothctl/pipewire would otherwise spam the journal.
                if not self._poll_failed:
                    log.warning("receiver poll failed: %s", exc)
                    self._poll_failed = True
            window_open = self._agent is not None and self._clock() < self._deadline
            self._stop.wait(POLL_WINDOW_S if window_open else POLL_IDLE_S)

    def poll_once(self) -> None:
        """One housekeeping pass: window expiry, trust, bridge lifecycles."""
        now = self._clock()
        with self._lock:  # read (agent, deadline) atomically vs open_pairing
            expired = self._agent is not None and now >= self._deadline
        if expired:
            self._close_pairing()

        connected = self._connected_devices()
        with self._lock:
            self._names.update(connected)
        window_open = self._agent is not None
        if window_open:
            # Answer any pending yes/no the agent may still throw (e.g. an
            # AuthorizeService for a device that connects its audio profile
            # faster than we trust it). Harmless when nothing is pending —
            # bluetoothctl just ignores an unknown command.
            try:
                self._agent.stdin.write("yes\n")
                self._agent.stdin.flush()
            except OSError:
                pass

        for mac in connected:
            if window_open and mac not in self._trusted:
                # A newcomer paired during the window: trust it so it can
                # reconnect forever without another pairing dance.
                self._run(["trust", mac], 10)
                self._trusted.add(mac)

        # pw-dump is the expensive call — only when a device that can even
        # carry audio is around, or we still hold bridges to reap.
        if not self._bridges and not any(
                self._is_audio_peer(mac) for mac in connected):
            return

        objects = self._pw_dump()
        streams = {node.name: node
                   for node in find_bt_streams(objects, self.headset_mac)}

        with self._lock:
            # Reap: node gone, or a loopback we own died underneath us.
            for name in list(self._bridges):
                bridge = self._bridges[name]
                if (name not in streams
                        or (bridge.proc is not None
                            and bridge.proc.poll() is not None)):
                    self._kill_bridge(name)
            for name, node in streams.items():
                if name in self._bridges:
                    continue
                # Before any external audio flows, make sure the defaults
                # still point at Venom's own headset — a device that just
                # connected must never disturb the Pi's audio path.
                if self._repin is not None:
                    try:
                        self._repin()
                    except Exception:
                        log.exception("defaults re-pin failed")
                proc = None
                if node.managed:
                    try:
                        proc = self._bridge_factory(name, node.direction)
                    except OSError as exc:
                        log.warning("could not start audio bridge for %s: %s",
                                    name, exc)
                        continue
                self._bridges[name] = _Bridge(proc, node.mac, node.direction)
                if node.mac:
                    self._trusted.add(node.mac)
                    self._run(["trust", node.mac], 10)
                log.info("bluetooth %s (%s): %s (%s)",
                         "bridge" if node.managed else "stream",
                         "their audio -> earphone" if node.direction == "in"
                         else "earphone mic -> them",
                         name, self._names.get(node.mac, node.mac))

        # Self-heal orphaned streams. WirePlumber links a stream node to the
        # defaults when it appears — usually. Observed live: after the node
        # was recreated (A2DP suspend/resume across a service restart) it
        # came back with NO links: running, carrying audio, connected to
        # nothing — pure silence. Every poll, wire any unlinked stream to
        # the default sink/source ourselves; no-op when WirePlumber did
        # its job (or a previous heal already linked it).
        out_linked, in_linked = find_linked_nodes(objects)
        defaults = find_default_node_names(objects)
        for name, node in streams.items():
            if node.managed:
                continue  # our own pw-loopback handles those
            try:
                if (node.direction == "in" and defaults.get("sink")
                        and node.node_id not in out_linked):
                    self._link(name, defaults["sink"])
                    log.info("re-linked orphaned stream %s -> %s",
                             name, defaults["sink"])
                elif (node.direction == "out" and defaults.get("source")
                        and node.node_id not in in_linked):
                    self._link(defaults["source"], name)
                    log.info("re-linked orphaned mic stream %s -> %s",
                             defaults["source"], name)
            except Exception as exc:
                log.warning("stream re-link failed for %s: %s", name, exc)

    # ── helpers ───────────────────────────────────────────────────────────────
    def _connected_devices(self) -> dict[str, str]:
        """{mac: name} of connected devices that aren't the headset."""
        devices = parse_devices(self._run(["devices", "Connected"], 10))
        out: dict[str, str] = {}
        for mac, name in devices.items():
            if self.headset_mac and mac == self.headset_mac:
                continue
            if (self.headset_name
                    and self.headset_name.lower() in name.lower()):
                continue
            out[mac] = name
        return out

    def _is_audio_peer(self, mac: str) -> bool:
        """Can this device stream audio at us or use our mic — i.e. does it
        offer A2DP Audio Source or a hands-free/headset Audio Gateway?
        Checked once per MAC (bluetoothctl info) so shutter remotes and
        other non-audio gadgets never trigger a pw-dump every poll."""
        if mac not in self._audio_capable:
            info = self._run(["info", mac], 10)
            self._audio_capable[mac] = ("Audio Source" in info
                                        or "Gateway" in info)
        return self._audio_capable[mac]

    def _close_pairing(self) -> None:
        with self._lock:
            agent, self._agent = self._agent, None
        if agent is None:
            return
        try:
            agent.stdin.write("discoverable off\npairable off\nexit\n")
            agent.stdin.flush()
        except OSError:
            pass
        try:
            agent.terminate()
        except Exception:
            pass
        log.info("bluetooth pairing window closed")

    def _kill_bridge(self, name: str) -> None:
        # caller holds self._lock
        bridge = self._bridges.pop(name)
        if bridge.proc is not None:
            try:
                bridge.proc.terminate()
            except Exception:
                pass
        log.info("bluetooth audio stream closed: %s (%s)", name,
                 self._names.get(bridge.mac, bridge.mac))


# One receiver per process: the voice orchestrator is rebuilt after crashes,
# and two instances would each spawn a loopback per stream — doubled audio.
_shared: BluetoothReceiver | None = None


def shared_receiver(**kwargs) -> BluetoothReceiver:
    global _shared
    if _shared is None:
        _shared = BluetoothReceiver(**kwargs)
    return _shared
