"""The Venom appliance supervisor.

One asyncio loop, one small cycle: probe internet, check the USB headset,
resolve the active brain, publish status, heartbeat the systemd watchdog,
sleep. State transitions are logged exactly once (journald-friendly), not
every cycle.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from venom import __version__, sdnotify
from venom.config import VenomConfig
from venom.monitors.audio import find_usb_audio
from venom.monitors.brain import BrainResolver
from venom.monitors.network import probe_tcp
from venom.status import StatusWriter

log = logging.getLogger("venom")


class Supervisor:
    def __init__(self, config: VenomConfig):
        self.config = config
        self.resolver = BrainResolver(config.brains, probe_timeout=config.probe_timeout)
        self.status = StatusWriter(config.status_path)
        self._stop = asyncio.Event()
        self._last: dict[str, Any] = {}

    # ── one monitoring cycle ─────────────────────────────────────────────────
    async def cycle(self) -> dict[str, Any]:
        internet_task = asyncio.create_task(
            probe_tcp(self.config.internet_host, self.config.internet_port,
                      self.config.probe_timeout)
        )
        resolution = await self.resolver.resolve()
        internet = await internet_task
        headset = find_usb_audio()

        snapshot: dict[str, Any] = {
            "version": __version__,
            "internet": internet,
            "headset": headset.description if headset else None,
            "brain": resolution.brain.name if resolution.brain else None,
            "online": resolution.online,
        }
        self._log_transitions(snapshot, resolution.switched)
        self.status.write(snapshot)
        return snapshot

    def _log_transitions(self, snapshot: dict[str, Any], brain_switched: bool) -> None:
        prev = self._last
        if snapshot["internet"] != prev.get("internet"):
            log.info("internet: %s", "up" if snapshot["internet"] else "down")
        if snapshot["headset"] != prev.get("headset"):
            if snapshot["headset"]:
                log.info("headset connected: %s", snapshot["headset"])
            else:
                log.warning("no USB headset detected")
        if brain_switched or snapshot["brain"] != prev.get("brain"):
            if snapshot["brain"]:
                log.info("brain: %s", snapshot["brain"])
            else:
                log.warning("brain: none reachable — offline mode")
        self._last = snapshot

    # ── lifecycle ────────────────────────────────────────────────────────────
    def request_stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                # Windows dev box — Ctrl+C raises KeyboardInterrupt instead.
                pass

    async def run(self) -> None:
        self._install_signal_handlers()
        log.info("venom %s starting (poll %.1fs, %d brain candidates)",
                 __version__, self.config.poll_interval, len(self.config.brains))
        sdnotify.notify_ready()
        try:
            while not self._stop.is_set():
                try:
                    await self.cycle()
                except Exception:
                    log.exception("monitor cycle failed")
                sdnotify.notify_watchdog()
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.config.poll_interval
                    )
                except TimeoutError:
                    pass
        finally:
            sdnotify.notify_stopping()
            log.info("venom stopped")
