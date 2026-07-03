"""Headset button handling.

When a Bluetooth headset connects, BlueZ exposes its AVRCP buttons as a
Linux input device ("... (AVRCP)"). We watch for such devices appearing
and translate play/pause presses into MusicPlayer.toggle_pause(). Device
discovery re-scans periodically, so the listener survives headset drops
and reconnects without coordination.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("venom.buttons")

# Key codes across headset firmwares (linux/input-event-codes.h)
TOGGLE_CODES = {164}          # KEY_PLAYPAUSE
PLAY_CODES = {200, 207}       # KEY_PLAYCD, KEY_PLAY
PAUSE_CODES = {201, 209}      # KEY_PAUSECD, KEY_STOPCD-adjacent variants
RESCAN_SECONDS = 10
DEBOUNCE_SECONDS = 0.5        # one press can emit several events — act once


def find_avrcp_devices() -> list:
    """All input devices that look like Bluetooth headset controls."""
    try:
        import evdev
    except ImportError:
        return []
    devices = []
    for path in evdev.list_devices():
        try:
            device = evdev.InputDevice(path)
        except OSError:
            continue
        name = (device.name or "").upper()
        if "AVRCP" in name or "HEADPHONE" in name or "HEADSET" in name:
            devices.append(device)
        else:
            device.close()
    return devices


async def watch_buttons(music) -> None:
    """Forever: attach to headset button devices and route presses to music."""
    try:
        import evdev
    except ImportError:
        log.info("evdev not installed — headset buttons disabled")
        return

    watched: dict[str, asyncio.Task] = {}

    import time

    last_action = 0.0

    async def listen(device) -> None:
        nonlocal last_action
        log.info("headset buttons attached: %s", device.name)
        try:
            async for event in device.async_read_loop():
                if event.type != evdev.ecodes.EV_KEY or event.value != 1:
                    continue
                now = time.monotonic()
                if now - last_action < DEBOUNCE_SECONDS:
                    continue
                if event.code in TOGGLE_CODES:
                    last_action = now
                    result = await asyncio.to_thread(music.toggle_pause)
                elif event.code in PAUSE_CODES:
                    last_action = now
                    result = await asyncio.to_thread(music.set_paused, True)
                elif event.code in PLAY_CODES:
                    last_action = now
                    result = await asyncio.to_thread(music.set_paused, False)
                else:
                    log.info("headset button: unmapped key code %d", event.code)
                    continue
                log.info("headset button (%d): %s", event.code, result)
        except OSError:
            log.info("headset buttons detached: %s", device.name)
        finally:
            watched.pop(device.path, None)

    while True:
        for device in await asyncio.to_thread(find_avrcp_devices):
            if device.path not in watched:
                watched[device.path] = asyncio.create_task(listen(device))
            else:
                device.close()
        await asyncio.sleep(RESCAN_SECONDS)
