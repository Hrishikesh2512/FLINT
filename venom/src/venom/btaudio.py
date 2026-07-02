"""Bluetooth headset automation — pair once, reconnect forever.

Drives bluetoothctl non-interactively (BlueZ ships with the appliance).
Modern headsets use "Just Works" pairing, so no PIN is involved: put the
headset in pairing mode near the Pi on its first boot; Venom scans, pairs
by MAC (or finds the MAC by name), trusts, and connects. `trust` makes
BlueZ accept the headset automatically on every future power-on.

All command execution goes through an injectable runner so the whole flow
is unit-testable without hardware.
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
    """'info <mac>' output -> {paired, trusted, connected}."""
    flags = {}
    for key in ("Paired", "Trusted", "Connected"):
        match = re.search(rf"{key}:\s*(yes|no)", output)
        flags[key.lower()] = bool(match and match.group(1) == "yes")
    return flags


class BluetoothHeadset:
    def __init__(self, mac: str = "", name: str = "",
                 runner: Runner = _default_runner):
        if not mac and not name:
            raise ValueError("need a Bluetooth MAC or a device name")
        self.mac = normalize_mac(mac) if mac else ""
        self.name = name
        self._run = runner

    # ── state ─────────────────────────────────────────────────────────────────
    def status(self) -> dict[str, bool]:
        if not self.mac:
            return {"paired": False, "trusted": False, "connected": False}
        return parse_info(self._run(["info", self.mac], 10))

    @property
    def connected(self) -> bool:
        return self.status()["connected"]

    # ── discovery ─────────────────────────────────────────────────────────────
    def discover_mac(self, scan_seconds: float = 12) -> str:
        """Find the MAC by device name via a live scan (first boot only)."""
        self._run(["power", "on"], 10)
        self._run(["--timeout", str(int(scan_seconds)), "scan", "on"], scan_seconds + 10)
        devices = parse_devices(self._run(["devices"], 10))
        wanted = self.name.lower()
        for mac, name in devices.items():
            if wanted and wanted in name.lower():
                log.info("found %r at %s", name, mac)
                self.mac = mac
                return mac
        raise LookupError(
            f"no Bluetooth device named like {self.name!r} in range — "
            f"is the headset in pairing mode? (saw: {list(devices.values())[:5]})"
        )

    # ── the one-time pairing + every-boot connect ────────────────────────────
    def ensure_connected(self) -> bool:
        """Pair/trust if needed, then connect. True when audio can flow."""
        self._run(["power", "on"], 10)
        self._run(["agent", "NoInputNoOutput"], 10)

        if not self.mac:
            self.discover_mac()

        state = self.status()
        if not state["paired"]:
            log.info("pairing with %s ...", self.mac)
            self._run(["--timeout", "10", "scan", "on"], 20)  # must be in range
            out = self._run(["pair", self.mac], 30)
            if "Failed" in out and "AlreadyExists" not in out:
                log.warning("pair failed: %s", out.strip()[:120])
                return False
        if not state["trusted"]:
            self._run(["trust", self.mac], 10)

        if not self.status()["connected"]:
            out = self._run(["connect", self.mac], 30)
            if "Failed" in out:
                log.warning("connect failed: %s", out.strip()[:120])

        connected = self.status()["connected"]
        if connected:
            log.info("bluetooth headset connected: %s", self.mac)
        return connected

    def wait_for_connection(self, attempts: int = 5, delay: float = 5.0,
                            sleep=time.sleep) -> bool:
        for attempt in range(1, attempts + 1):
            try:
                if self.ensure_connected():
                    return True
            except LookupError as exc:
                log.info("attempt %d/%d: %s", attempt, attempts, exc)
            except Exception:
                log.exception("bluetooth attempt %d/%d failed", attempt, attempts)
            if attempt < attempts:
                sleep(delay)
        return False
