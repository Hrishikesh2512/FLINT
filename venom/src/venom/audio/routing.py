"""PipeWire routing control: pin the Bluetooth headset's microphone profile
and make its nodes the defaults.

Cheap headsets connect in music-only mode (A2DP: no mic) and PipeWire's
autoswitch doesn't trigger for plain ALSA clients, so Venom takes charge:
after every connect, switch the bluez card to the HFP profile and point
the default sink/source at it. All parsing is pure (testable); only the
two subprocess helpers touch the system.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time

log = logging.getLogger("venom.routing")


def _run(args: list[str], timeout: float = 10) -> str:
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return result.stdout or ""


def pw_dump() -> list[dict]:
    try:
        return json.loads(_run(["pw-dump"], timeout=15) or "[]")
    except (json.JSONDecodeError, subprocess.SubprocessError) as exc:
        log.warning("pw-dump failed: %s", exc)
        return []


# ── pure parsing ──────────────────────────────────────────────────────────────
def find_bluez_card(objects: list[dict], mac: str = "") -> int | None:
    """The headset's bluez card. With a MAC, prefer the card at that address —
    a laptop streaming audio TO the Pi is also a bluez card, and switching
    *its* profile would kill the incoming stream. Falls back to the first
    bluez card when no address matches (older stacks without the prop)."""
    want = mac.strip().upper().replace("-", ":")
    fallback: int | None = None
    for obj in objects:
        props = (obj.get("info", {}) or {}).get("props", {}) or {}
        if not (props.get("media.class") == "Audio/Device"
                and "bluez" in str(props.get("device.api", ""))):
            continue
        if not want:
            return obj.get("id")
        if str(props.get("api.bluez5.address", "")).upper() == want:
            return obj.get("id")
        if fallback is None:
            fallback = obj.get("id")
    return fallback


def enum_profiles(objects: list[dict], card_id: int) -> list[dict]:
    for obj in objects:
        if obj.get("id") != card_id:
            continue
        params = (obj.get("info", {}) or {}).get("params", {}) or {}
        return list(params.get("EnumProfile", []))
    return []


def pick_headset_profile(profiles: list[dict]) -> dict | None:
    """The HFP/HSP profile (has the microphone), best codec first."""
    headset = [p for p in profiles if "headset-head-unit" in str(p.get("name", ""))]
    if not headset:
        return None
    # prefer mSBC (16 kHz — matches our pipeline) over CVSD when offered
    headset.sort(key=lambda p: ("msbc" not in str(p.get("name", "")),))
    return headset[0]


def pick_a2dp_profile(profiles: list[dict]) -> dict | None:
    """The A2DP profile — speaker only, no microphone."""
    a2dp = [p for p in profiles if str(p.get("name", "")).startswith("a2dp-sink")]
    if not a2dp:
        return None
    # Plain a2dp-sink before the sbc_xq variant: xq is a higher-bitrate SBC,
    # which is exactly the wrong trade when the point of being here is airtime.
    a2dp.sort(key=lambda p: (str(p.get("name", "")) != "a2dp-sink",))
    return a2dp[0]


def release_bluetooth_mic(wait: float = 2.0, mac: str = "") -> bool:
    """Drop the headset to A2DP: speaker alive, microphone gone. True on success.

    The inverse of pin_bluetooth_audio, and the reason it exists is airtime,
    not audio. HFP carries the mic over a SCO link, which reserves periodic
    2.4GHz slots; on a Pi, where one antenna serves both radios, that starves
    Wi-Fi. Measured on this device, same link, minutes apart:

        HFP (mic live)    ~20 KB/s
        A2DP (speaker)   ~370 KB/s
        Bluetooth off    ~845 KB/s

    Ping barely moves across those three — small packets slip between the
    reserved slots — so this is only ever visible in a throughput test.
    Idling in A2DP buys that bandwidth back for everything that isn't a
    conversation; pin_bluetooth_audio takes the mic back on wake.
    """
    objects = pw_dump()
    card = find_bluez_card(objects, mac)
    if card is None:
        log.info("no bluez card to release")
        return False

    target = pick_a2dp_profile(enum_profiles(objects, card))
    if target is None:
        log.warning("card %d offers no a2dp profile", card)
        return False

    _run(["wpctl", "set-profile", str(card), str(target.get("index"))])
    log.info("card %d -> profile %s (mic released)", card, target.get("name"))
    time.sleep(wait)  # nodes are re-created after a profile switch

    # Re-assert the speaker as default: the sink is a *new* node id after the
    # switch, and without this she would keep talking into the old one.
    nodes = find_bluez_nodes(pw_dump(), card)
    for node_id in nodes.values():
        _run(["wpctl", "set-default", str(node_id)])
    return "sink" in nodes


def find_bluez_nodes(objects: list[dict], card_id: int) -> dict[str, int]:
    """{'sink': id, 'source': id} for nodes belonging to the bluez card."""
    nodes: dict[str, int] = {}
    for obj in objects:
        props = (obj.get("info", {}) or {}).get("props", {}) or {}
        if props.get("device.id") != card_id:
            continue
        media_class = props.get("media.class", "")
        if media_class == "Audio/Sink":
            nodes["sink"] = obj["id"]
        elif media_class == "Audio/Source":
            nodes["source"] = obj["id"]
    return nodes


def find_usb_nodes(objects: list[dict]) -> dict[str, int]:
    """{'sink': id, 'source': id} for the USB audio card's nodes."""
    nodes: dict[str, int] = {}
    for obj in objects:
        props = (obj.get("info", {}) or {}).get("props", {}) or {}
        blob = (str(props.get("node.name", "")) + " "
                + str(props.get("node.description", ""))).lower()
        if "usb" not in blob:
            continue
        media_class = props.get("media.class", "")
        if media_class == "Audio/Sink":
            nodes["sink"] = obj["id"]
        elif media_class == "Audio/Source":
            nodes["source"] = obj["id"]
    return nodes


