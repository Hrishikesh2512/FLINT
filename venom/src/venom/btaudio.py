"""Bluetooth headset automation — pair once (per session, for bond-forgetting
headsets), reconnect fast.

Drives bluetoothctl non-interactively. Discovery runs as ONE continuous
scan with fast polling — the moment the headset enters pairing mode it
becomes visible and pairing follows within seconds (short scan windows
made connects take minutes on real hardware).

Command execution and the scanner process are injectable, so the whole
flow is unit-testable without hardware.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable

log = logging.getLogger("venom.bt")

MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

# (args, timeout_seconds) -> stdout text
Runner = Callable[[list[str], float], str]


def _default_runner(args: list[str], timeout: float) -> str:
    result = subprocess.run(
        ["bluetoothctl", *args], capture_output=True, text=True, timeout=timeout
    )
    return (result.stdout or "") + (result.stderr or "")


def _default_scanner():
    """A running discovery session; caller terminates it."""
    return subprocess.Popen(
        ["bluetoothctl", "scan", "on"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def normalize_mac(mac: str) -> str:
    mac = mac.strip().upper().replace("-", ":")
    if not MAC_RE.match(mac):
        raise ValueError(f"not a Bluetooth MAC address: {mac!r}")
    return mac


def parse_devices(output: str) -> dict[str, str]:
    """'Device XX:.. Name' lines -> {mac: name}."""
    found: dict[str, str] = {}
    for line in output.splitlines():
        match = re.search(
            r"Device\s+(([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+(.+)$", line.strip()
        )
        if match:
            found[match.group(1).upper()] = match.group(3).strip()
    return found


def parse_info(output: str) -> dict[str, bool]:
    """'info <mac>' output -> {paired, trusted, connected, in_range}."""
    flags = {}
    for key in ("Paired", "Trusted", "Connected"):
        match = re.search(rf"{key}:\s*(yes|no)", output)
        flags[key.lower()] = bool(match and match.group(1) == "yes")
    # RSSI only appears while the device is actually broadcasting in range.
    flags["in_range"] = "RSSI:" in output or flags["connected"]
    return flags


class BluetoothHeadset:
    def __init__(self, mac: str = "", name: str = "",
                 runner: Runner = _default_runner,
                 scanner: Callable = _default_scanner):
        if not mac and not name:
            raise ValueError("need a Bluetooth MAC or a device name")
        self.mac = normalize_mac(mac) if mac else ""
        self.name = name
        self._run = runner
        self._scanner = scanner

    # ── state ─────────────────────────────────────────────────────────────────
    def status(self) -> dict[str, bool]:
        if not self.mac:
            return {"paired": False, "trusted": False,
                    "connected": False, "in_range": False}
        return parse_info(self._run(["info", self.mac], 10))

    @property
    def connected(self) -> bool:
        return self.status()["connected"]

    # ── discovery: one continuous scan, fast polling ─────────────────────────
    def wait_visible(self, timeout: float = 45.0, poll: float = 2.0,
                     sleep=time.sleep, clock=time.monotonic) -> bool:
        scan = self._scanner()
        try:
            deadline = clock() + timeout
            while clock() < deadline:
                if not self.mac and self.name:
                    for mac, name in parse_devices(self._run(["devices"], 10)).items():
                        if self.name.lower() in name.lower():
                            log.info("found %r at %s", name, mac)
                            self.mac = mac
                            break
                if self.mac and self.status()["in_range"]:
                    return True
                sleep(poll)
            return False
        finally:
            try:
                scan.terminate()
            except Exception:
                pass
            self._run(["scan", "off"], 10)

    # ── pair (as needed) + connect ────────────────────────────────────────────
    def ensure_connected(self) -> bool:
        """True when audio can flow. Fast path when already connected."""
        self._run(["power", "on"], 10)
        self._run(["agent", "NoInputNoOutput"], 10)

        if self.mac and self.status()["connected"]:
            return True

        # Idle headsets often accept a direct page connection (and re-pair
        # silently via the agent) even though they refuse discovery — try
        # that first; it turns "hold the pairing button" into "just turn
        # the earbuds on". Verified on real hardware.
        if self.mac:
            self._run(["connect", self.mac], 30)
            if self.status()["connected"]:
                log.info("bluetooth headset connected directly (no pairing mode): %s",
                         self.mac)
                return True

        if not self.wait_visible():
            log.info("headset not broadcasting — pairing mode needed; will retry")
            return False

        state = self.status()
        if not state["paired"]:
            log.info("pairing with %s ...", self.mac)
            out = self._run(["pair", self.mac], 30)
            if "Failed" in out and "AlreadyExists" not in out:
                log.warning("pair failed: %s", out.strip()[:120])
                return False
        self._run(["trust", self.mac], 10)

        if not self.status()["connected"]:
            out = self._run(["connect", self.mac], 30)
            if "Failed" in out:
                log.warning("connect failed: %s", out.strip()[:120])

        connected = self.status()["connected"]
        if connected:
            log.info("bluetooth headset connected: %s", self.mac)
        return connected

    def wait_for_connection(self, attempts: int = 3, delay: float = 3.0,
                            sleep=time.sleep) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                if self.ensure_connected():
                    return True
            except Exception:
                log.exception("bluetooth attempt %d/%d failed", attempt, attempts)
            if attempt < attempts:
                sleep(delay)
        return False
