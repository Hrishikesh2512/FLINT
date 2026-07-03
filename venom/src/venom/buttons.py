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

# Key codes for play/pause across headset firmwares (linux/input-event-codes.h)
PLAY_PAUSE_CODES = {
    200,  # KEY_PLAYCD
    201,  # KEY_PAUSECD
    164,  # KEY_PLAYPAUSE
    207,  # KEY_PLAY
}
RESCAN_SECONDS = 10


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

    async def listen(device) -> None:
        log.info("headset buttons attached: %s", device.name)
        try:
            async for event in device.async_read_loop():
                if (event.type == evdev.ecodes.EV_KEY and event.value == 1
                        and event.code in PLAY_PAUSE_CODES):
                    result = await asyncio.to_thread(music.toggle_pause)
                    log.info("headset button: %s", result)
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