# ── the operation ─────────────────────────────────────────────────────────────
def pin_usb_audio(wait: float = 1.0, attempts: int = 4) -> bool:
    """Make the USB headset's sink/source PipeWire's defaults, so the
    resampling 'pipewire' device Venom opens routes to the USB earphone
    (and not a Bluetooth headset that reconnected and grabbed default).
    True when a USB microphone source exists."""
    for attempt in range(1, attempts + 1):
        nodes = find_usb_nodes(pw_dump())
        if nodes:
            for node_id in nodes.values():
                _run(["wpctl", "set-default", str(node_id)])
            log.info("pinned USB audio (sink=%s source=%s)",
                     nodes.get("sink"), nodes.get("source"))
            return "source" in nodes
        log.info("no USB audio node yet (attempt %d/%d)", attempt, attempts)
        time.sleep(wait)
    return False


def pin_bluetooth_audio(wait: float = 2.0, attempts: int = 3,
                        mac: str = "") -> bool:
    """Switch the connected headset to its mic-capable profile and make its
    nodes the defaults. True when a Bluetooth microphone source exists.
    Pass the headset's MAC when other Bluetooth audio devices (a laptop
    streaming to the Pi) may be connected, so we pin the right card."""
    for attempt in range(1, attempts + 1):
        objects = pw_dump()
        card = find_bluez_card(objects, mac)
        if card is None:
            log.info("no bluez card in the graph yet (attempt %d/%d)", attempt, attempts)
            time.sleep(wait)
            continue

        # Fast path: a bluez source already exists, so the mic-capable
        # profile is active. Re-switching the profile here would tear the
        # Bluetooth audio link down audibly for nothing — just re-assert
        # the defaults (silent) and report ready.
        nodes = find_bluez_nodes(objects, card)
        if "source" in nodes:
            for node_id in nodes.values():
                _run(["wpctl", "set-default", str(node_id)])
            log.info("bluetooth mic already live (sink=%s source=%s)",
                     nodes.get("sink"), nodes.get("source"))
            return True

        profiles = enum_profiles(objects, card)
        target = pick_headset_profile(profiles)
        if target is None:
            log.warning("card %d offers no headset profile (profiles: %s)",
                        card, [p.get("name") for p in profiles])
            time.sleep(wait)
            continue

        _run(["wpctl", "set-profile", str(card), str(target.get("index"))])
        log.info("card %d -> profile %s", card, target.get("name"))
        time.sleep(wait)  # nodes are re-created after a profile switch

        objects = pw_dump()
        nodes = find_bluez_nodes(objects, card)
        for node_id in nodes.values():
            _run(["wpctl", "set-default", str(node_id)])
        if "source" in nodes:
            log.info("bluetooth mic is live (sink=%s source=%s)",
                     nodes.get("sink"), nodes.get("source"))
            return True
        log.info("no source after profile switch (attempt %d/%d)", attempt, attempts)
        time.sleep(wait)
    return False
