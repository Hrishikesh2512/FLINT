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
import subprocess
from typing import Any

from venom import __version__, sdnotify
from venom.config import VenomConfig
from venom.monitors.audio import find_usb_audio
from venom.monitors.brain import BrainResolver, Resolution
from venom.monitors.network import probe_any
from venom.status import StatusWriter

log = logging.getLogger("venom")


class VoiceActivity:
    """FIXED (Fix 2): shared flag between the voice loop and the supervisor.

    The voice loop sets ``session_active`` True while a conversation is live
    and speaking, and False in the gap between sessions (pre-warm/reconnect).
    The brain switcher reads it and refuses to probe or switch while a
    conversation is active, so a momentary blip can never interrupt a working
    session mid-sentence.
    """

    def __init__(self) -> None:
        self.session_active = False


class Supervisor:
    def __init__(self, config: VenomConfig):
        self.config = config
        # FIXED (Fix 2): give the resolver hysteresis in production — tolerate 3
        # consecutive failed probes before abandoning a brain, and require 2
        # consecutive successes before a higher-priority brain pre-empts.
        self.resolver = BrainResolver(config.brains,
                                      probe_timeout=config.probe_timeout,
                                      fail_threshold=3, success_threshold=2)
        self.status = StatusWriter(config.status_path)
        self._stop = asyncio.Event()
        self._last: dict[str, Any] = {}
        self.voice_state = "disabled"
        self.activity = VoiceActivity()

    # ── one monitoring cycle ─────────────────────────────────────────────────
    async def cycle(self) -> dict[str, Any]:
        internet_task = asyncio.create_task(
            probe_any(self.config.internet_targets, self.config.probe_timeout)
        )
        # FIXED (Fix 2): never run the brain switcher while a conversation is
        # live — hold the last resolved brain and skip probing entirely. Brain
        # switching is only evaluated in the gap between sessions.
        if self.activity.session_active:
            resolution = Resolution(self.resolver.current, switched=False)
        else:
            resolution = await self.resolver.resolve()
        internet = await internet_task
        headset_desc = await asyncio.to_thread(self._headset_status)

        snapshot: dict[str, Any] = {
            "version": __version__,
            "internet": internet,
            "headset": headset_desc,
            "brain": resolution.brain.name if resolution.brain else None,
            "online": resolution.online,
            "voice": self.voice_state,
        }
        self._log_transitions(snapshot, resolution.switched)
        self.status.write(snapshot)
        return snapshot

    def _headset_status(self) -> str | None:
        """Bluetooth headset connection state, or the USB card description."""
        if self.config.audio.use_bluetooth:
            try:
                from venom.btaudio import BluetoothHeadset

                headset = BluetoothHeadset(self.config.audio.bluetooth_mac,
                                           self.config.audio.bluetooth_name)
                if headset.mac and headset.connected:
                    return f"bluetooth {headset.mac}"
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            return None
        card = find_usb_audio()
        return card.description if card else None

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

    def _start_voice(self) -> asyncio.Task | None:
        """Launch the voice loop if configured and the audio stack imports.

        Voice is an additive: a Pi without a key or without the audio deps
        still runs as a healthy monitored appliance.
        """
        if not self.config.voice_ready:
            self.voice_state = (
                "disabled (no gemini api key)" if not self.config.gemini_api_key
                else "disabled (config)"
            )
            log.info("voice %s", self.voice_state)
            return None
        try:
            import openwakeword  # noqa: F401 — fail here, not mid-loop
            import sounddevice  # noqa: F401

            from venom.voice import run_voice_forever
        except ImportError as exc:
            self.voice_state = f"disabled (missing dependency: {exc.name})"
            log.warning("voice %s", self.voice_state)
            return None

        def set_state(state: str) -> None:
            self.voice_state = state

        # FIXED (Fix 2): hand the shared activity flag to the voice loop so it
        # can tell the brain switcher when a conversation is live.
        return asyncio.create_task(
            run_voice_forever(self.config, set_state, self.activity))

    async def run(self) -> None:
        self._install_signal_handlers()
        log.info("venom %s starting (poll %.1fs, %d brain candidates)",
                 __version__, self.config.poll_interval, len(self.config.brains))
        sdnotify.notify_ready()
        voice_task = self._start_voice()
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
            if voice_task is not None:
                voice_task.cancel()
                try:
                    await voice_task
                except (asyncio.CancelledError, Exception):
                    pass
            sdnotify.notify_stopping()
            log.info("venom stopped")
